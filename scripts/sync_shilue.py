#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 personal_txt_files/释略大典.md 同步成博客「释略大典」页面（shilue.html）。

源文件格式（非常规整）：
    # A                          ← 字母分区（A-Z，另有开头的 "#"）
    **安堵：**                    ← 词条（整行加粗，可带冒号也可不带）
    方言，"安定"意。               ← 释义正文，可多行/多段

策略：**并集（最大化收容）**，与 sync_refreshment.py 一致
  * 源文件提供最新内容；
  * 基线只有一份：data/shilue-merged.md，每次构建都用它和源文件重算并集；
  * 词条以「归一化标题」为 key：同一分区内重名的两条合并成一条，
    内容不一致时保留信息量更大的版本（--prefer 可切换）；
  * 只在一侧的词条一律保留 —— 源文件删掉的内容不会从页面消失。

输出格式与页面现有格式一致：
    ## A                           ← 分区标题
    **安堵：**                     ← 词条名（整行加粗）
    方言，"安定"意。                ← 释义，条目内空行 = 新段落
    ---                            ← 同分区内不同词条之间的分隔

用法：
    python scripts/sync_shilue.py docs
    python scripts/sync_shilue.py docs --source ../personal_txt_files/释略大典.md
"""

import argparse
import difflib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_curated import (  # noqa: E402  —— 复用已打磨好的通用件
    build_head,
    clean_header,
    load_shell,
    read_text,
    write_text,
)

SOURCE_URLS = (
    "https://raw.githubusercontent.com/russianqin/personal_txt_files/master/%E9%87%8A%E7%95%A5%E5%A4%A7%E5%85%B8.md",
    "https://cdn.jsdelivr.net/gh/russianqin/personal_txt_files@master/%E9%87%8A%E7%95%A5%E5%A4%A7%E5%85%B8.md",
)
SLUG = "shilue"
TITLE = "释略大典"
SIMILARITY = 0.9
USER_AGENT = "russianqin-blog-shilue/1.0 (+https://russianqin.github.io)"

# 分区顺序：先 "#"（无字母的词条），再 A-Z
SECTION_HEAD_RE = re.compile(r"^#\s*(\S*)\s*$")
ENTRY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
TRAILING_PUNCT = "\uff09\uff0c\u3002\uff1b\u3001\uff01\uff1f\u3011\u300b\"'"


def log(msg):
    print("[shilue] %s" % msg)


def normalize(text):
    return re.sub(r"\s+", "", text or "")


def split_title(raw):
    """把 `安堵：` / `day job` / `语言:` 统一成 (标题, 是否有冒号)。"""
    title = raw.strip()
    has_colon = title.endswith(("\uff1a", ":"))
    title = title.rstrip("\uff1a:").strip()
    return title, has_colon


def norm_key(title):
    """合并用的 key：忽略空白、大小写、常见全角括号差异。"""
    return normalize(title).lower().replace("\uff08", "(").replace("\uff09", ")")


def key_of(entry):
    return norm_key(entry["title"])


# ---------------------------------------------------------------- 解析

def parse_source(text):
    """源文件：`# 分区` + `**词条**` + 后续正文行。"""
    sections = []
    current = None
    entry = None

    def start_section(name):
        nonlocal current
        current = {"name": name, "entries": []}
        sections.append(current)

    def flush_entry():
        nonlocal entry
        if entry is not None:
            body = [p for p in entry["paragraphs"] if normalize(p)]
            if body:
                entry["paragraphs"] = body
                current["entries"].append(entry)
            entry = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = SECTION_HEAD_RE.match(line)
        if head:
            flush_entry()
            start_section(head.group(1))
            continue
        match = ENTRY_RE.match(line)
        if match:
            flush_entry()
            if current is None:
                start_section("")
            title, has_colon = split_title(match.group(1))
            entry = {"title": title, "has_colon": has_colon, "paragraphs": []}
            continue
        if entry is None:
            # 词条之前散落的行：忽略（源文件里没有这种内容）
            continue
        entry["paragraphs"].append(line)

    flush_entry()
    return [s for s in sections if s["entries"]]


def parse_baseline(text):
    """基线格式：`## 分区` + `**词条**` + 段落，`---` 分隔词条。"""
    sections = []
    current = None
    entry = None
    lines = []

    def flush_paragraph():
        if lines:
            entry["paragraphs"].append("\n".join(lines))
            del lines[:]

    def flush_entry():
        nonlocal entry
        if entry is not None:
            flush_paragraph()
            body = [p for p in entry["paragraphs"] if normalize(p)]
            if body:
                entry["paragraphs"] = body
                current["entries"].append(entry)
            entry = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        head = re.match(r"^##\s*(.*)$", stripped)
        if head:
            flush_entry()
            current = {"name": head.group(1), "entries": []}
            sections.append(current)
            continue
        match = ENTRY_RE.match(stripped)
        if match:
            flush_entry()
            if current is None:
                current = {"name": "", "entries": []}
                sections.append(current)
            title, has_colon = split_title(match.group(1))
            entry = {"title": title, "has_colon": has_colon, "paragraphs": []}
            continue
        if stripped == "---":
            flush_entry()
            continue
        if not stripped:
            flush_paragraph()
            continue
        if entry is not None:
            lines.append(stripped)

    flush_entry()
    return [s for s in sections if s["entries"] or s["name"]]


# ---------------------------------------------------------------- 合并

def section_index(sections):
    return {s["name"]: s for s in sections}


def merge(baseline_sections, source_sections, prefer="source"):
    """并集：源文件词条优先，基线里独有的词条全部保留；同分区重名合并。"""
    merged = []
    stats = {
        "only_source": 0,
        "only_baseline": 0,
        "both": 0,
        "kept_longer": 0,
        "kept_baseline_format": 0,
        "source_total": sum(len(s["entries"]) for s in source_sections),
        "baseline_total": sum(len(s["entries"]) for s in baseline_sections),
    }
    duplicates = []

    base_by_name = section_index(baseline_sections)
    src_by_name = section_index(source_sections)
    order = [s["name"] for s in source_sections] + [
        s["name"] for s in baseline_sections if s["name"] not in src_by_name
    ]

    def put(section, entry, is_source):
        key = key_of(entry)
        match = None
        for index, other in enumerate(section["entries"]):
            if key_of(other) == key:
                match = index
                break
            if difflib.SequenceMatcher(None, key, key_of(other)).ratio() >= SIMILARITY:
                match = index
                break
        if match is None:
            section["entries"].append(
                {
                    "title": entry["title"],
                    "has_colon": entry["has_colon"],
                    "paragraphs": list(entry["paragraphs"]),
                    "origin": "source" if is_source else "baseline",
                    "from_source": is_source,
                    "key": key,
                }
            )
            stats["only_source" if is_source else "only_baseline"] += 1
            return

        other = section["entries"][match]
        if other["origin"] != "both":
            stats["only_source" if other["origin"] == "source" else "only_baseline"] -= 1
            stats["both"] += 1
            other["origin"] = "both"
            duplicates.append(
                {
                    "section": section["name"] or "#",
                    "title": entry["title"],
                    "kept": other["title"],
                }
            )
        new_text = normalize("".join(entry["paragraphs"]))
        old_text = normalize("".join(other["paragraphs"]))
        if len(new_text) > len(old_text):
            other["paragraphs"] = list(entry["paragraphs"])
            other["has_colon"] = entry["has_colon"]
            stats["kept_longer"] += 1
        elif not is_source and new_text == old_text:
            other["paragraphs"] = list(entry["paragraphs"])
            stats["kept_baseline_format"] += 1

    for name in order:
        section = {"name": name, "entries": []}
        merged.append(section)
        src = src_by_name.get(name)
        base = base_by_name.get(name)
        if src:
            for entry in src["entries"]:
                put(section, entry, True)
        if base:
            for entry in base["entries"]:
                put(section, entry, False)

    for section in merged:
        section["entries"] = [e for e in section["entries"] if e["origin"]]
        for entry in section["entries"]:
            entry.pop("key", None)
    return merged, stats, duplicates


# ---------------------------------------------------------------- 输出

def split_url(raw):
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


def to_baseline_markdown(sections):
    parts = []
    for section in sections:
        parts.append("## %s" % section["name"])
        parts.append("")
        for index, entry in enumerate(section["entries"]):
            if index:
                parts.append("---")
                parts.append("")
            parts.append("**%s%s**" % (entry["title"], "\uff1a" if entry["has_colon"] else ""))
            parts.append("")
            for paragraph_index, paragraph in enumerate(entry["paragraphs"]):
                if paragraph_index:
                    parts.append("")
                parts.append(paragraph)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


PAGE_CSS = """
<style>
.shilue-intro{color:var(--fgColor-muted,#59636e);margin:0 0 16px;}
.shilue-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px;}
.shilue-search{flex:1;min-width:200px;padding:6px 12px;font-size:14px;
  border:1px solid var(--borderColor-default,#d1d9e0);border-radius:6px;background:transparent;color:inherit;}
.shilue-search:focus{outline:none;border-color:var(--fgColor-accent,#0969da);}
.shilue-count{font-size:13px;color:var(--fgColor-muted,#59636e);white-space:nowrap;}
.shilue-nav{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:24px;padding-bottom:16px;
  border-bottom:1px solid var(--borderColor-muted,#d1d9e0);}
.shilue-nav a,.shilue-nav span{display:inline-flex;align-items:center;justify-content:center;
  min-width:28px;height:28px;padding:0 6px;border-radius:6px;font-size:13px;font-weight:600;
  border:1px solid var(--borderColor-default,#d1d9e0);text-decoration:none;color:inherit;}
.shilue-nav a:hover{background:var(--bgColor-accent-muted,#ddf4ff);border-color:var(--fgColor-accent,#0969da);}
.shilue-nav span.shilue-nav-empty{color:var(--fgColor-disabled,#8b949e);opacity:.5;}
.shilue-section{margin-bottom:32px;}
.shilue-section-title{font-size:24px;font-weight:700;margin:0 0 12px;
  padding-bottom:6px;border-bottom:1px solid var(--borderColor-muted,#d1d9e0);
  scroll-margin-top:16px;}
.shilue-section-empty{color:var(--fgColor-muted,#59636e);font-size:14px;font-style:italic;}
.shilue-entry{margin:0 0 16px;padding-left:12px;border-left:2px solid var(--borderColor-muted,#d1d9e0);}
.shilue-entry-title{font-weight:700;}
.shilue-entry-body{margin:4px 0 0;}
.shilue-entry-body p{margin:0 0 6px;}
.shilue-entry-body p:last-child{margin-bottom:0;}
.shilue-hidden{display:none!important;}
.shilue-top{position:fixed;right:20px;bottom:20px;width:40px;height:40px;border-radius:50%;
  display:none;align-items:center;justify-content:center;cursor:pointer;font-size:18px;
  border:1px solid var(--borderColor-default,#d1d9e0);background:var(--bgColor-default,#fff);
  color:inherit;box-shadow:0 2px 8px rgba(0,0,0,.12);}
.shilue-top.shilue-top-show{display:flex;}
@media (max-width:600px){
  .shilue-section-title{font-size:20px;}
  .shilue-top{right:12px;bottom:12px;}
}
</style>
"""

PAGE_JS = """
<script>
(function(){
  var input=document.getElementById('shilueSearch');
  var count=document.getElementById('shilueCount');
  var entries=[].slice.call(document.querySelectorAll('.shilue-entry'));
  var sections=[].slice.call(document.querySelectorAll('.shilue-section'));
  var total=entries.length;
  function setCount(n){count.textContent=(n===total?'共 '+total+' 条':'匹配 '+n+' / '+total+' 条');}
  function apply(){
    var q=(input.value||'').trim().toLowerCase();
    if(!q){
      entries.forEach(function(e){e.classList.remove('shilue-hidden');});
      sections.forEach(function(s){
        s.classList.remove('shilue-hidden');
        var empty=s.querySelector('.shilue-section-empty');
        if(empty){empty.classList.remove('shilue-hidden');}
      });
      setCount(total);
      return;
    }
    var hits=0;
    entries.forEach(function(e){
      var hit=e.textContent.toLowerCase().indexOf(q)>=0;
      e.classList.toggle('shilue-hidden',!hit);
      if(hit){hits++;}
    });
    sections.forEach(function(s){
      var has=s.querySelector('.shilue-entry:not(.shilue-hidden)');
      s.classList.toggle('shilue-hidden',!has);
      var empty=s.querySelector('.shilue-section-empty');
      if(empty){empty.classList.toggle('shilue-hidden',!!has);}
    });
    setCount(hits);
  }
  if(input){input.addEventListener('input',apply);}
  setCount(total);
  var top=document.getElementById('shilueTop');
  if(top){
    top.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
    window.addEventListener('scroll',function(){
      top.classList.toggle('shilue-top-show',window.scrollY>400);
    });
  }
})();
</script>
"""


def render_body(sections, alphabet):
    total = sum(len(s["entries"]) for s in sections)
    by_name = {s["name"]: s for s in sections}

    nav = []
    for letter in alphabet:
        section = by_name.get(letter)
        if section and section["entries"]:
            nav.append('<a href="#shilue-%s">%s</a>' % (html.escape(letter or "0"), html.escape(letter or "#")))
        else:
            nav.append('<span class="shilue-nav-empty" title="暂无条目">%s</span>' % html.escape(letter or "#"))

    parts = [
        '<p class="shilue-intro">《释略大典》是我随手记下的词语、典故与概念，'
        "共 %d 条，按首字母排列。随时增补。</p>" % total,
        '<div class="shilue-toolbar">'
        '<input id="shilueSearch" class="shilue-search" type="search" '
        'placeholder="搜索词条或释义…" aria-label="搜索词条">'
        '<span id="shilueCount" class="shilue-count"></span></div>',
        '<nav class="shilue-nav">%s</nav>' % "".join(nav),
    ]

    for letter in alphabet:
        section = by_name.get(letter)
        # 空分区也渲染占位标题（方便以后补充；导航条里对应字母置灰不可点）
        entries = section["entries"] if section else []
        label = letter or "#"
        parts.append('<section class="shilue-section" id="shilue-%s">' % html.escape(letter or "0"))
        parts.append('<h2 class="shilue-section-title">%s</h2>' % html.escape(label))
        if not entries:
            parts.append('<p class="shilue-section-empty">暂无条目</p>')
            parts.append("</section>")
            continue
        for entry in entries:
            title = entry["title"] + ("：" if entry["has_colon"] else "")
            body = "\n".join(
                "<p>%s</p>" % render_paragraph(p) for p in entry["paragraphs"]
            )
            parts.append(
                '<div class="shilue-entry"><div class="shilue-entry-title">%s</div>'
                '<div class="shilue-entry-body">%s</div></div>'
                % (html.escape(title), body)
            )
        parts.append("</section>")

    parts.append('<button class="shilue-top" id="shilueTop" type="button" title="回到顶部">↑</button>')
    parts.append(PAGE_CSS)
    parts.append(PAGE_JS)
    return "\n".join(parts)


# ---------------------------------------------------------------- 拉取

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


# ---------------------------------------------------------------- 主流程

def build_alphabet():
    return ["#"] + [chr(code) for code in range(ord("A"), ord("Z") + 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--source", help="本地源文件路径（离线调试用，省略则联网拉取）")
    parser.add_argument(
        "--prefer",
        choices=("source", "baseline"),
        default="source",
        help="内容高度相似时优先哪一边（默认 source）",
    )
    parser.add_argument("--site", default="https://russianqin.github.io")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    if not docs.is_dir():
        log("找不到 docs 目录：%s" % docs)
        return 1

    merged_path = root / "data" / "shilue-merged.md"

    if args.source:
        source_text = read_text(args.source)
        log("使用本地源文件：%s（%d 字符）" % (args.source, len(source_text)))
    else:
        source_text = fetch_source()

    source_sections = parse_source(source_text) if source_text else []
    if source_text and not source_sections:
        log("源文件解析结果为空")

    if merged_path.exists():
        baseline_sections = parse_baseline(read_text(merged_path))
        log("基线：%s（%d 条）" % (merged_path.name, sum(len(s["entries"]) for s in baseline_sections)))
    else:
        baseline_sections = []
        log("基线文件不存在，首次以源文件建立：%s" % merged_path.name)

    if not source_sections and not baseline_sections:
        log("源文件与基线都没有内容，退出")
        return 1

    merged, stats, duplicates = merge(baseline_sections, source_sections, args.prefer)
    write_text(merged_path, to_baseline_markdown(merged))

    total = sum(len(s["entries"]) for s in merged)
    log(
        "并集结果：%d 条 / %d 个分区（源文件 %d 条 + 基线 %d 条）"
        % (total, len(merged), stats["source_total"], stats["baseline_total"])
    )
    log(
        "  新增自源文件 %d，两边都有 %d，仅基线保留 %d，采用更长版本 %d，沿用基线格式 %d"
        % (
            stats["only_source"],
            stats["both"],
            stats["only_baseline"],
            stats["kept_longer"],
            stats["kept_baseline_format"],
        )
    )
    if duplicates:
        log("  合并的重名词条 %d 组：" % len(duplicates))
        for item in duplicates:
            log("    [%s] %s" % (item["section"], item["title"]))
    log("已写出 %s" % merged_path.relative_to(root).as_posix())

    shell = load_shell(docs)
    if shell is None:
        log("找不到可借用的页面骨架（docs/post/*.html 或 index.html），无法生成页面")
        return 1

    alphabet = build_alphabet()
    body = render_body(merged, alphabet)
    description = "释略大典：%d 条词语、典故与概念，按首字母排列，可搜索。" % total
    canonical = "%s/%s.html" % (args.site.rstrip("/"), SLUG)
    head = build_head(shell["head"], "%s - 五环魔法师" % TITLE, description, canonical)

    page = [
        "<!DOCTYPE html>\n",
        head,
        "</head>\n<body>\n",
        clean_header(shell["header"], TITLE),
        '<div id="content">\n<div class="markdown-body" id="postBody">\n',
        body,
        "\n</div>\n</div>\n",
        shell["footer"],
    ]
    out = docs / ("%s.html" % SLUG)
    write_text(out, "".join(page))
    log("已生成 %s（正文 %d 字符，%d 条）" % (out.relative_to(root).as_posix(), len(body), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
