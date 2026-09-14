#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：<head> 里出现"裸文本"的页面。

为什么需要它：HTML 规范规定 head 里出现字符数据时，浏览器会提前结束 head，
把这段文字挪进 <body> 顶部显示出来。2026-09 出过一次事故——
`optimize_docs.py` / `sync_curated.py` 的正则被属性值里的 ">" 截断，
导致 480 个收藏页顶部多出两行 `📄 本文来自微博旧文归档（[原微博](…)）。">`。

用法：
    python scripts/check_head.py docs      # 有裸文本时退出码为 1（可直接挂到工作流）
"""

import glob
import os
import sys
from html.parser import HTMLParser


class HeadTextFinder(HTMLParser):
    """收集 head 里位于标签之外、且不在 script/style/title/template 内部的文本。"""

    SKIP = {"script", "style", "title", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.skip_depth = 0
        self.hits = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "head":
            self.in_head = True
        elif self.in_head and tag in self.SKIP:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "head":
            self.in_head = False
        elif self.in_head and tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.in_head or self.skip_depth:
            return
        text = data.strip()
        if text:
            self.hits.append(text)


def check(path):
    parser = HeadTextFinder()
    with open(path, encoding="utf-8", errors="replace") as handle:
        parser.feed(handle.read())
    return parser.hits


def main():
    docs = sys.argv[1] if len(sys.argv) > 1 else "docs"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    files = sorted(glob.glob(os.path.join(docs, "**", "*.html"), recursive=True))
    bad = []
    for path in files:
        hits = check(path)
        if hits:
            bad.append((path, hits))

    print("检查 %d 个页面，head 里有裸文本的：%d 个" % (len(files), len(bad)))
    for path, hits in bad[:limit]:
        rel = os.path.relpath(path, docs).replace(os.sep, "/")
        print("   %-44s %s" % (rel, hits[0][:70].replace("\n", " ")))
    if len(bad) > limit:
        print("   …（共 %d 个）" % len(bad))
    if bad:
        print("\n提示：通常是清理 meta 标签的正则被属性值里的 \">\" 截断了，"
              "检查 scripts 里匹配标签的写法（应跳过成对引号）。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
