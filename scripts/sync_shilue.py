#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 personal_txt_files/释略大典.md 同步成博客「释略大典」页面（shilue.html）。

源文件格式（非常规整）：
    # A                          ← 字母分区（A-Z，另有开头的 "#"）
    **安堵：**                    ← 词条（整行加粗，可带冒号也可不带）
    方言，"安定"意。               ← 释义正文，可多行/多段

策略：**单源（源文件是唯一真源）**
  * 页面内容完全来自 释略大典.md，没有任何本地基线参与合并；
  * 源文件改了 → 页面跟着改；源文件删了 → 页面跟着消失；
  * 同一分区内重名词条按「归一化标题」去重，保留信息量更大的版本。

  源文件在 personal_txt_files 仓库，构建时联网拉取（见 SOURCE_URLS）。
  该仓库的 .github/workflows/notify-blog.yml 会在 释略大典.md 被 push 后
  触发本仓库的 Gmeek.yml，因此改动会自动流到博客。

输出格式与页面现有格式一致：
    ## A                           ← 分区标题
    **安堵：**                     ← 词条名（整行加粗）
    方言，"安定"意。                ← 释义，条目内空行 = 新段落
    ---                            ← 同分区内不同词条之间的分隔

用法：
    python scripts/sync_shilue.py docs
    python scripts/sync_shilue.py docs --source ../personal_txt_files/释略大典.md

【前置条件：Gmeek 的顶栏按钮依赖一个 GitHub Issue】
本脚本只负责生成 docs/shilue.html，**不会**让顶栏出现入口图标。
Gmeek 的顶栏单页按钮来自「Issue 的首个 label 命中 config.json 的 singlePage」：
    * 模板 plist.html:  <a href=".../{{ labels[0] }}.html" title="{{ postTitle }}">
    * 即 URL 取自 **label 名**，按钮名取自 **Issue 标题**，图标取自 iconList[label]
所以必须在该仓库建一个 Issue：
    * 标题 = 释略大典        （决定顶栏 tooltip / title）
    * label = shilue         （决定 URL：/shilue.html，必须与 config.json 的 singlePage 项一致）
参考现有单页：link → #5、refreshment → #12、quest in Moscow → #19、reading → #522
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


def dedupe(sections):
    """清理源文件内的重名词条：同一分区内 key（或高度相似）相同只留一条。

    保留信息量更大的版本；返回 (总条数, 重名清单)。
    """
    duplicates = []
    for section in sections:
        kept = []
        index = {}
        for entry in section["entries"]:
            key = key_of(entry)
            match = index.get(key)
            if match is None:
                for pos, other in enumerate(kept):
                    if difflib.SequenceMatcher(None, key, key_of(other)).ratio() >= SIMILARITY:
                        match = pos
                        break
            if match is None:
                index[key] = len(kept)
                kept.append(entry)
                continue
            other = kept[match]
            old_text = normalize("".join(other["paragraphs"]))
            new_text = normalize("".join(entry["paragraphs"]))
            if len(new_text) > len(old_text):
                kept[match] = entry
            duplicates.append(
                {
                    "section": section["name"] or "#",
                    "title": other["title"],
                    "count": 2,
                }
            )
        section["entries"] = kept
    total = sum(len(s["entries"]) for s in sections)
    return total, duplicates


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


PAGE_CSS = """
<style>
/* --- 页面留白：Gmeek 的 body 在窄屏把 padding 压到 8px，正文会顶到屏幕边。
       这里只做「补足」，桌面不动（45px 已够），窄屏给到 20px。 --- */
@media (max-width: 600px) {
  body { padding: 20px 20px 28px !important; }
}
@media (min-width: 601px) and (max-width: 900px) {
  body { padding: 28px 28px; }
}

.shilue-intro{color:var(--fgColor-muted,#59636e);margin:0 0 16px;}
.shilue-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px;}
.shilue-search{flex:1;min-width:200px;padding:6px 12px;font-size:14px;
  border:1px solid var(--borderColor-default,#d1d9e0);border-radius:6px;background:transparent;color:inherit;}
.shilue-search:focus{outline:none;border-color:var(--fgColor-accent,#0969da);}
.shilue-count{font-size:13px;color:var(--fgColor-muted,#59636e);white-space:nowrap;}

/* 字母导航：27 个字母排成一行（26+3=29px × 27 = 783px < 810px 容器宽）；
   窄屏放不下时才换行，末行居中，不留参差 */
.shilue-nav{display:flex;flex-wrap:wrap;gap:3px;justify-content:center;
  margin-bottom:24px;padding-bottom:16px;
  border-bottom:1px solid var(--borderColor-muted,#d1d9e0);}
.shilue-nav a,.shilue-nav span{display:inline-flex;align-items:center;justify-content:center;
  box-sizing:border-box;width:26px;height:26px;padding:0;border-radius:6px;
  font-size:12.5px;font-weight:600;
  border:1px solid var(--borderColor-default,#d1d9e0);text-decoration:none;color:inherit;
  flex:0 0 auto;}
.shilue-nav a:hover{background:var(--bgColor-accent-muted,#ddf4ff);border-color:var(--fgColor-accent,#0969da);}
.shilue-nav span.shilue-nav-empty{color:var(--fgColor-disabled,#8b949e);opacity:.5;}

.shilue-section{margin-bottom:32px;}
.shilue-section-title{font-size:24px;font-weight:700;margin:0 0 12px;
  padding-bottom:6px;border-bottom:1px solid var(--borderColor-muted,#d1d9e0);
  scroll-margin-top:16px;}
.shilue-section-empty{color:var(--fgColor-muted,#59636e);font-size:14px;font-style:italic;}

/* 词条：左右都留呼吸位，正文不再顶到容器右边缘 */
.shilue-entry{margin:0 0 18px;padding:2px 0 2px 14px;
  border-left:2px solid var(--borderColor-muted,#d1d9e0);}
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
  .shilue-nav{gap:3px;}
  .shilue-nav a,.shilue-nav span{width:23px;height:23px;font-size:11.5px;}
  .shilue-entry{padding-left:10px;}
  .shilue-top{right:14px;bottom:14px;}
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
    parser.add_argument("--site", default="https://russianqin.github.io")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    if not docs.is_dir():
        log("找不到 docs 目录：%s" % docs)
        return 1

    if args.source:
        source_text = read_text(args.source)
        log("使用本地源文件：%s（%d 字符）" % (args.source, len(source_text)))
    else:
        source_text = fetch_source()

    sections = parse_source(source_text) if source_text else []
    if not sections:
        log("源文件解析结果为空，退出")
        return 1

    total, duplicates = dedupe(sections)
    log("源文件：%d 条 / %d 个分区" % (total, len(sections)))
    if duplicates:
        log("  源文件内重名词条 %d 组（已合并为一条）：" % len(duplicates))
        for item in duplicates:
            log("    [%s] %s ×%d" % (item["section"], item["title"], item["count"]))
    else:
        log("  源文件内无重名词条")

    shell = load_shell(docs)
    if shell is None:
        log("找不到可借用的页面骨架（docs/post/*.html 或 index.html），无法生成页面")
        return 1

    alphabet = build_alphabet()
    body = render_body(sections, alphabet)
    description = "释略大典：%d 条词语、典故与概念，按首字母排列，可搜索。" % total
    canonical = "%s/%s.html" % (args.site.rstrip("/"), SLUG)
    head = build_head(shell["head"], "%s - 五环魔法师" % TITLE, description, canonical)

    page = [
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
