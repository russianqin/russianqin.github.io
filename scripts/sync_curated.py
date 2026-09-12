#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 WenZhangShouCang 的收藏文章同步成博客的「文章收藏」页面。

生成内容：
  * docs/curated.html           —— 列表页（全部收藏，带站内搜索）
  * docs/curated/<编号>.html     —— 每篇一个页面（存档正文 + 来源标注 + 看原文评论）
  * 首页导航里的「Curated Articles」指向站内 /curated.html

增删自动同步：每次构建都按源仓库当前的文件列表重新生成列表页与文章页，
新增的收藏自动出现，删掉的收藏自动消失。

图片：
  --images download 时把正文图片下载到 data/curated-images/（内容哈希命名），
  并在页面里改成本地路径；下载失败的那张保留外链，不会让整体失败。

用法：
    python scripts/sync_curated.py docs                          # 联网浅克隆源仓库
    python scripts/sync_curated.py docs --source D:\\WenZhangShouCang --images hotlink
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE_REPO = "https://github.com/russianqin/WenZhangShouCang.git"
INDEX_SLUG = "curated"
IMAGE_DIR_NAME = "curated-images"
USER_AGENT = "russianqin-blog-curated/1.0 (+https://russianqin.github.io)"

ASSET_HOSTS = (
    "zhimg.com", "qpic.cn", "qlogo.cn", "wx.qq.com",
    "chuapp.com/wp-content", "mpres", "equation",
)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

TITLE_LINK_RE = re.compile(r"^#{0,6}\s*\[(?P<title>.+?)\]\((?P<url>https?://[^)]+)\)\s*$")
TITLE_PLAIN_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
NUM_PREFIX_RE = re.compile(r"^(\d{1,4})[.\-_\s]")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<url>https?://[^\"']+)[\"']", re.I)
ANY_URL_RE = re.compile(r"https?://[^\s\)\]\">]+")
COMMENT_MARKER_RE = re.compile(r"^\*{0,2}\s*(精选留言|全部留言|留言)\s*\*{0,2}$")
META_LINE_RE = re.compile(r"^(阅读\s*[\d.]+万?|文章已于.*修改|发布于.*|编辑于.*)$")
AVATAR_ITEM_RE = re.compile(r"^-\s*!\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)\s*$")
AVATAR_PLAIN_RE = re.compile(r"^!\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)\s*$")
LIKES_RE = re.compile(r"^赞\s*(\d+)$")

IMAGE_REFERER = (
    ("qpic.cn", "https://mp.weixin.qq.com/"),
    ("qlogo.cn", "https://mp.weixin.qq.com/"),
    ("zhimg.com", "https://www.zhihu.com/"),
    ("chuapp.com", "https://www.chuapp.com/"),
    ("weibo.com", "https://weibo.com/"),
)


def log(msg):
    print("[curated] %s" % msg)


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def is_image_url(url):
    return url.lower().split("?")[0].endswith(IMAGE_EXT)


def host_label(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "zhihu.com" in host:
        return "知乎"
    if "weixin.qq.com" in host:
        return "微信公众号"
    if "weibo.com" in host:
        return "微博"
    if "chuapp.com" in host:
        return "触乐"
    if "jianshu.com" in host:
        return "简书"
    if "douban.com" in host:
        return "豆瓣"
    return host


# ---------------------------------------------------------------- 数据源

def fetch_source(local_dir):
    """返回 (仓库目录, 是否为临时目录)。默认浅克隆源仓库。"""
    if local_dir:
        path = Path(local_dir).resolve()
        if not path.is_dir():
            raise SystemExit("找不到本地源目录：%s" % path)
        log("使用本地源目录：%s" % path)
        return path, False
    tmp = Path(tempfile.mkdtemp(prefix="wzsc-"))
    log("浅克隆 %s ..." % SOURCE_REPO)
    subprocess.run(
        ["git", "clone", "--depth", "1", SOURCE_REPO, str(tmp / "repo")],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    return tmp / "repo", True


def collect_articles(repo_dir):
    articles = []
    for path in sorted(repo_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if path.name.lower() == "readme.md":
            continue
        article = parse_article(path)
        if article:
            articles.append(article)
    articles.sort(key=lambda item: item["order"])
    # 源仓库里存在重复编号（例如两篇都叫 270.*），给后来的加后缀，避免互相覆盖
    seen = {}
    for article in articles:
        slug = article["slug"]
        if slug in seen:
            seen[slug] += 1
            article["slug"] = "%s-%d" % (slug, seen[slug])
        else:
            seen[slug] = 1
    return articles


def parse_article(path):
    text = read_text(path).replace("\r\n", "\n")
    lines = text.split("\n")
    head_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if head_index is None:
        return None
    head = lines[head_index].strip()
    body_lines = lines[head_index + 1:]
    title, url = None, None

    match = TITLE_LINK_RE.match(head)
    if match:
        title = match.group("title").strip()
        url = match.group("url").strip()
    else:
        match = TITLE_PLAIN_RE.match(head)
        if match:
            title = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", match.group("title")).strip()
            link = re.search(r"\((https?://[^)]+)\)", head)
            if link:
                url = link.group(1).strip()
        else:
            body_lines = lines[head_index:]

    stem = path.stem
    if not title:
        title = NUM_PREFIX_RE.sub("", stem).strip() or stem
        body_lines = lines[head_index:]

    number = NUM_PREFIX_RE.match(stem)
    order = int(number.group(1)) if number else 10 ** 6
    slug = number.group(1).zfill(3) if number else hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]

    body = "\n".join(body_lines).strip("\n")
    if not url:
        for candidate in ANY_URL_RE.findall(body):
            if is_image_url(candidate):
                continue
            if any(part in candidate for part in ASSET_HOSTS):
                continue
            url = candidate
            break

    body, comments, meta = split_comments(body)

    return {
        "slug": slug,
        "order": order,
        "title": title,
        "url": url,
        "host": host_label(url) if url else "来源缺失",
        "body": body,
        "comments": comments,
        "meta": meta,
        "file": path.name,
    }


def split_comments(body):
    """把「精选留言」段落从正文里切出来，并解析成结构化的留言记录。"""
    lines = body.split("\n")
    marker = next((i for i, line in enumerate(lines) if COMMENT_MARKER_RE.match(line.strip())), None)
    if marker is None:
        return body, [], []

    head = lines[:marker]
    tail = lines[marker + 1:]
    meta = []
    while head and not head[-1].strip():
        head.pop()
    while head and META_LINE_RE.match(head[-1].strip()):
        meta.insert(0, head.pop().strip())
        while head and not head[-1].strip():
            head.pop()
    return "\n".join(head).strip("\n"), parse_comments(tail), meta


def parse_comments(lines):
    """留言区格式：`- ![头像](url)` 起一条，随后是 昵称 / (作者) / 赞N / 正文；
    缩进的 `![头像](url)` 表示这条下面还有一条作者回复。"""
    records = []
    current = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if current is not None and current["text_lines"]:
                current["text_lines"].append("")
            continue

        item = AVATAR_ITEM_RE.match(stripped)
        if item:
            current = {
                "avatar": item.group("url"),
                "nick": "",
                "author": False,
                "likes": "",
                "text_lines": [],
                "replies": [],
            }
            records.append(current)
            continue

        reply = AVATAR_PLAIN_RE.match(stripped)
        if reply and current is not None:
            current = {
                "avatar": reply.group("url"),
                "nick": "",
                "author": False,
                "likes": "",
                "text_lines": [],
            }
            records[-1]["replies"].append(current)
            continue

        if current is None:
            continue
        if stripped == "(作者)":
            current["author"] = True
            continue
        likes = LIKES_RE.match(stripped)
        if likes and not current["likes"]:
            current["likes"] = likes.group(1)
            continue
        if not current["nick"] and not current["likes"] and not current["text_lines"]:
            current["nick"] = stripped
            continue
        current["text_lines"].append(stripped)

    for record in records:
        clean_comment(record)
        for reply in record["replies"]:
            clean_comment(reply)
    return [record for record in records if record["nick"] or record["text"]]


def clean_comment(record):
    text = "\n".join(record.pop("text_lines", [])).strip()
    record["text"] = re.sub(r"\n{3,}", "\n\n", text)


# ---------------------------------------------------------------- 图片

def image_headers(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    headers = {"User-Agent": USER_AGENT}
    for key, referer in IMAGE_REFERER:
        if key in host:
            headers["Referer"] = referer
            break
    return headers


def download_image(url, cache_dir):
    """下载单张图片，返回缓存文件名；失败返回 None。"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in IMAGE_EXT:
        ext = ".jpg"
    name = digest + ext
    target = cache_dir / name
    if target.exists() and target.stat().st_size > 0:
        return name
    try:
        request = urllib.request.Request(url, headers=image_headers(url))
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read()
        if not data:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return name
    except Exception:  # noqa: BLE001
        return None


def localize_images(article, cache_dir, mode, stats):
    """把正文图片和留言头像换成本地路径（hotlink 模式保持外链）。"""
    if mode != "download":
        return

    def localize(url):
        if not url:
            return url
        if url in article["_images"]:
            name = article["_images"][url]
        else:
            name = download_image(url, cache_dir)
            article["_images"][url] = name
        if not name:
            stats["failed"] += 1
            return url
        stats["downloaded"] += 1
        article["_local_images"].add(name)
        return "/%s/%s" % (IMAGE_DIR_NAME, name)

    def replace(match):
        url = match.group("url")
        local = localize(url)
        if not article.get("_first_body_image") and local != url:
            article["_first_body_image"] = local
        return match.group(0).replace(url, local)

    article["body"] = MD_IMAGE_RE.sub(replace, article["body"])
    article["body"] = HTML_IMAGE_RE.sub(replace, article["body"])
    for record in article.get("comments", []):
        record["avatar"] = localize(record.get("avatar"))
        for reply in record.get("replies", []):
            reply["avatar"] = localize(reply.get("avatar"))
    if article.get("_first_body_image"):
        article["og_image"] = article["_first_body_image"]


# ---------------------------------------------------------------- 渲染

def markdown_to_html(text):
    try:
        import markdown  # type: ignore

        return markdown.markdown(text, extensions=["extra", "sane_lists", "nl2br", "tables"])
    except Exception:  # noqa: BLE001 - 没有 markdown 包时用内置渲染器
        return fallback_markdown(text)


def inline_html(text):
    """行内格式：图片、链接、粗体、斜体、行内代码。"""
    placeholders = []

    def stash(value):
        placeholders.append(value)
        return "\x00%d\x00" % (len(placeholders) - 1)

    text = html.escape(text, quote=False)
    text = re.sub(
        r"!\[[^\]]*\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)",
        lambda m: stash('<img src="%s" alt="" loading="lazy" decoding="async">' % m.group(1)),
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: stash('<a href="%s" rel="nofollow">%s</a>' % (m.group(2), m.group(1))),
        text,
    )
    text = re.sub(r"`([^`]+)`", lambda m: stash("<code>%s</code>" % m.group(1)), text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = text.replace("  \n", "<br>\n")
    for index, value in enumerate(placeholders):
        text = text.replace("\x00%d\x00" % index, value)
    return text


def is_table_separator(line):
    return bool(re.match(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", line)) and "-" in line


def fallback_markdown(text):
    """不依赖第三方库的 Markdown 渲染（标题/段落/图片/链接/列表/引用/表格/代码块）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        fence = re.match(r"^\s*```(.*)$", line)
        if fence:
            index += 1
            block = []
            while index < total and not re.match(r"^\s*```\s*$", lines[index]):
                block.append(html.escape(lines[index], quote=False))
                index += 1
            index += 1
            out.append("<pre><code>%s</code></pre>" % "\n".join(block))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)) + 1, 6)
            out.append("<h%d>%s</h%d>" % (level, inline_html(heading.group(2)), level))
            index += 1
            continue

        if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\S]*$", stripped) and len(set(stripped.replace(" ", ""))) == 1:
            out.append("<hr>")
            index += 1
            continue

        if stripped.startswith(">"):
            block = []
            while index < total and lines[index].strip().startswith(">"):
                block.append(lines[index].strip()[1:].strip())
                index += 1
            out.append("<blockquote><p>%s</p></blockquote>" % "<br>".join(inline_html(b) for b in block))
            continue

        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line) and not re.match(r"^\s*[-*+]\s*$", line):
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            items = []
            while index < total and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[index]):
                items.append(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[index]))
                index += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline_html(i) for i in items), tag))
            continue

        if "|" in line and index + 1 < total and is_table_separator(lines[index + 1]):
            header = [cell.strip() for cell in line.strip().strip("|").split("|")]
            index += 2
            rows = []
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            head_html = "".join("<th>%s</th>" % inline_html(cell) for cell in header)
            row_html = "".join(
                "<tr>%s</tr>" % "".join("<td>%s</td>" % inline_html(cell) for cell in row) for row in rows
            )
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head_html, row_html))
            continue

        block = [line.strip()]
        index += 1
        while index < total and lines[index].strip() and not re.match(
            r"^\s*(#{1,6}\s|```|>|[-*+]\s|\d+[.)]\s)", lines[index]
        ):
            block.append(lines[index].strip())
            index += 1
        out.append("<p>%s</p>" % "<br>\n".join(inline_html(b) for b in block))
    return "\n".join(out)


def add_lazy_loading(body_html):
    return re.sub(r"<img\b(?![^>]*\bloading=)", '<img loading="lazy" decoding="async"', body_html, flags=re.I)


def load_shell(docs):
    """从已生成的博客页面里取页头/页脚模板，保证样式、主题切换、导航一致。"""
    candidates = []
    if (docs / "post").is_dir():
        candidates += sorted((docs / "post").glob("*.html"))
    if (docs / "index.html").exists():
        candidates.append(docs / "index.html")
    if not candidates:
        return None
    text = read_text(candidates[0])
    body_pos = text.find("<body>")
    content_pos = text.find('<div id="content">')
    footer_pos = text.find('<div id="footer">')
    if min(body_pos, content_pos, footer_pos) < 0:
        return None
    head = text[:body_pos]
    head = re.sub(r"<!-- optimized:seo -->.*?(?=</head>)", "", head, flags=re.S)
    head = re.sub(r'<meta\s+name="description"[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<meta\s+property="og:[^"]*"[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<meta\s+name="twitter:[^"]*"[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<link\s+rel="canonical"[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<meta\s+name="author"[^>]*>\s*', "", head, flags=re.I)
    head = re.sub(r'<meta\s+property="article:[^"]*"[^>]*>\s*', "", head, flags=re.I)
    header = text[body_pos + len("<body>"):content_pos]
    footer = text[footer_pos:]
    return {"head": head, "header": header, "footer": footer}


def build_head(shell_head, title, description, canonical, og_image=None, article=False, source_url=None):
    head = re.sub(r"<title>.*?</title>", "<title>%s</title>" % html.escape(title), shell_head, count=1, flags=re.S)
    tags = [
        '<meta name="description" content="%s">' % html.escape(description, quote=True),
        '<link rel="canonical" href="%s">' % html.escape(canonical, quote=True),
        '<meta property="og:title" content="%s">' % html.escape(title, quote=True),
        '<meta property="og:description" content="%s">' % html.escape(description, quote=True),
        '<meta property="og:type" content="%s">' % ("article" if article else "website"),
        '<meta property="og:url" content="%s">' % html.escape(canonical, quote=True),
        '<meta name="twitter:card" content="%s">' % ("summary_large_image" if og_image else "summary"),
    ]
    if og_image:
        tags.append('<meta property="og:image" content="%s">' % html.escape(og_image, quote=True))
    if article:
        data = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": title,
            "url": canonical,
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            "inLanguage": "zh-CN",
            "description": description[:160],
            "isPartOf": {"@type": "Blog", "name": "五环魔法师", "url": canonical.split("/curated/")[0] + "/"},
        }
        if source_url:
            data["isBasedOn"] = source_url
        tags.append(
            '<script type="application/ld+json">%s</script>'
            % json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )
    insert_at = head.lower().rfind("</head>")
    return head[:insert_at] + "".join(tags) + head[insert_at:]


def clean_header(header, title):
    header = re.sub(
        r'(<h1 class="postTitle">).*?(</h1>)',
        lambda m: m.group(1) + html.escape(title) + m.group(2),
        header,
        count=1,
        flags=re.S,
    )
    return re.sub(
        r'\s*<a href="https://github\.com/[^"]*issues/[^"]*".*?</a>',
        "",
        header,
        flags=re.S,
    )


def render_comment_text(text):
    blocks = [block.strip() for block in (text or "").split("\n\n") if block.strip()]
    return "".join("<p>%s</p>" % inline_html(block.replace("\n", "<br>")) for block in blocks)


def render_comment(record, is_reply=False):
    avatar = record.get("avatar")
    avatar_html = (
        '<img class="curated-avatar" src="%s" alt="" loading="lazy" decoding="async">'
        % html.escape(avatar, quote=True)
        if avatar
        else '<span class="curated-avatar curated-avatar-empty"></span>'
    )
    head = ['<span class="curated-nick">%s</span>' % html.escape(record.get("nick") or "匿名")]
    if record.get("author"):
        head.append('<span class="curated-badge">作者</span>')
    if record.get("likes"):
        head.append('<span class="curated-likes">赞 %s</span>' % html.escape(record["likes"]))
    replies = "".join(render_comment(reply, True) for reply in record.get("replies", []))
    return (
        '<div class="curated-comment%s">%s<div class="curated-comment-main">'
        '<div class="curated-comment-head">%s</div>'
        '<div class="curated-comment-text">%s</div>%s</div></div>'
        % (
            " curated-comment-reply" if is_reply else "",
            avatar_html,
            "".join(head),
            render_comment_text(record.get("text", "")),
            replies,
        )
    )


def render_comments(records):
    if not records:
        return ""
    return (
        '<section class="curated-comments">'
        '<h2 class="curated-comments-title">精选留言<span class="curated-comments-count">%d</span></h2>%s</section>'
        % (len(records), "".join(render_comment(record) for record in records))
    )


def render_article(article, shell, site, body_html):
    base = site.rstrip("/")
    canonical = "%s/%s/%s.html" % (base, INDEX_SLUG, article["slug"])
    plain = re.sub(r"\s+", " ", re.sub(r"[#*>`\[\]!]", "", article["body"]))[:150]
    description = "%s（收藏自%s，个人存档）" % (plain, article["host"])
    head = build_head(
        shell["head"],
        "%s - 文章收藏" % article["title"],
        description,
        canonical,
        og_image=article.get("og_image"),
        article=True,
        source_url=article["url"],
    )
    parts = ["<!DOCTYPE html>\n", head, "</head>\n<body>\n"]
    parts.append(clean_header(shell["header"], article["title"]))
    parts.append('<div id="content">\n')
    parts.append('<div class="curated-source">\n')
    if article["url"]:
        parts.append(
            '<span>收藏自 <b>%s</b></span><a class="btn btn-sm" href="%s" target="_blank" '
            'rel="noopener noreferrer">看原文评论 ↗</a>'
            % (html.escape(article["host"]), html.escape(article["url"], quote=True))
        )
    else:
        parts.append('<span>收藏自 <b>%s</b>（原文链接缺失）</span>' % html.escape(article["host"]))
    parts.append('<div class="curated-note">本文为个人存档，版权归原作者所有；评论请点上方按钮前往原文查看。</div>')
    parts.append("</div>\n")
    parts.append('<div class="markdown-body" id="postBody">%s</div>\n' % body_html)
    if article.get("meta"):
        parts.append(
            '<div class="curated-article-meta">%s</div>\n'
            % html.escape(" · ".join(article["meta"]))
        )
    parts.append(render_comments(article.get("comments", [])))
    parts.append('<div class="curated-footer"><a href="/%s.html">← 返回收藏列表</a></div>\n' % INDEX_SLUG)
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


def render_index(articles, shell, site):
    canonical = "%s/%s.html" % (site.rstrip("/"), INDEX_SLUG)
    description = "文章收藏：共 %d 篇存档（知乎、微信公众号等），个人备份，可搜索。" % len(articles)
    head = build_head(shell["head"], "文章收藏", description, canonical)
    rows = []
    for article in articles:
        rows.append(
            '<li class="curated-item" data-title="%s">'
            '<a class="curated-link" href="/%s/%s.html">%s</a>'
            '<span class="curated-tag">%s</span></li>'
            % (
                html.escape((article["title"] + " " + article["host"]).lower(), quote=True),
                INDEX_SLUG,
                article["slug"],
                html.escape(article["title"]),
                html.escape(article["host"]),
            )
        )
    script = (
        "<script>(function(){var box=document.getElementById('curatedSearch');"
        "var list=document.getElementById('curatedList');var items=[].slice.call(list.children);"
        "var count=document.getElementById('curatedCount');"
        "function update(){var q=(box.value||'').trim().toLowerCase();var shown=0;"
        "items.forEach(function(li){var hit=!q||li.getAttribute('data-title').indexOf(q)>=0;"
        "li.style.display=hit?'':'none';if(hit)shown++;});"
        "count.textContent='显示 '+shown+' / '+items.length+' 篇';}"
        "box.addEventListener('input',update);update();})();</script>\n"
    )
    parts = ["<!DOCTYPE html>\n", head, "</head>\n<body>\n"]
    parts.append(clean_header(shell["header"], "文章收藏"))
    parts.append('<div id="content">\n')
    parts.append(
        '<div class="curated-toolbar"><input id="curatedSearch" type="search" class="form-control" '
        'placeholder="搜索标题或来源（共 %d 篇）" aria-label="搜索收藏">'
        '<span id="curatedCount" class="curated-count"></span></div>\n' % len(articles)
    )
    parts.append('<ul class="curated-list" id="curatedList">%s</ul>\n' % "".join(rows))
    parts.append(script)
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


def rewrite_nav(docs, target="/%s.html" % INDEX_SLUG):
    """把导航里指向 GitHub 收藏仓库的链接改成站内收藏列表。"""
    changed = 0
    pattern = re.compile(r'<a href="https://github\.com/russianqin/WenZhangShouCang"[^>]*>', re.I)

    def repl(match):
        tag = re.sub(r'\s+target="_blank"', "", match.group(0))
        return re.sub(r'href="[^"]*"', 'href="%s"' % target, tag)

    targets = list(docs.glob("*.html"))
    for sub in ("post", "curated"):
        if (docs / sub).is_dir():
            targets += list((docs / sub).glob("*.html"))
    for path in targets:
        text = read_text(path)
        if "WenZhangShouCang" not in text:
            continue
        updated = pattern.sub(repl, text)
        if updated != text:
            write_text(path, updated)
            changed += 1
    return changed


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--source", help="本地 WenZhangShouCang 目录（离线调试用）")
    parser.add_argument("--images", choices=("hotlink", "download"), default="hotlink")
    parser.add_argument("--limit", type=int, default=0, help="只生成前 N 篇（调试用）")
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

    repo_dir, temporary = fetch_source(args.source)
    try:
        articles = collect_articles(repo_dir)
        if args.limit:
            articles = articles[: args.limit]
        log("解析到收藏 %d 篇" % len(articles))

        cache_dir = root / "data" / IMAGE_DIR_NAME
        out_dir = docs / "curated"
        image_out = docs / IMAGE_DIR_NAME
        stats = {"downloaded": 0, "failed": 0}

        for article in articles:
            article["_images"] = {}
            article["_local_images"] = set()
            localize_images(article, cache_dir, args.images, stats)

        if args.images == "download":
            wanted = set()
            for article in articles:
                wanted |= article["_local_images"]
            if wanted:
                image_out.mkdir(parents=True, exist_ok=True)
                for name in sorted(wanted):
                    source = cache_dir / name
                    if source.exists():
                        target = image_out / name
                        if not target.exists() or target.stat().st_size != source.stat().st_size:
                            shutil.copyfile(str(source), str(target))
            log(
                "图片：下载 %d 张，失败 %d 张，落地文件 %d 个"
                % (stats["downloaded"], stats["failed"], len(wanted))
            )

        # 先清掉上一轮生成的页面，源仓库里删掉的收藏不会留下残页
        if out_dir.is_dir():
            for stale in out_dir.glob("*.html"):
                try:
                    stale.unlink()
                except OSError:
                    pass

        for article in articles:
            body_html = add_lazy_loading(markdown_to_html(article["body"]))
            write_text(out_dir / ("%s.html" % article["slug"]), render_article(article, shell, args.site, body_html))
        log("已生成 %d 个文章页 → %s" % (len(articles), out_dir.relative_to(root).as_posix()))

        write_text(docs / ("%s.html" % INDEX_SLUG), render_index(articles, shell, args.site))
        log("已生成列表页 → %s.html" % INDEX_SLUG)

        changed = rewrite_nav(docs)
        log("导航链接已改为站内页面：%d 个页面" % changed)
    finally:
        if temporary:
            shutil.rmtree(str(repo_dir.parent), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
