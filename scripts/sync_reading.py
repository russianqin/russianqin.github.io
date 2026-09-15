#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 RongZhaiSuiBi-BiJi（读书笔记）同步成博客的「读书心得」页面。

生成内容：
  * docs/reading.html                    —— 读书心得首页（书单，一本书一张卡片）
  * docs/reading/<书>.html                —— 某本书的目录页（按卷分组 + 每一则 + 站内搜索）
  * docs/reading/<书>-<卷>-<则>.html       —— 每一则一个页面（白话解读 + 我的心得 + 洪迈原文）

源文件格式（一卷一个 md，内容高度规整）：
    **1.欧率更帖**                 ← 则目（整行加粗、带编号）
    （白话解读，普通段落）
    **法帖是对一种书法形式的统称…**   ← 我的心得（整行加粗）
    容斋随笔·随笔卷一·欧率更帖       ← 原文出处标记
    临川石刻杂法帖一卷，载…          ← 原文（标记之后的内容）

用法：
    python scripts/sync_reading.py docs                          # 联网浅克隆源仓库
    python scripts/sync_reading.py docs --source D:\\RongZhaiSuiBi-BiJi   # 本地调试
    python scripts/sync_reading.py docs --limit 5                # 只生成前 5 则（调试）
"""

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_curated import (  # noqa: E402  —— 复用「文章收藏」里已经打磨好的通用件
    add_lazy_loading,
    build_head,
    clean_header,
    load_shell,
    markdown_to_html,
    plain_text,
    read_text,
    write_text,
)

INDEX_SLUG = "reading"
OUT_DIR_NAME = "reading"
USER_AGENT = "russianqin-blog-reading/1.0 (+https://russianqin.github.io)"

# 书单：以后加新书，只要往这里加一条（kind="juan" 表示"一个 md 一卷"）
BOOKS = [
    {
        "slug": "rongzhai-suibi",
        "title": "容斋随笔",
        "author": "洪迈（南宋）",
        "kind": "juan",
        "repo": "https://github.com/russianqin/RongZhaiSuiBi-BiJi.git",
        "desc": "南宋洪迈的读书笔记，随手记下经史百家、诗词典故与朝野见闻。"
                "这里是我的精读：每一则都配白话解读、我的心得，以及原文对照。",
    },
]

JUAN_FILE_RE = re.compile(r"^(?P<num>\d+)\s*[.、]\s*(?P<label>.+?)\.md$")
ZE_TITLE_RE = re.compile(r"^\*\*(?P<num>\d+)\s*[.．、]\s*(?P<title>[^*]+?)\*\*\s*$")
BOLD_LINE_RE = re.compile(r"^\*\*(?P<text>.+?)\*\*\s*$")
SOURCE_MARK_RE = re.compile(r"^容斋随笔\s*[·・]\s*(?P<volume>[^·・]+)\s*[·・]\s*(?P<title>.+?)\s*$")


def log(message):
    print("[%s] [reading] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def fetch_repo(repo, local_dir, prefix):
    """返回 (仓库目录, 是否为临时目录)"""
    if local_dir:
        path = Path(local_dir).resolve()
        if not path.is_dir():
            raise SystemExit("找不到本地源目录：%s" % path)
        log("使用本地源目录：%s" % path)
        return path, False
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    log("浅克隆 %s ..." % repo)
    subprocess.run(
        ["git", "clone", "--depth", "1", repo, str(tmp / "repo")],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    return tmp / "repo", True


def repo_last_commit(repo_dir):
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()[:10]
    except Exception:
        return ""


def juan_title_from_label(label):
    """文件名里的"容斋随笔—随笔卷二" → "随笔卷二"""
    text = re.sub(r"^容斋随笔\s*[—\-－]*\s*", "", label).strip()
    return text or label


def parse_juan(path):
    """把一个 md 解析成若干"则"（含卷首的序）"""
    text = read_text(path).replace("\r\n", "\n")
    match = JUAN_FILE_RE.match(path.name)
    file_num = int(match.group("num")) if match else 0
    file_label = juan_title_from_label(match.group("label")) if match else path.stem

    entries = []
    current = None
    in_origin = False

    def start(num, title):
        entry = {
            "num": num,
            "title": title,
            "volume": "",          # 从原文标记里取，取不到就用文件名
            "notes": [],
            "thoughts": [],
            "origin": [],
            "file_num": file_num,
            "file_label": file_label,
        }
        entries.append(entry)
        return entry

    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        ze = ZE_TITLE_RE.match(stripped)
        if ze:
            current = start(int(ze.group("num")), ze.group("title").strip())
            in_origin = False
            continue

        mark = SOURCE_MARK_RE.match(stripped)
        if mark:
            if current is None:
                current = start(0, "序")
            if not current["volume"]:
                current["volume"] = mark.group("volume").strip()
            in_origin = True
            continue

        if current is None:
            # 第一个则目之前的内容：整行加粗的行是卷首小标题（例如"**序**"）
            bold = BOLD_LINE_RE.match(stripped)
            if bold:
                title = bold.group("text").strip()
                if title and len(title) <= 12:
                    current = start(0, title)
                    current["volume"] = title
                    # 卷首这块（例如洪迈的自序）本身就是原文，之后的内容都算原文
                    in_origin = True
                    continue
            if stripped:
                current = start(0, "序")
                current["volume"] = "序"
                in_origin = True
            else:
                continue

        if not stripped:
            continue

        if in_origin:
            bold = BOLD_LINE_RE.match(stripped)
            # 原文经常整段写成加粗（例如卷首的序），这里把 ** 去掉
            current["origin"].append(bold.group("text").strip() if bold else stripped)
            continue

        bold = BOLD_LINE_RE.match(stripped)
        if bold:
            current["thoughts"].append(bold.group("text").strip())
        else:
            current["notes"].append(stripped)

    for entry in entries:
        if not entry["volume"]:
            entry["volume"] = entry["file_label"]
    return entries


def collect_book(repo_dir):
    files = []
    for path in sorted(repo_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if path.name.lower() == "readme.md":
            continue
        if not JUAN_FILE_RE.match(path.name):
            continue
        files.append(path)

    entries = []
    for path in files:
        entries.extend(parse_juan(path))

    # 稳定链接：<书>-<卷号>-<则号>
    for entry in entries:
        entry["slug"] = "%s-%d-%02d" % ("", entry["file_num"], entry["num"])
    return entries


def group_by_volume(entries):
    groups = []
    for entry in entries:
        key = entry["volume"]
        if not groups or groups[-1]["volume"] != key:
            groups.append({"volume": key, "entries": []})
        groups[-1]["entries"].append(entry)
    return groups


CN_NUM = "零一二三四五六七八九十"


def volume_label(groups):
    """给"序 + 七卷"这种描述（序不算卷）"""
    titles = [group["volume"] for group in groups]
    if titles and titles[0] == "序":
        rest = len(titles) - 1
        number = CN_NUM[rest] if 1 <= rest <= 10 else str(rest)
        return "序 + %s卷" % number
    return "%d 卷" % len(titles)


def render_thoughts(entry):
    if not entry["thoughts"]:
        return ""
    items = "".join("<p>%s</p>" % html.escape(text) for text in entry["thoughts"])
    return (
        '<div class="reading-thoughts"><div class="reading-thoughts-title">我的心得</div>%s</div>\n' % items
    )


def render_origin(entry):
    if not entry["origin"]:
        return ""
    body = "".join("<p>%s</p>" % html.escape(text) for text in entry["origin"])
    label = "洪迈原文"
    if entry["volume"]:
        label += " · " + entry["volume"]
    if entry["title"] and entry["num"]:
        label += " · " + entry["title"]
    return (
        '<div class="reading-origin"><div class="reading-origin-title">%s</div>'
        "<blockquote>%s</blockquote></div>\n" % (html.escape(label), body)
    )


def render_notes(entry, body_html):
    if body_html.strip():
        return '<div class="markdown-body reading-notes" id="postBody">%s</div>\n' % body_html
    # 卷首的"序"本来就是原文，不提示"还没写解读"
    if entry["num"] and (entry["thoughts"] or entry["origin"]):
        return '<div class="reading-todo">这一则还没写白话解读，先放原文。</div>\n'
    return ""


def render_entry_page(book, entry, prev_entry, next_entry, shell, site):
    base = site.rstrip("/")
    canonical = "%s/%s/%s.html" % (base, OUT_DIR_NAME, entry["slug"])
    title = "%s·%s - %s" % (entry["volume"], entry["title"], book["title"])
    description = plain_text(" ".join(entry["notes"]) or " ".join(entry["origin"]), 120)
    if not description:
        description = "%s 的读书笔记：%s" % (book["title"], entry["title"])
    head = build_head(
        shell["head"],
        "%s - 读书心得" % title,
        description,
        canonical,
        article=True,
        source_url=book["repo"].replace(".git", ""),
    )

    parts = ["<!DOCTYPE html>\n", head, "</head>\n<body>\n"]
    parts.append(clean_header(shell["header"], "%s · %s" % (entry["volume"], entry["title"])))
    parts.append('<div id="content">\n')
    parts.append(
        '<div class="reading-crumb"><a href="/%s.html">读书心得</a> › '
        '<a href="/%s/%s.html">%s</a> › <span>%s</span></div>\n'
        % (
            INDEX_SLUG,
            OUT_DIR_NAME,
            book["slug"],
            html.escape(book["title"]),
            html.escape(entry["volume"]),
        )
    )
    parts.append('<h1 class="reading-ze-title">%s</h1>\n' % html.escape(entry["title"]))
    meta_bits = [book["title"], book["author"], entry["volume"]]
    if entry["num"]:
        meta_bits.append("第 %d 则" % entry["num"])
    parts.append('<div class="reading-ze-meta">%s</div>\n' % html.escape(" · ".join(meta_bits)))

    parts.append(render_notes(entry, add_lazy_loading(markdown_to_html("\n\n".join(entry["notes"])))))
    parts.append(render_thoughts(entry))
    parts.append(render_origin(entry))

    parts.append('<div class="reading-nav">')
    if prev_entry:
        parts.append(
            '<a href="/%s/%s.html">← 上一则：%s</a>'
            % (OUT_DIR_NAME, prev_entry["slug"], html.escape(prev_entry["title"]))
        )
    else:
        parts.append("<span></span>")
    parts.append('<a href="/%s/%s.html">目录</a>' % (OUT_DIR_NAME, book["slug"]))
    if next_entry:
        parts.append(
            '<a href="/%s/%s.html">下一则：%s →</a>'
            % (OUT_DIR_NAME, next_entry["slug"], html.escape(next_entry["title"]))
        )
    else:
        parts.append("<span></span>")
    parts.append("</div>\n")
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


def render_book_page(book, entries, groups, shell, site):
    base = site.rstrip("/")
    canonical = "%s/%s/%s.html" % (base, OUT_DIR_NAME, book["slug"])
    total = len(entries)
    description = "%s（%s）的读书笔记：共 %d 则，每一则都有白话解读、我的心得和原文对照。" % (
        book["title"],
        book["author"],
        total,
    )
    head = build_head(shell["head"], "%s - 读书心得" % book["title"], description, canonical)

    rows = []
    for group in groups:
        items = []
        for entry in group["entries"]:
            label = entry["title"] if not entry["num"] else "%d. %s" % (entry["num"], entry["title"])
            items.append(
                '<li class="reading-ze"><a href="/%s/%s.html" data-title="%s">%s</a></li>'
                % (
                    OUT_DIR_NAME,
                    entry["slug"],
                    html.escape(label.lower(), quote=True),
                    html.escape(label),
                )
            )
        rows.append(
            '<section class="reading-juan"><h2 class="reading-juan-title">%s'
            '<span class="reading-juan-count">%d 则</span></h2>'
            '<ul class="reading-ze-list">%s</ul></section>\n'
            % (html.escape(group["volume"]), len(group["entries"]), "".join(items))
        )

    script = (
        "<script>(function(){var box=document.getElementById('readingSearch');"
        "var list=document.getElementById('readingList');var items=[].slice.call(list.querySelectorAll('.reading-ze'));"
        "var sections=[].slice.call(list.querySelectorAll('.reading-juan'));"
        "var count=document.getElementById('readingCount');"
        "function update(){var q=(box.value||'').trim().toLowerCase();var shown=0;"
        "items.forEach(function(li){var a=li.firstChild;"
        "var hit=!q||a.getAttribute('data-title').indexOf(q)>=0;"
        "li.style.display=hit?'':'none';if(hit)shown++;});"
        "sections.forEach(function(sec){var any=sec.querySelector('.reading-ze:not([style*=\"none\"])');"
        "sec.style.display=any?'':'none';});"
        "count.textContent='显示 '+shown+' / '+items.length+' 则';}"
        "box.addEventListener('input',update);update();})();</script>\n"
    )

    parts = ["<!DOCTYPE html>\n", head, "</head>\n<body>\n"]
    parts.append(clean_header(shell["header"], book["title"]))
    parts.append('<div id="content">\n')
    parts.append(
        '<div class="reading-book-head"><h1 class="reading-book-title">%s</h1>'
        '<div class="reading-book-meta">%s · %s · 共 %d 则</div>'
        '<p class="reading-book-desc">%s</p>'
        '<div class="reading-book-links"><a class="btn btn-sm" href="%s" target="_blank" '
        'rel="noopener noreferrer">在 GitHub 上查看这个项目 ↗</a></div></div>\n'
        % (
            html.escape(book["title"]),
            html.escape(book["author"]),
            html.escape(volume_label(groups)),
            total,
            html.escape(book["desc"]),
            html.escape(book["repo"].replace(".git", ""), quote=True),
        )
    )
    parts.append(
        '<div class="reading-toolbar"><input id="readingSearch" type="search" class="form-control" '
        'placeholder="搜索则名（共 %d 则）" aria-label="搜索则目">'
        '<span id="readingCount" class="reading-count"></span></div>\n' % total
    )
    parts.append('<div id="readingList">%s</div>\n' % "".join(rows))
    parts.append(script)
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


def render_index_page(books_info, shell, site):
    canonical = "%s/%s.html" % (site.rstrip("/"), INDEX_SLUG)
    total_entries = sum(info["count"] for info in books_info)
    description = "读书心得：%d 本书、共 %d 则笔记，每则都配白话解读、心得与原文对照。" % (
        len(books_info),
        total_entries,
    )
    head = build_head(shell["head"], "读书心得", description, canonical)

    cards = []
    for info in books_info:
        book = info["book"]
        meta = [book["author"], "%s · %d 则" % (info["volume_label"], info["count"])]
        if info["updated"]:
            meta.append("最近更新 " + info["updated"])
        cards.append(
            '<a class="reading-book-card" href="/%s/%s.html">'
            '<div class="reading-card-title">%s</div>'
            '<div class="reading-card-meta">%s</div>'
            '<div class="reading-card-desc">%s</div>'
            '<div class="reading-card-more">开始阅读 →</div></a>\n'
            % (
                OUT_DIR_NAME,
                book["slug"],
                html.escape(book["title"]),
                html.escape(" · ".join(meta)),
                html.escape(book["desc"]),
            )
        )

    parts = ["<!DOCTYPE html>\n", head, "</head>\n<body>\n"]
    parts.append(clean_header(shell["header"], "读书心得"))
    parts.append('<div id="content">\n')
    parts.append(
        '<div class="reading-home"><p class="reading-home-intro">'
        "读书笔记内容自动同步自我的笔记仓库，随时更新。"
        "</p></div>\n"
    )
    parts.append('<div class="reading-books">%s</div>\n' % "".join(cards))
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--source", help="本地源仓库目录（离线调试用，只对第一本书生效）")
    parser.add_argument("--limit", type=int, default=0, help="每本书只生成前 N 则（调试用）")
    parser.add_argument("--site", default="https://russianqin.github.io")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    if not docs.is_dir():
        log("找不到 docs 目录：%s" % docs)
        return 1

    shell = load_shell(docs)
    if not shell:
        log("无法从已生成页面提取模板，跳过")
        return 1

    out_dir = docs / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉上一轮生成的页面：源仓库里删掉的则不会留下残页
    for stale in out_dir.glob("*.html"):
        try:
            stale.unlink()
        except OSError:
            pass

    books_info = []
    for index, book in enumerate(BOOKS):
        source = args.source if index == 0 else None
        repo_dir, temporary = fetch_repo(book["repo"], source, "wzsc-reading-")
        try:
            entries = collect_book(repo_dir)
            updated = repo_last_commit(repo_dir)
        finally:
            if temporary:
                shutil.rmtree(str(repo_dir.parent), ignore_errors=True)

        if not entries:
            log("《%s》没有解析到内容，跳过" % book["title"])
            continue
        if args.limit:
            entries = entries[: args.limit]

        for entry in entries:
            entry["slug"] = "%s-%d-%02d" % (book["slug"], entry["file_num"], entry["num"])

        groups = group_by_volume(entries)
        log(
            "《%s》：%d 则，%d 个分卷%s"
            % (book["title"], len(entries), len(groups), ("，最近更新 " + updated) if updated else "")
        )

        for position, entry in enumerate(entries):
            prev_entry = entries[position - 1] if position > 0 else None
            next_entry = entries[position + 1] if position + 1 < len(entries) else None
            write_text(
                out_dir / ("%s.html" % entry["slug"]),
                render_entry_page(book, entry, prev_entry, next_entry, shell, args.site),
            )

        write_text(out_dir / ("%s.html" % book["slug"]), render_book_page(book, entries, groups, shell, args.site))
        books_info.append(
            {
                "book": book,
                "count": len(entries),
                "volumes": len(groups),
                "volume_label": volume_label(groups),
                "updated": updated,
            }
        )
        log("已生成《%s》%d 个页面 → %s/" % (book["title"], len(entries) + 1, OUT_DIR_NAME))

    write_text(docs / ("%s.html" % INDEX_SLUG), render_index_page(books_info, shell, args.site))
    log("已生成读书心得首页 → %s.html（%d 本书）" % (INDEX_SLUG, len(books_info)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
