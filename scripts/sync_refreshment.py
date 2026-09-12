#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 personal_txt_files/日日新.md 同步成博客「日日新」页面（refreshment.html）。

策略：**并集（最大化收容）**
  * 源文件（personal_txt_files/日日新.md）提供最新内容；
  * 基线（data/refreshment-archive.md，之后累积成 data/refreshment-merged.md）保存历史内容；
  * 两边都有、内容高度相似的条目视为「同一条被改过」，保留信息量更大的版本；
  * 只在一侧的条目一律保留 —— 源文件删掉内容也不会让页面内容消失；
  * 完全相同的文本只保留一条：默认以源文件的日期为准（--prefer 可切换）。

输出格式与页面现有格式完全一致：
    ### 2026.08.24                 ← 日期标题
    段落一                          ← 条目内的空行 = 新段落
    段落二
    ---                            ← 同一天内不同条目之间的分隔
    另一条
    条目内部没有空行的换行渲染成 <br>

用法：
    python scripts/sync_refreshment.py [docs_dir]
    python scripts/sync_refreshment.py docs --source ../personal_txt_files/日日新.md
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SOURCE_URLS = (
    "https://raw.githubusercontent.com/russianqin/personal_txt_files/master/%E6%97%A5%E6%97%A5%E6%96%B0.md",
    "https://cdn.jsdelivr.net/gh/russianqin/personal_txt_files@master/%E6%97%A5%E6%97%A5%E6%96%B0.md",
)
ISSUE_NOTE = "本页内容自动同步自 personal_txt_files"
ISSUE_LABEL = "refreshment"
SIMILARITY = 0.9
USER_AGENT = "russianqin-blog-sync/1.0 (+https://russianqin.github.io)"

DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*[\u2014\u2013-]{1,2}\s*")
HEAD_RE = re.compile(r"^###\s*(\d{4}\.\d{2}\.\d{2})\s*$")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
TRAILING_PUNCT = "\uff09\uff0c\u3002\uff1b\u3001\uff01\uff1f\u3011\u300b\"'"


def log(msg):
    print("[refreshment] %s" % msg)


def normalize(text):
    return re.sub(r"\s+", "", text or "")


def norm_entry(entry):
    return normalize("".join(entry["paragraphs"]))


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path, text):
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ---------------------------------------------------------------- 解析

def split_date_line(line):
    """一行里可能塞了多条（日期标记出现在行中间），这里全部拆开。"""
    matches = list(DATE_RE.finditer(line))
    if not matches:
        return []
    chunks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        date = "%s.%02d.%02d" % (match.group(1), int(match.group(2)), int(match.group(3)))
        chunks.append((date, line[match.end():end].strip()))
    return chunks


def parse_source(text):
    """源文件：`YYYY.MM.DD——内容`，一条一行，续行属于同一条（渲染成 <br>）。"""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        chunks = split_date_line(line)
        if chunks:
            for date, chunk in chunks:
                entries.append({"date": date, "paragraphs": [chunk]})
            continue
        if entries:
            paragraph = entries[-1]["paragraphs"][-1]
            entries[-1]["paragraphs"][-1] = (paragraph + "\n" + line) if paragraph else line
    return [entry for entry in entries if norm_entry(entry)]


def parse_page(text):
    """页面格式：`### 日期` + 段落；条目内空行 = 新段落，`---` = 新条目。"""
    entries = []
    date = None
    paragraphs = []
    lines = []

    def flush_paragraph():
        if lines:
            paragraphs.append("\n".join(lines))
            del lines[:]

    def flush_entry():
        flush_paragraph()
        kept = [p for p in paragraphs if normalize(p)]
        if date is not None and kept:
            entries.append({"date": date, "paragraphs": kept})
        del paragraphs[:]

    for raw in text.splitlines():
        line = raw.rstrip()
        head = HEAD_RE.match(line.strip())
        if head:
            flush_entry()
            date = head.group(1)
            continue
        if line.strip() == "---":
            flush_entry()
            continue
        if not line.strip():
            flush_paragraph()
            continue
        lines.append(line.strip())
    flush_entry()
    return entries


# ---------------------------------------------------------------- 合并

def merge(archive_entries, source_entries, prefer="source"):
    """并集：源文件条目优先，基线里独有的条目全部保留。"""
    merged = []
    by_date = {}
    stats = {
        "only_source": 0,
        "only_archive": 0,
        "both": 0,
        "kept_longer": 0,
        "kept_page_format": 0,
        "merged_duplicates": 0,
        "source_total": len(source_entries),
        "archive_total": len(archive_entries),
    }

    def find_same(date, norm):
        for index in by_date.get(date, []):
            other = merged[index]
            if other["norm"] == norm or norm in other["norm"] or other["norm"] in norm:
                return index
            if difflib.SequenceMatcher(None, norm, other["norm"]).ratio() >= SIMILARITY:
                return index
        return None

    def put(entry, is_source):
        norm = norm_entry(entry)
        index = find_same(entry["date"], norm)
        if index is None:
            merged.append(
                {
                    "date": entry["date"],
                    "paragraphs": list(entry["paragraphs"]),
                    "norm": norm,
                    "origin": "source" if is_source else "archive",
                    "from_source": is_source,
                }
            )
            by_date.setdefault(entry["date"], []).append(len(merged) - 1)
            stats["only_source" if is_source else "only_archive"] += 1
            return

        other = merged[index]
        if other["origin"] != "both":
            stats["only_source" if other["origin"] == "source" else "only_archive"] -= 1
            stats["both"] += 1
            other["origin"] = "both"
        if len(norm) > len(other["norm"]):
            # 一边的内容更长，保留信息更全的那份
            other["paragraphs"] = list(entry["paragraphs"])
            other["norm"] = norm
            stats["kept_longer"] += 1
        elif not is_source and norm == other["norm"]:
            # 内容一致时，保留页面原有的段落结构（更贴近现在的排版）
            other["paragraphs"] = list(entry["paragraphs"])
            stats["kept_page_format"] += 1

    for entry in source_entries:
        put(entry, True)
    for entry in archive_entries:
        put(entry, False)

    # 文本完全相同（忽略空白）只保留一条：默认以源文件一侧为准
    grouped = {}
    for item in merged:
        grouped.setdefault(item["norm"], []).append(item)

    result = []
    for item in merged:
        pool = grouped[item["norm"]]
        if len(pool) == 1:
            result.append(item)
            continue
        if prefer == "page":
            preferred = [x for x in pool if x["origin"] == "archive"] or pool
        elif prefer == "earliest":
            preferred = pool
        else:
            preferred = [x for x in pool if x["from_source"]] or pool
        chosen = min(preferred, key=lambda x: x["date"])
        if chosen is item:
            result.append(item)
        else:
            stats["merged_duplicates"] += 1
            if item["origin"] == "both":
                stats["both"] -= 1
            elif item["origin"] == "source":
                stats["only_source"] -= 1
            else:
                stats["only_archive"] -= 1
    return result, stats


def order_entries(merged, source_date_order):
    """先按源文件的日期顺序，再把基线独有日期按时间倒序插进正确位置。"""
    groups = {}
    for item in merged:
        groups.setdefault(item["date"], []).append(item["paragraphs"])

    ordered = [date for date in source_date_order if date in groups]
    rest = sorted([date for date in groups if date not in ordered], reverse=True)
    for date in rest:
        position = 0
        while position < len(ordered) and ordered[position] > date:
            position += 1
        ordered.insert(position, date)
    return [(date, groups[date]) for date in ordered]


# ---------------------------------------------------------------- 输出

def split_url(raw):
    """拆掉 URL 尾巴上的中文标点（避免把「）」当成链接的一部分）。"""
    url = raw
    trailing = ""
    while url and url[-1] in TRAILING_PUNCT:
        trailing = url[-1] + trailing
        url = url[:-1]
    return url, trailing


def render_paragraph(text):
    """转义 HTML、换行变 <br>、裸链接变 <a rel=nofollow>。"""
    parts = []
    last = 0
    for match in URL_RE.finditer(text):
        url, trailing = split_url(match.group(0))
        if not url:
            continue
        parts.append(html.escape(text[last:match.start()], quote=False))
        parts.append(
            '<a href="%s" rel="nofollow">%s</a>%s'
            % (html.escape(url, quote=True), html.escape(url, quote=False), html.escape(trailing, quote=False))
        )
        last = match.end()
    parts.append(html.escape(text[last:], quote=False))
    return "".join(parts).replace("\n", "<br>")


def render_body(groups):
    parts = []
    for date, entries in groups:
        parts.append("<h3>%s</h3>" % date)
        for index, paragraphs in enumerate(entries):
            if index:
                parts.append("<hr>")
            parts.append("<p>%s</p>" % "</p>\n<p>".join(render_paragraph(p) for p in paragraphs))
    return "\n".join(parts)


def to_markdown(groups):
    parts = []
    for date, entries in groups:
        parts.append("### %s" % date)
        parts.append("")
        for index, paragraphs in enumerate(entries):
            if index:
                parts.append("---")
                parts.append("")
            for paragraph_index, paragraph in enumerate(paragraphs):
                if paragraph_index:
                    parts.append("")
                parts.append(paragraph)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def inject_body(page_html, body, description):
    marker = 'id="postBody">'
    start = page_html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    stop = page_html.find('<div style="font-size:small', start)
    if stop < 0:
        return None
    new_html = page_html[:start] + "\n" + body + "</div>\n" + page_html[stop:]
    escaped = html.escape(description, quote=True)
    return re.sub(
        r'(<meta name="description" content=")[^"]*(")',
        lambda match: match.group(1) + escaped + match.group(2),
        new_html,
        count=1,
    )


# ---------------------------------------------------------------- 拉取 / Issue

def fetch_source():
    for url in SOURCE_URLS:
        target = url + ("&" if "?" in url else "?") + "t=%d" % int(time.time())
        try:
            request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=25) as response:
                text = response.read().decode("utf-8", "replace")
            if text.strip():
                log("已拉取源文件：%s（%d 字符）" % (url.split("/")[2], len(text)))
                return text
            log("源文件内容为空：%s" % url)
        except Exception as exc:  # noqa: BLE001
            log("拉取失败 %s：%s" % (url, exc))
    return None


def github_request(url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="PATCH" if data else "GET")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", "Bearer %s" % token)
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def update_issue_note():
    """把 Issue「日日新」正文换成一行说明（幂等；本地无 token 时跳过）。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("未提供 GITHUB_TOKEN/GITHUB_REPOSITORY，跳过 Issue 正文更新")
        return
    try:
        url = "https://api.github.com/repos/%s/issues?state=all&labels=%s&per_page=20" % (repo, ISSUE_LABEL)
        for issue in github_request(url, token):
            if (issue.get("body") or "").strip() == ISSUE_NOTE:
                continue
            github_request(
                "https://api.github.com/repos/%s/issues/%s" % (repo, issue["number"]),
                token,
                {"body": ISSUE_NOTE},
            )
            log("已更新 Issue #%s 正文为同步说明" % issue["number"])
    except Exception as exc:  # noqa: BLE001
        log("更新 Issue 正文失败（不影响页面生成）：%s" % exc)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--source", help="本地源文件路径（离线调试用，省略则联网拉取）")
    parser.add_argument(
        "--prefer",
        choices=("source", "page", "earliest"),
        default="source",
        help="完全重复的内容保留哪一边的日期（默认 source）",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    archive_path = root / "data" / "refreshment-archive.md"
    merged_path = root / "data" / "refreshment-merged.md"

    baseline_path = merged_path if merged_path.exists() else archive_path
    if not baseline_path.exists():
        log("找不到基线文件 %s，无法同步" % baseline_path)
        return 1
    archive_entries = parse_page(read_text(baseline_path))
    log("基线：%s（%d 条）" % (baseline_path.name, len(archive_entries)))

    if args.source:
        source_text = read_text(args.source)
        log("使用本地源文件：%s（%d 字符）" % (args.source, len(source_text)))
    else:
        source_text = fetch_source()

    source_entries = parse_source(source_text) if source_text else []
    if source_text and not source_entries:
        log("源文件解析结果为空，本次按基线渲染")

    merged, stats = merge(archive_entries, source_entries, args.prefer)
    source_date_order = []
    for entry in source_entries:
        if entry["date"] not in source_date_order:
            source_date_order.append(entry["date"])
    groups = order_entries(merged, source_date_order)

    write_text(merged_path, to_markdown(groups))
    total = sum(len(entries) for _date, entries in groups)
    log(
        "并集结果：%d 条 / %d 个日期（源文件 %d 条 + 基线 %d 条）"
        % (total, len(groups), stats["source_total"], stats["archive_total"])
    )
    log(
        "  新增自源文件 %d，两边都有 %d，仅基线保留 %d，完全相同合并 %d，采用更长版本 %d，沿用页面段落 %d"
        % (
            stats["only_source"],
            stats["both"],
            stats["only_archive"],
            stats["merged_duplicates"],
            stats["kept_longer"],
            stats["kept_page_format"],
        )
    )
    if groups:
        log("最新日期：%s" % groups[0][0])
    log("已写出 %s" % merged_path.relative_to(root).as_posix())

    page = docs / "refreshment.html"
    if page.exists():
        body = render_body(groups)
        description = "日日新：随想、摘录与金句合集，共 %d 条，最后同步 %s" % (
            total,
            time.strftime("%Y-%m-%d", time.localtime()),
        )
        updated = inject_body(read_text(page), body, description)
        if updated is None:
            log("页面结构不符合预期，未注入正文：%s" % page)
        else:
            write_text(page, updated)
            log("已更新 %s（正文 %d 字符）" % (page.relative_to(root).as_posix(), len(body)))
    else:
        log("未找到 %s，跳过页面注入" % page)

    update_issue_note()
    return 0


if __name__ == "__main__":
    sys.exit(main())
