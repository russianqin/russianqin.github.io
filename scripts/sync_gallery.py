#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/gallery-images/ 里的图片同步成博客「色廊」页面（docs/gallery.html）。

为什么这样组织（与 sync_refreshment.py / sync_shilue.py 同一套约定）：
  * 图片**真源**放在仓库 data/gallery-images/，本脚本每次构建把它拷进 docs/gallery-images/；
    docs/ 是构建产物，Gmeek 每轮都会整体覆盖，手工放进去的图片活不过一次构建
    （2026-10-03 踩过：8 张看板娘图放进 docs/ 后被 auto-commit 当删除提交，线上全 404）。
  * 页面模板从**已构建好的站内单页**里取（head / header / footer），保证主题切换、页脚、
    右上角按钮与其它页面完全一致，不需要在这儿再维护一份 HTML 骨架。
  * 导航入口走 config.json 的 exlink（"色廊": "/gallery.html"）。Gmeek 渲染 exlink 时固定带
    target="_blank"，本脚本在构建后把它改回站内跳转。

用图：jpg / jpeg / png / webp / gif / avif / bmp 直接丢进 data/gallery-images/，
可以建子文件夹分类（目录结构会原样保留），按路径自然排序
（1.jpg、2.jpg、10.jpg 不会排成 1、10、2）。
想调整顺序就在文件名前面加序号。

浏览性能：几百张图不会一次性写成几百个 img 标签 —— 首屏只输出前 --first-batch 张，
其余存成页面里的 JSON，滚动到底部时按 --per-batch 一批追加。点开大图是自己的一套
灯箱（支持键盘 ← →、触摸左右滑动），不用 custom.js 那个单张灯箱。

用法：
    python scripts/sync_gallery.py docs
    python scripts/sync_gallery.py docs --title 色廊 --per-batch 60
"""

import argparse
import html
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

INDEX_SLUG = "gallery"           # 输出 docs/gallery.html
DEFAULT_TITLE = "色廊"
SRC_DIR_NAME = "gallery-images"  # 图片真源：data/gallery-images
OUT_DIR_NAME = "gallery-images"  # 输出：docs/gallery-images
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")

# 标签体不能简单写 [^>]*：属性值本身可能含 ">"，会把标签截断并留下裸文本进 head
# （2026-09 那次事故就是这么来的，check_head.py 专门盯着这件事）。
TAG_BODY = r"""(?:[^>"']|"[^"]*"|'[^']*')*?"""
META_TAG_RE = re.compile(r"<meta\b" + TAG_BODY + r">", re.I | re.S)
LINK_TAG_RE = re.compile(r"<link\b" + TAG_BODY + r">", re.I | re.S)
SEO_META_RE = re.compile(
    r"""(?:name|property)\s*=\s*["'](?:(?:og|article|twitter):[^"']*|description|author|keywords)["']""",
    re.I,
)
TITLE_H1_RE = re.compile(r'(<h1 class="postTitle">).*?(</h1>)', re.S)
ISSUE_LINK_RE = re.compile(r'\s*<a href="https://github\.com/[^"]*issues/[^"]*".*?</a>', re.S)


def log(msg):
    print("[gallery] %s" % msg)


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def esc(value, quote=False):
    return html.escape(value or "", quote=quote)


# ---------------------------------------------------------------- 图片清单

def natural_key(text):
    """自然排序：把连续数字当数字比，避免 10.jpg 排在 2.jpg 前面。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def scan_images(src_dir):
    """递归收集图片，返回 [(相对路径 posix, 绝对路径)]，按自然顺序排好。"""
    if not src_dir.is_dir():
        return []
    found = []
    for path in src_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(src_dir).parts):
            continue
        if path.suffix.lower() not in IMAGE_EXT:
            continue
        found.append((path.relative_to(src_dir).as_posix(), path))
    found.sort(key=lambda item: natural_key(item[0]))
    return found


def sync_assets(src_dir, out_dir, images):
    """图片拷进 docs/<OUT_DIR_NAME>/（尺寸相同就跳过），并清掉源里已删除的旧图。"""
    if not images:
        return 0, 0
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set()
    copied = 0
    for rel, source in images:
        target = out_dir / rel
        wanted.add(target.as_posix())
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        copied += 1

    removed = 0
    for existing in out_dir.rglob("*"):
        if not existing.is_file():
            continue
        if existing.suffix.lower() not in IMAGE_EXT:
            continue
        if existing.as_posix() not in wanted:
            try:
                existing.unlink()
                removed += 1
            except OSError:
                pass
    return copied, removed


# ---------------------------------------------------------------- 页面骨架

def strip_seo_tags(head):
    """模板 head 里的 description / og:* / canonical 属于原来那一页，全部剔掉再写自己的。"""
    head = META_TAG_RE.sub(lambda m: "" if SEO_META_RE.search(m.group(0)) else m.group(0), head)
    return LINK_TAG_RE.sub(
        lambda m: "" if re.search(r"""rel\s*=\s*["']canonical["']""", m.group(0), re.I) else m.group(0),
        head,
    )


def load_shell(docs):
    """从已构建的站内页面取 head / header / footer 三段模板。

    优先级：单页（refreshment / link）→ 任一文章页 → 首页。
    文章页也要排在首页前面：首页的 header 是「头像 + 站点名」的列表版，
    没有 h1.postTitle，拿它当模板会让画廊页顶部没有标题。
    """
    candidates = [docs / "refreshment.html", docs / "link.html"]
    post_dir = docs / "post"
    if post_dir.is_dir():
        candidates += sorted(post_dir.glob("*.html"))[:1]
    candidates += [docs / "index.html"]
    candidates += sorted(docs.glob("*.html"))
    for path in candidates:
        if not path.is_file():
            continue
        text = read_text(path)
        body_pos = text.find("<body>")
        content_pos = text.find('<div id="content">')
        footer_pos = text.find('<div id="footer">')
        if min(body_pos, content_pos, footer_pos) < 0:
            continue
        head = text[:body_pos]
        # optimize_docs.py 写的 SEO 块是上一页的，删掉
        head = re.sub(r"<!-- optimized:seo -->.*?(?=</head>)", "", head, flags=re.S)
        head = strip_seo_tags(head)
        header = text[body_pos + len("<body>"):content_pos]
        footer = text[footer_pos:]
        return {"source": path.name, "head": head, "header": header, "footer": footer}
    return None


def clean_header(header, title):
    """换掉 h1 标题，并去掉指向某个 Issue 的按钮（画廊页没有对应 Issue）。"""
    header = TITLE_H1_RE.sub(lambda m: m.group(1) + esc(title) + m.group(2), header, count=1)
    return ISSUE_LINK_RE.sub("", header)


def build_head(shell_head, title, description, canonical):
    head = re.sub(r"<title>.*?</title>", "<title>%s</title>" % esc(title), shell_head, count=1, flags=re.S)
    tags = [
        '<meta name="description" content="%s">' % esc(description, quote=True),
        '<link rel="canonical" href="%s">' % esc(canonical, quote=True),
        '<meta property="og:title" content="%s">' % esc(title, quote=True),
        '<meta property="og:description" content="%s">' % esc(description, quote=True),
        '<meta property="og:type" content="website">',
        '<meta property="og:url" content="%s">' % esc(canonical, quote=True),
        '<meta name="twitter:card" content="summary">',
    ]
    insert_at = head.lower().rfind("</head>")
    if insert_at < 0:
        return head + "".join(tags)
    return head[:insert_at] + "".join(tags) + head[insert_at:]


# ---------------------------------------------------------------- 正文

def item_url(rel):
    return "/%s/%s" % (OUT_DIR_NAME, urllib.parse.quote(rel))


def label_of(rel):
    """alt / 序号用的名字：文件名去扩展名。"""
    return Path(rel).stem


def render_item(rel):
    url = item_url(rel)
    name = label_of(rel)
    return (
        '<a class="gallery-item" href="%s" data-name="%s">'
        '<img src="%s" alt="%s" loading="lazy" decoding="async">'
        "</a>"
        % (esc(url, quote=True), esc(name, quote=True), esc(url, quote=True), esc(name, quote=True))
    )


def render_body(images, title, first_batch, per_batch):
    """首屏输出 first_batch 张，其余塞进 JSON 由页面脚本追加。"""
    total = len(images)
    if not total:
        return (
            '<div class="markdown-body" id="postBody">\n'
            '<div class="gallery-intro">%s还没有图片。把图片放进仓库的 '
            "<code>data/gallery-images/</code>，下次构建就会出现在这里。</div>\n" % esc(title)
            + "</div>\n"
        )

    head_items = [rel for rel, _path in images[:first_batch]]
    rest = [[item_url(rel), label_of(rel)] for rel, _path in images[first_batch:]]
    payload = json.dumps(rest, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    parts = ['<div class="markdown-body" id="postBody">\n']
    parts.append(
        '<div class="gallery-intro">共 <b>%d</b> 张。点任意一张看大图，键盘 <kbd>←</kbd> <kbd>→</kbd> 翻页。</div>\n'
        % total
    )
    parts.append(
        '<div class="gallery-grid" id="galleryGrid" data-total="%d">%s</div>\n'
        % (total, "".join(render_item(rel) for rel in head_items))
    )
    if rest:
        parts.append(
            '<div class="gallery-sentinel" id="gallerySentinel" role="button" tabindex="0">'
            "正在加载更多…</div>\n"
        )
        parts.append('<script type="application/json" id="galleryData">%s</script>\n' % payload)
    # 灯箱脚本任何时候都要输出：图少到一屏放得下、没有分批数据时，照样要能点开大图。
    # 顺序必须是「先 DOM 后脚本」——脚本里要抓 galleryViewer，放在它前面会抓到 null。
    parts.append(VIEWER_HTML)
    parts.append(GALLERY_JS.replace("__PER_BATCH__", str(per_batch)))
    parts.append("</div>\n")
    return "".join(parts)


VIEWER_HTML = """<div class="gallery-viewer" id="galleryViewer" role="dialog" aria-modal="true" aria-label="查看大图">
<button class="gallery-viewer-close" id="galleryViewerClose" type="button" aria-label="关闭">&#10005;</button>
<button class="gallery-viewer-prev" id="galleryViewerPrev" type="button" aria-label="上一张">&#8249;</button>
<img id="galleryViewerImg" alt="">
<button class="gallery-viewer-next" id="galleryViewerNext" type="button" aria-label="下一张">&#8250;</button>
<div class="gallery-viewer-count" id="galleryViewerCount"></div>
</div>
"""

# 批量追加 + 画廊灯箱。放在页面里而不是 custom.js：灯箱需要「整册翻页」，
# 和 custom.js 那个只管单张的灯箱是两种交互，混在一起会互相抢 click 事件。
GALLERY_JS = """<script>(function(){
  var grid = document.getElementById("galleryGrid");
  if (!grid) return;
  var sentinel = document.getElementById("gallerySentinel");
  var dataEl = document.getElementById("galleryData");
  var rest = dataEl ? JSON.parse(dataEl.textContent || "[]") : [];
  var PER = __PER_BATCH__;
  var cursor = 0;
  /* 序号按「总数」显示，而不是「已渲染数」：滚到一半点开时不该出现 37 / 96 这种怪数字 */
  var total = parseInt(grid.getAttribute("data-total"), 10) || 0;

  function makeItem(url, name){
    var a = document.createElement("a");
    a.className = "gallery-item";
    a.href = url;
    a.setAttribute("data-name", name);
    var img = document.createElement("img");
    img.src = url;
    img.alt = name;
    img.loading = "lazy";
    img.decoding = "async";
    a.appendChild(img);
    return a;
  }

  function loadMore(){
    if (!sentinel || cursor >= rest.length) return;
    var frag = document.createDocumentFragment();
    var end = Math.min(cursor + PER, rest.length);
    for (var i = cursor; i < end; i++) frag.appendChild(makeItem(rest[i][0], rest[i][1]));
    grid.appendChild(frag);
    cursor = end;
    if (cursor >= rest.length) sentinel.parentNode.removeChild(sentinel);
  }

  if (sentinel && rest.length){
    sentinel.textContent = "向下滚动加载更多…";
    if ("IntersectionObserver" in window){
      var io = new IntersectionObserver(function(entries){
        for (var i = 0; i < entries.length; i++){
          if (entries[i].isIntersecting){ loadMore(); break; }
        }
      }, { rootMargin: "800px 0px" });
      io.observe(sentinel);
    } else {
      sentinel.addEventListener("click", loadMore);
    }
  }

  /* ---- 灯箱 ---- */
  var viewer = document.getElementById("galleryViewer");
  var big = document.getElementById("galleryViewerImg");
  var counter = document.getElementById("galleryViewerCount");
  if (!viewer || !big) return;
  var current = -1;

  function items(){ return [].slice.call(grid.querySelectorAll("a.gallery-item")); }

  function show(index){
    var list = items();
    if (!list.length) return;
    /* 往后翻到还没渲染的区域：先补一批，而不是直接绕回第一张 */
    if (index >= list.length && cursor < rest.length){
      loadMore();
      list = items();
    }
    if (index < 0){
      if (cursor < rest.length) loadMore();
      list = items();
      index = list.length - 1;
    }
    if (index >= list.length) index = 0;
    current = index;
    var link = list[index];
    big.src = link.getAttribute("href");
    big.alt = link.getAttribute("data-name") || "";
    if (counter) counter.textContent = (index + 1) + " / " + (total || list.length);
    viewer.classList.add("show");
    document.body.classList.add("gallery-viewer-open");
  }

  function hide(){
    viewer.classList.remove("show");
    document.body.classList.remove("gallery-viewer-open");
  }

  grid.addEventListener("click", function(event){
    var link = event.target.closest ? event.target.closest("a.gallery-item") : null;
    if (!link) return;
    event.preventDefault();
    /* 拦住冒泡：否则 custom.js 的通用灯箱会再弹一层 */
    event.stopPropagation();
    var list = items();
    show(list.indexOf(link));
  });

  document.getElementById("galleryViewerClose").addEventListener("click", hide);
  document.getElementById("galleryViewerNext").addEventListener("click", function(e){ e.stopPropagation(); show(current + 1); });
  document.getElementById("galleryViewerPrev").addEventListener("click", function(e){ e.stopPropagation(); show(current - 1); });
  /* 点大图本身不关闭（很多人习惯性点一下图），关闭走遮罩 / 关闭按钮 / Esc。
     同时要 stopPropagation，否则事件冒到 document 会被 custom.js 的通用灯箱接走。 */
  big.addEventListener("click", function(event){ event.stopPropagation(); });
  viewer.addEventListener("click", function(event){
    if (event.target === viewer) hide();
  });

  document.addEventListener("keydown", function(event){
    if (!viewer.classList.contains("show")) return;
    if (event.key === "Escape") hide();
    else if (event.key === "ArrowLeft") show(current - 1);
    else if (event.key === "ArrowRight") show(current + 1);
  });

  var touchX = null;
  viewer.addEventListener("touchstart", function(e){
    if (e.touches.length === 1) touchX = e.touches[0].clientX;
  }, { passive: true });
  viewer.addEventListener("touchend", function(e){
    if (touchX === null) return;
    var endX = e.changedTouches.length ? e.changedTouches[0].clientX : touchX;
    var delta = endX - touchX;
    touchX = null;
    if (Math.abs(delta) < 45) return;
    show(current + (delta < 0 ? 1 : -1));
  });
})();</script>
"""


def render_page(shell, title, description, canonical, body):
    parts = [build_head(shell["head"], title, description, canonical), "<body>\n"]
    parts.append(clean_header(shell["header"], title))
    parts.append('<div id="content">\n')
    parts.append(body)
    parts.append("</div>\n")
    parts.append(shell["footer"])
    return "".join(parts)


# ---------------------------------------------------------------- 导航

def clean_nav(docs, slug=INDEX_SLUG):
    """导航里指向本页的链接是 Gmeek 按 exlink 渲染的，固定带 target="_blank"。

    站内页面不该新开窗口，这里去掉（幂等：没有 target 就什么都不做）。
    """
    pattern = re.compile(r'<a href="(?:https?://[^"]*)?/%s\.html"[^>]*>' % re.escape(slug), re.I)
    changed = 0
    targets = list(docs.glob("*.html"))
    for path in targets:
        text = read_text(path)
        if "%s.html" % slug not in text:
            continue

        def repl(match):
            tag = re.sub(r'\s+target="_blank"', "", match.group(0))
            return re.sub(r'href="[^"]*"', 'href="/%s.html"' % slug, tag)

        updated = pattern.sub(repl, text)
        if updated != text:
            write_text(path, updated)
            changed += 1
    return changed


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", nargs="?", default="docs", help="docs 目录")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="页面标题 / 导航名字（默认 色廊）")
    parser.add_argument("--site", default="https://russianqin.github.io")
    parser.add_argument("--first-batch", type=int, default=48, help="首屏直接输出的图片数")
    parser.add_argument("--per-batch", type=int, default=48, help="滚动加载每批追加的图片数")
    parser.add_argument("--source", help="图片源目录（默认 data/gallery-images）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    if not docs.is_dir():
        log("找不到 docs 目录：%s" % docs)
        return 1

    src_dir = Path(args.source).resolve() if args.source else root / "data" / SRC_DIR_NAME
    images = scan_images(src_dir)
    if not src_dir.is_dir():
        log("图片目录还不存在：%s（先生成一个空页面，导航不会 404）" % src_dir)
    log("扫描到图片 %d 张：%s" % (len(images), src_dir))

    out_dir = docs / OUT_DIR_NAME
    copied, removed = sync_assets(src_dir, out_dir, images)
    if images:
        log("图片同步：新拷贝 %d 张，清理旧图 %d 张 → docs/%s/" % (copied, removed, OUT_DIR_NAME))

    shell = load_shell(docs)
    if not shell:
        log("取不到页面模板（docs 里没有可用页面），跳过生成")
        return 1
    log("页面模板取自 %s" % shell["source"])

    canonical = "%s/%s.html" % (args.site.rstrip("/"), INDEX_SLUG)
    if images:
        description = "%s：图片收藏，共 %d 张。" % (args.title, len(images))
    else:
        description = "%s：图片收藏，暂时还没有内容。" % args.title
    body = render_body(images, args.title, max(1, args.first_batch), max(1, args.per_batch))
    page = render_page(shell, args.title, description, canonical, body)
    write_text(docs / ("%s.html" % INDEX_SLUG), page)
    log("已生成 docs/%s.html（%d 张图，首屏 %d 张）" % (INDEX_SLUG, len(images), min(len(images), args.first_batch)))

    changed = clean_nav(docs)
    if changed:
        log("导航链接已改为站内跳转：%d 个页面" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
