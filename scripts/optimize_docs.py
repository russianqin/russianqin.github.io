#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gmeek 静态站点构建后处理脚本（SEO / 性能 / 可访问性 / sitemap 自动化）。

在 Gmeek.py 生成 docs/ 之后运行，对生成出来的 HTML 统一做增强，
这样以后每发一篇新文章都会自动带上这些优化，不需要手工维护。

做的事：
  1. SEO      canonical、og:site_name、twitter card、article:published_time/tag、
              BlogPosting / Blog JSON-LD、<title> 追加站点名、rel=prev/next
  2. 分享卡片  文章 og:image 取正文第一张图（此前所有页面都复用头像）
  3. 加载性能  正文图片 loading=lazy / decoding=async、首图 fetchpriority=high、
              preconnect 提示、把 Gmeek 放在 </head> 之后的 <style> 移回 head
  4. 可访问性  正文图片 alt 取紧随其后的图注（原先一律是 "Image"）
  5. 自动化    依据 docs/ 里的实际文件重新生成 sitemap.xml，并生成 404.html
  6. 静态资源  把 static/ 下的自定义资源同步到 docs/（与 Gmeek 构建行为一致）

用法：
    python scripts/optimize_docs.py [docs_dir]
"""

from __future__ import print_function

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote

DEFAULT_SITE = "https://russianqin.github.io"
STATIC_ASSETS = ("custom.css", "custom.js", "robots.txt")
SEO_MARK = "<!-- optimized:seo -->"  # 幂等标记：重复运行不会重复插入

# 正文之后的模板内容（用来切出 #postBody 的范围）
POST_BODY_START = re.compile(r'<div class="markdown-body" id="postBody">', re.I)
POST_BODY_STOP = re.compile(r'<div style="font-size:small', re.I)

# 标签体不能简单写 [^>]*：属性值本身可能含 ">"（Gmeek 生成的 description 常以 ">" 开头），
# 那样匹配到的标签是残缺的，set_meta 会改不动 content，页面就留下脏 description。
TAG_BODY = r"""(?:[^>"']|"[^"]*"|'[^']*')*?"""
IMG_RE = re.compile(r"<img\b" + TAG_BODY + r">", re.I)
STYLE_RE = re.compile(r"<style\b" + TAG_BODY + r">.*?</style>", re.I | re.S)
META_RE = re.compile(r"<meta\b" + TAG_BODY + r">", re.I | re.S)

STEP_TITLE = re.compile(r"^\s*(?:\u2191|\u2b06|\^|\uff1e|>)\s*")


def log(msg):
    print("[optimize] %s" % msg)


def read_source(path):
    """读取文件，返回 (文本, 是否原本是 CRLF)。"""
    raw = Path(path).read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    return text, crlf


def write_source(path, text, crlf):
    out = text.replace("\n", "\r\n") if crlf else text
    Path(path).write_bytes(out.encode("utf-8"))


def write_generated(path, text):
    """写生成文件（统一 LF，兼容 Python 3.8）。"""
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 构建脚本，缺文件不应中断
        log("读取 %s 失败：%s" % (path, exc))
        return {}


def esc_attr(value):
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def esc_xml(value):
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def strip_tags(value):
    text = re.sub(r"<br\s*/?>", " ", value or "", flags=re.I)
    text = re.sub(r"<" + TAG_BODY + r">", "", text)
    return re.sub(r"\s+", " ", text).strip()


def iso_datetime(date_str, tz):
    """把 2025-06-08 变成 2025-06-08T00:00:00+03:00（og 要求 ISO 8601 时间）。"""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        return "%sT00:00:00%s" % (date_str, tz)
    return date_str


def clean_text(value, limit=160):
    """把 Gmeek 塞进 description 的 Markdown 原文清成一行纯文本。"""
    text = value or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # 链接只留文字
    text = re.sub(r"<" + TAG_BODY + r">", "", text)
    text = re.sub(r"^[ \t]*[#>*+\-]+\s*", "", text, flags=re.M)  # 行首标记
    text = re.sub(r"`+|~+|\*\*?", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def timezone_offset(value):
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = 0
    sign = "-" if hours < 0 else "+"
    hours = abs(hours)
    return "%s%02d:00" % (sign, hours)


def jsonld(obj):
    return (
        '<script type="application/ld+json">'
        + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        + "</script>"
    )


def meta_content(text, key, value):
    """读取 <meta name/property=value content=...> 的 content。"""
    for tag in META_RE.findall(text):
        if re.search(r'\b%s=["\']%s["\']' % (key, re.escape(value)), tag, re.I):
            found = re.search(r'\bcontent=["\'](.*?)["\']', tag, re.I | re.S)
            if found:
                return found.group(1)
    return None


def set_meta(text, key, value, content):
    """存在则改写 content，不存在则在 </head> 前补一条。"""
    attr = esc_attr(content)
    hit = [False]

    def repl(match):
        tag = match.group(0)
        if not re.search(r'\b%s=["\']%s["\']' % (key, re.escape(value)), tag, re.I):
            return tag
        hit[0] = True
        if re.search(r'\bcontent=["\']', tag, re.I):
            return re.sub(
                r'(\bcontent=["\'])[^"\']*(["\'])',
                lambda m: m.group(1) + attr + m.group(2),
                tag,
                count=1,
                flags=re.I,
            )
        return tag[:-1].rstrip() + ' content="%s">' % attr

    text = META_RE.sub(repl, text)
    if not hit[0]:
        snippet = '<meta %s="%s" content="%s">' % (key, esc_attr(value), attr)
        text = insert_into_head(text, snippet)
    return text


def insert_into_head(text, snippet):
    idx = text.lower().find("</head>")
    if idx < 0:
        return text
    return text[:idx] + snippet + text[idx:]


def title_with_site(text, site_name):
    """给 <title> 追加站点名（已存在则不重复）。"""
    match = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if not match:
        return text
    title = match.group(1).strip()
    if site_name in title:
        return text
    new_title = "%s - %s" % (title, site_name)
    return text[: match.start()] + "<title>%s</title>" % esc_attr(new_title) + text[match.end():]


def inject_attrs(tag, attrs):
    attrs = [a for a in attrs if a]
    if not attrs:
        return tag
    add = "".join(" " + a for a in attrs)
    if tag.endswith("/>"):
        return tag[:-2].rstrip() + add + ">"
    return tag[:-1].rstrip() + add + ">"


def enhance_images(region):
    """正文图片：懒加载 + 用图注补 alt（图注形如紧跟图片的 blockquote）。"""
    pieces = []
    last = 0
    for index, match in enumerate(IMG_RE.finditer(region)):
        tag = match.group(0)
        after = region[match.end(): match.end() + 400]
        caption = re.search(r"</p>\s*<blockquote>\s*<p>(.*?)</p>", after, re.I | re.S)
        if caption and re.search(r'\balt=["\']Image["\']', tag, re.I):
            text = STEP_TITLE.sub("", strip_tags(caption.group(1))).strip()
            if text:
                tag = re.sub(
                    r'alt=["\'][^"\']*["\']',
                    lambda m: 'alt="%s"' % esc_attr(text),
                    tag,
                    count=1,
                    flags=re.I,
                )
        attrs = []
        if index == 0:
            # 首图通常是文章封面的候选 LCP，保持立即加载
            if "fetchpriority=" not in tag.lower():
                attrs.append('fetchpriority="high"')
        elif "loading=" not in tag.lower():
            attrs.append('loading="lazy"')
        if "decoding=" not in tag.lower():
            attrs.append('decoding="async"')
        pieces.append(region[last:match.start()])
        pieces.append(inject_attrs(tag, attrs))
        last = match.end()
    pieces.append(region[last:])
    return "".join(pieces)


def enhance_body_images(text):
    """对页面里的 #postBody 区域做图片增强（文章页和单页都适用）。"""
    start = POST_BODY_START.search(text)
    if not start:
        return text
    stop = POST_BODY_STOP.search(text, start.end())
    if not stop:
        return text
    region = text[start.end(): stop.start()]
    return text[: start.end()] + enhance_images(region) + text[stop.start():]


def move_stray_styles(text):
    """Gmeek 把若干 <style> 放在 </head> 之后，这里搬回 head 里（保证 custom.css 仍然最后生效）。"""
    head_end = text.lower().find("</head>")
    body_start = text.lower().find("<body>")
    if head_end < 0 or body_start < 0 or body_start <= head_end:
        return text
    between = text[head_end + len("</head>"): body_start]
    blocks = STYLE_RE.findall(between)
    if not blocks:
        return text

    between_clean = STYLE_RE.sub("", between)
    head = text[:head_end]
    anchor = re.search(r"<link\b" + TAG_BODY + r"primer\.css" + TAG_BODY + r">", head, re.I)
    if anchor:
        head = head[: anchor.end()] + "".join(blocks) + head[anchor.end():]
    else:
        head = head + "".join(blocks)
    return head + "</head>" + between_clean + text[body_start:]


def posts_index(post_list):
    """把 postList.json 变成 {解码后的文件名: 元信息}，并给出按时间排序的列表。"""
    by_file = {}
    ordered = []
    for key, value in post_list.items():
        if not re.match(r"^P\d+$", key) or not isinstance(value, dict):
            continue
        post_url = (value.get("postUrl") or "").strip()
        if not post_url:
            continue
        item = {
            "num": int(key[1:]),
            "file": unquote(post_url.split("/")[-1]),
            "url": post_url,
            "title": value.get("postTitle") or "",
            "labels": value.get("labels") or [],
            "date": value.get("createdDate") or "",
        }
        by_file[item["file"]] = item
        ordered.append(item)
    ordered.sort(key=lambda item: item["num"])
    return by_file, ordered


def preconnect_hints():
    return (
        '<link rel="preconnect" href="https://avatars.githubusercontent.com" crossorigin>'
        '<link rel="dns-prefetch" href="https://avatars.githubusercontent.com">'
        '<link rel="dns-prefetch" href="https://github.com">'
        '<link rel="dns-prefetch" href="https://mirrors.sustech.edu.cn">'
    )


def detect_site(docs, config):
    """站点根地址：优先 og:url / config.siteUrl，最后用默认值。"""
    site = (config.get("siteUrl") or "").strip()
    if site:
        return site.rstrip("/")
    index = docs / "index.html"
    if index.exists():
        text, _ = read_source(index)
        found = meta_content(text, "property", "og:url")
        if found:
            return found.rstrip("/")
    return DEFAULT_SITE


def process_post(path, meta, ctx):
    """文章页：SEO + 结构化数据 + 图片增强。"""
    text, crlf = read_source(path)
    site = ctx["site"]
    canonical = "%s/%s" % (site, meta["url"])
    description = clean_text(meta_content(text, "name", "description")) or ctx["subtitle"]

    start_match = POST_BODY_START.search(text)
    stop_match = POST_BODY_STOP.search(text, start_match.end()) if start_match else None
    body_start = start_match.end() if start_match else -1
    body_stop = stop_match.start() if stop_match else -1

    og_image = ctx["avatar"]
    if body_start > 0 and body_stop > body_start:
        region = text[body_start:body_stop]
        first_img = IMG_RE.search(region)
        if first_img:
            src = re.search(r'\bsrc=["\']([^"\']+)["\']', first_img.group(0), re.I)
            if src:
                og_image = src.group(1)
        region = enhance_images(region)
        text = text[:body_start] + region + text[body_stop:]

    text = set_meta(text, "property", "og:image", og_image)
    text = set_meta(text, "property", "og:site_name", ctx["title"])
    text = set_meta(text, "name", "description", description)
    text = set_meta(text, "property", "og:description", description)
    text = set_meta(text, "name", "twitter:card", "summary_large_image")
    text = set_meta(text, "name", "twitter:title", meta["title"])
    text = set_meta(text, "name", "twitter:description", description)
    text = set_meta(text, "name", "twitter:image", og_image)
    text = set_meta(text, "name", "author", ctx["title"])
    if meta["date"]:
        text = set_meta(text, "property", "article:published_time", iso_datetime(meta["date"], ctx["tz"]))
    text = title_with_site(text, ctx["title"])

    extras = [preconnect_hints(), '<link rel="canonical" href="%s">' % esc_attr(canonical)]
    extras.append(jsonld(build_post_jsonld(meta, description, og_image, ctx)))

    index = ctx["order_index"].get(meta["num"])
    if index is not None:
        ordered = ctx["ordered"]
        if index > 0:
            extras.append(
                '<link rel="prev" href="%s/%s">' % (site, ordered[index - 1]["url"])
            )
        if index < len(ordered) - 1:
            extras.append(
                '<link rel="next" href="%s/%s">' % (site, ordered[index + 1]["url"])
            )
    extras.append("".join('<meta property="article:tag" content="%s">' % esc_attr(lb) for lb in meta["labels"]))

    if SEO_MARK not in text:
        text = insert_into_head(text, SEO_MARK + "".join(extras))
    text = move_stray_styles(text)
    write_source(path, text, crlf)


def build_post_jsonld(meta, description, image, ctx):
    data = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": meta["title"],
        "url": "%s/%s" % (ctx["site"], meta["url"]),
        "mainEntityOfPage": {"@type": "WebPage", "@id": "%s/%s" % (ctx["site"], meta["url"])},
        "inLanguage": "zh-CN",
        "author": {"@type": "Person", "name": ctx["title"], "url": ctx["site"] + "/"},
        "publisher": {
            "@type": "Organization",
            "name": ctx["title"],
            "logo": {"@type": "ImageObject", "url": ctx["avatar"]},
        },
        "isPartOf": {"@type": "Blog", "name": ctx["title"], "url": ctx["site"] + "/"},
    }
    if description:
        data["description"] = description
    if meta["date"]:
        data["datePublished"] = meta["date"]
        data["dateModified"] = meta["date"]
    if image:
        data["image"] = [image]
    if meta["labels"]:
        data["keywords"] = ",".join(meta["labels"])
    return data


def process_page(path, ctx):
    """列表页 / 单页：SEO + 结构化数据。"""
    name = path.name
    text, crlf = read_source(path)
    site = ctx["site"]
    canonical = "%s/" % site if name == "index.html" else "%s/%s" % (site, quote(name))
    description = clean_text(meta_content(text, "name", "description")) or ctx["subtitle"]

    text = set_meta(text, "property", "og:site_name", ctx["title"])
    text = set_meta(text, "name", "description", description)
    text = set_meta(text, "property", "og:description", description)
    text = set_meta(text, "name", "twitter:card", "summary")
    text = set_meta(text, "name", "twitter:title", ctx["title"])
    text = set_meta(text, "name", "twitter:description", description)
    text = title_with_site(text, ctx["title"])
    text = enhance_body_images(text)

    if name == "index.html":
        data = {
            "@context": "https://schema.org",
            "@type": "Blog",
            "name": ctx["title"],
            "description": description,
            "url": canonical,
            "inLanguage": "zh-CN",
            "image": ctx["avatar"],
            "author": {"@type": "Person", "name": ctx["title"]},
        }
    else:
        data = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": re.sub(r"<[^>]+>", "", re.search(r"<title>(.*?)</title>", text, re.I | re.S).group(1)).strip()
            if re.search(r"<title>(.*?)</title>", text, re.I | re.S)
            else ctx["title"],
            "url": canonical,
            "inLanguage": "zh-CN",
            "isPartOf": {"@type": "Blog", "name": ctx["title"], "url": site + "/"},
        }

    extras = [preconnect_hints(), '<link rel="canonical" href="%s">' % esc_attr(canonical), jsonld(data)]
    if SEO_MARK not in text:
        text = insert_into_head(text, SEO_MARK + "".join(extras))
    text = move_stray_styles(text)
    write_source(path, text, crlf)


def build_sitemap(docs, ctx):
    entries = []

    def add(loc, lastmod=None, changefreq="monthly", priority="0.8"):
        item = ["  <url>", "    <loc>%s</loc>" % esc_xml(loc)]
        if lastmod:
            item.append("    <lastmod>%s</lastmod>" % esc_xml(lastmod))
        item.append("    <changefreq>%s</changefreq>" % changefreq)
        item.append("    <priority>%s</priority>" % priority)
        item.append("  </url>")
        entries.append("\n".join(item))

    add(ctx["site"] + "/", changefreq="daily", priority="1.0")
    for page in sorted(docs.glob("*.html")):
        if page.name in ("index.html", "404.html"):
            continue
        add("%s/%s" % (ctx["site"], quote(page.name)), changefreq="weekly", priority="0.5")
    curated_dir = docs / "curated"
    if curated_dir.is_dir():
        for page in sorted(curated_dir.glob("*.html")):
            add("%s/curated/%s" % (ctx["site"], quote(page.name)), changefreq="monthly", priority="0.4")
    for item in ctx["ordered"]:
        add("%s/%s" % (ctx["site"], item["url"]), lastmod=item["date"], priority="0.8")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    write_generated(docs / "sitemap.xml", xml)
    return len(entries)


def build_404(docs, ctx):
    html = """<!DOCTYPE html>
<html data-color-mode="light" lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="robots" content="noindex">
<link href="https://mirrors.sustech.edu.cn/cdnjs/ajax/libs/Primer/21.0.7/primer.css" rel="stylesheet">
<link rel="stylesheet" href="/custom.css">
<link rel="icon" href="%s">
<title>页面不存在 - %s</title>
</head>
<body>
<div id="header"><div class="title-left">
<img src="%s" class="avatar circle" alt="avatar"><a class="blogTitle">%s</a>
</div></div>
<div id="content">
<h1 class="postTitle">404</h1>
<p>这个页面不存在，可能已经被移动或者删除了。</p>
<p><a href="/">← 回到首页</a>　<a href="/tag.html">按标签/关键词浏览全部文章</a></p>
</div>
<div id="footer"><div id="footer1">Copyright © <span id="copyrightYear"></span> <a href="/">%s</a></div></div>
<script>document.getElementById("copyrightYear").innerHTML=new Date().getFullYear();</script>
</body>
</html>
""" % (
        esc_attr(ctx["avatar"]),
        esc_attr(ctx["title"]),
        esc_attr(ctx["avatar"]),
        esc_attr(ctx["title"]),
        esc_attr(ctx["title"]),
    )
    write_generated(docs / "404.html", html)


def sync_static(root, docs):
    count = 0
    static_dir = root / "static"
    if not static_dir.is_dir():
        return count
    for src in sorted(static_dir.iterdir()):
        if not src.is_file() or src.name in ("sitemap.xml", "index.html"):
            continue
        target = docs / src.name
        if src.name in STATIC_ASSETS or not target.exists():
            target.write_bytes(src.read_bytes())
            count += 1
    return count


def main():
    root = Path(__file__).resolve().parent.parent
    docs = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "docs"
    if not docs.is_dir():
        log("找不到 docs 目录：%s" % docs)
        return 1

    config = load_json(root / "config.json")
    post_list = load_json(docs / "postList.json")
    by_file, ordered = posts_index(post_list)
    site = detect_site(docs, config)

    ctx = {
        "site": site,
        "title": config.get("title") or "Blog",
        "subtitle": config.get("subTitle") or "",
        "avatar": config.get("avatarUrl") or "",
        "tz": timezone_offset(config.get("UTC", 0)),
        "ordered": ordered,
        "order_index": {item["num"]: i for i, item in enumerate(ordered)},
    }

    pages = 0
    for page in sorted(docs.glob("*.html")):
        if page.name == "404.html":
            continue
        try:
            process_page(page, ctx)
            pages += 1
        except Exception as exc:  # noqa: BLE001 - 单页失败不影响整体构建
            log("处理 %s 失败：%s" % (page.name, exc))

    posts = 0
    post_dir = docs / "post"
    for page in sorted(post_dir.glob("*.html")) if post_dir.is_dir() else []:
        meta = by_file.get(page.name)
        if not meta:
            log("postList.json 里没有 %s，跳过" % page.name)
            continue
        try:
            process_post(page, meta, ctx)
            posts += 1
        except Exception as exc:  # noqa: BLE001
            log("处理文章 %s 失败：%s" % (page.name, exc))

    total = build_sitemap(docs, ctx)
    build_404(docs, ctx)
    synced = sync_static(root, docs)
    log(
        "完成：%d 个页面、%d 篇文章，sitemap %d 条，同步静态资源 %d 个（站点 %s）"
        % (pages, posts, total, synced, site)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
