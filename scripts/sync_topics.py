#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/topics.json 里的主题板块生成成静态页（/btc.html、/waiwen.html …）。

四个板块：BTC / 外文 / 薄肌 / 自媒体。它们是**只读聚合页** —— 不改文章、不改
标签、不动 postListJson，一篇文章可以同时出现在多个板块里；顶栏入口由
config.json 的 exlink 提供（见文件末尾说明）。

## 为什么是「清单驱动」而不是自动分类

516 篇文章里 462 篇是「旧文归档」，全部标签只有 14 种，和这四个主题基本对不上
（只有 1 篇「币圈」）；关键词匹配噪声极大 —— 外文会被《三体》译文里的「德语」
命中，薄肌会被「力量」「有氧」这类泛词命中。所以归属只认 data/topics.json：

    labels   Issue 标签命中（外文板块靠这个一次吃下 26+23 篇生词表）
    numbers  issue 编号，给老文章用
    exclude  从本板块剔除

三者取并集，exclude 最后生效。没有任何概率判断。

## 为什么顶栏要在 exlink 里

Gmeek 的顶栏按钮顺序是：搜索 → `exlink` 各项 → `singeListJson` 各项 → RSS →
主题切换（见上游 templates/plist.html）。也就是说，**排在最前面只能靠 exlink
的书写顺序**，写进 exlink 最前面的项就紧跟在搜索图标后面。这批板块页不是
Issue 单页，所以不进 singlePage —— 那个字段决定「哪个标签的 Issue 变成整页」，
动它会改变文章归类。

## 用法

    python scripts/sync_topics.py docs                       # 正常构建
    python scripts/sync_topics.py docs --out /tmp/topics-test  # 只生成到临时目录
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_curated import build_head, load_shell, read_text, strip_seo_tags, write_text  # noqa: E402
from sync_gallery import clean_nav  # noqa: E402

DEFAULT_SITE = "https://russianqin.github.io"
EXCERPT_LIMIT = 90


def esc(value, quote=False):
    return html.escape(str(value or ""), quote=quote)


# ---------------------------------------------------------------- 归类

def plain(text, limit=EXCERPT_LIMIT):
    """把 markdown 摘要压成一行纯文本（摘要里常带 > 引用、链接、粗体）。"""
    text = text or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`>#|]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def collect(posts, topic):
    """返回 (按时间倒序的 [(编号, 条目)], 没匹配到的 numbers)。"""
    labels = set(str(x) for x in (topic.get("labels") or []))
    numbers = set(int(x) for x in (topic.get("numbers") or []))
    exclude = set(int(x) for x in (topic.get("exclude") or []))

    picked = {}
    for key, entry in posts.items():
        if not (key[:1] == "P" and key[1:].isdigit()):
            continue
        number = int(key[1:])
        if number in exclude:
            continue
        if number in numbers or labels.intersection(entry.get("labels") or []):
            picked[number] = entry

    ordered = sorted(picked.items(), key=lambda kv: kv[1].get("createdAt") or 0, reverse=True)
    return ordered, sorted(n for n in numbers if n not in picked)


# ---------------------------------------------------------------- 正文

def render_item(entry):
    url = "/" + (entry.get("postUrl") or "").lstrip("/")
    title = esc(entry.get("postTitle") or "(无标题)")
    date = esc(entry.get("createdDate") or "")
    tags = entry.get("labels") or []
    tag = '<span class="topic-tag">%s</span>' % esc(tags[0]) if tags else ""
    excerpt = plain(entry.get("description"))
    excerpt_html = '<p class="topic-excerpt">%s</p>\n' % esc(excerpt) if excerpt else ""
    return (
        '<li class="topic-item">\n'
        '<a class="topic-link" href="%s">%s</a>\n'
        '<div class="topic-meta"><span class="topic-date">%s</span>%s'
        '<span class="topic-words">%s 字</span></div>\n'
        "%s"
        "</li>\n"
        % (esc(url, quote=True), title, date, tag, esc(entry.get("wordCount") or "—"), excerpt_html)
    )


def render_body(topic, items):
    title = esc(topic.get("title") or topic.get("slug"))
    desc = esc(topic.get("desc") or "")

    parts = ['<div class="markdown-body" id="postBody">\n']
    parts.append('<h2 class="topic-heading">%s</h2>\n' % title)
    if desc:
        parts.append('<p class="topic-intro">%s</p>\n' % desc)

    if not items:
        parts.append(
            '<div class="topic-empty">这个话题还没开写。<br>'
            "想先看别的，回<a href=\"/\">首页</a>。</div>\n"
        )
    else:
        parts.append('<div class="topic-count">共 %d 篇</div>\n' % len(items))
        parts.append('<ul class="topic-list">\n')
        parts.extend(render_item(entry) for _number, entry in items)
        parts.append("</ul>\n")

    parts.append("</div>\n")
    return "".join(parts)


# ---------------------------------------------------------------- 页面骨架

def load_shell_from(docs, name="index.html"):
    """借 docs/index.html 的头/导航/页脚。

    不用 sync_curated.load_shell()：那个优先借文章页骨架，顶栏只有「首页 +
    主题切换」；板块页需要完整顶栏（四个入口互相跳）。
    """
    path = docs / name
    if not path.exists():
        return load_shell(docs)
    text = read_text(path)
    body_pos = text.find("<body>")
    content_pos = text.find('<div id="content">')
    footer_pos = text.find('<div id="footer">')
    if min(body_pos, content_pos, footer_pos) < 0:
        return load_shell(docs)

    head = text[:body_pos]
    head = re.sub(r"<!-- optimized:seo -->.*?(?=</head>)", "", head, flags=re.S)
    head = strip_seo_tags(head)
    return {
        "head": head,
        "header": text[body_pos + len("<body>"):content_pos],
        "footer": text[footer_pos:],
    }


def render_page(shell, title, description, canonical, body):
    parts = [build_head(shell["head"], title, description, canonical), "<body>\n"]
    parts.append(shell["header"])
    parts.append('<div id="content">\n')
    parts.append(body)
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="生成四个主题板块页")
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--topics", default="data/topics.json", help="归类清单")
    parser.add_argument("--base", default="blogBase.json", help="Gmeek 生成的文章索引")
    parser.add_argument("--site", default=DEFAULT_SITE, help="站点根地址（写 canonical 用）")
    parser.add_argument("--out", help="输出目录（默认写进 docs/；给临时目录时不动 docs）")
    args = parser.parse_args()

    docs = Path(args.docs)
    out_dir = Path(args.out) if args.out else docs
    out_dir.mkdir(parents=True, exist_ok=True)

    topics_path = Path(args.topics)
    if not topics_path.exists():
        print("[topics] 找不到 %s，跳过（不影响构建）" % topics_path)
        return 0
    topics = json.loads(read_text(topics_path)).get("topics") or []

    base_path = Path(args.base)
    if not base_path.exists():
        print("[topics] 找不到 %s，跳过（不影响构建）" % base_path)
        return 0
    base = json.loads(read_text(base_path))
    posts = dict(base.get("postListJson") or {})
    posts.update(base.get("singeListJson") or {})

    shell = load_shell_from(docs)
    if not shell:
        print("[topics] 借不到页面骨架，跳过（不影响构建）")
        return 0

    written = 0
    for topic in topics:
        slug = (topic.get("slug") or "").strip()
        if not slug:
            continue
        items, orphans = collect(posts, topic)
        if orphans:
            print("[topics] %s：numbers 里这些编号不存在或已关闭，已忽略 %s"
                  % (slug, orphans))

        title = topic.get("title") or slug
        desc = topic.get("desc") or title
        canonical = "%s/%s.html" % (args.site.rstrip("/"), slug)
        page = render_page(shell, "%s - 五环魔法师" % title, desc, canonical, render_body(topic, items))
        write_text(out_dir / ("%s.html" % slug), page)
        written += 1
        print("[topics] %-8s %2d 篇 → %s" % (slug, len(items), out_dir / ("%s.html" % slug)))

    if written and out_dir.resolve() == docs.resolve():
        # exlink 渲染出来的站内链接固定带 target="_blank"，去掉（幂等）
        for topic in topics:
            slug = (topic.get("slug") or "").strip()
            if slug:
                clean_nav(docs, slug)

    print("[topics] 完成：%d 个板块" % written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
