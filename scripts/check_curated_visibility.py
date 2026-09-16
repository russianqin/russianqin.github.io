#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：收藏页"隐藏展示"是否真的生效。

规则（任一不满足就报错并退出 1）：
  1. data/hidden/*.json 里的文章，不得出现在 docs/curated.html 的列表里；
  2. 它的编号页 docs/curated/<编号>.html 不得存在（否则别人按编号就能翻到）；
  3. 隐藏页 docs/curated/h-<hash>.html 必须存在，并且带 noindex；
  4. sitemap.xml 里不得出现隐藏页或编号页的 URL；
  5. 记录里还没有页面地址（刚隐藏、还没构建）只提示，不算失败。

用法：python scripts/check_curated_visibility.py docs
"""

import json
import pathlib
import sys


def load_records(hidden_dir):
    records = []
    if not hidden_dir.is_dir():
        return records
    for path in sorted(hidden_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            records.append({"slug": path.stem, "page": "", "error": str(exc)})
            continue
        if not isinstance(data, dict):
            data = {"slug": str(data).strip()}
        records.append(
            {
                "slug": str(data.get("slug") or path.stem).strip(),
                "title": str(data.get("title") or "").strip(),
                "page": str(data.get("page") or "").strip(),
            }
        )
    return records


def main():
    docs = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
    root = docs.resolve().parent
    records = load_records(root / "data" / "hidden")

    index_file = docs / "curated.html"
    index_text = index_file.read_text(encoding="utf-8", errors="replace") if index_file.exists() else ""
    sitemap_file = docs / "sitemap.xml"
    sitemap_text = sitemap_file.read_text(encoding="utf-8", errors="replace") if sitemap_file.exists() else ""

    problems = []
    notes = []
    for record in records:
        slug = record["slug"]
        if record.get("error"):
            problems.append("隐藏记录解析失败：%s（%s）" % (slug, record["error"]))
            continue
        numbered = "/curated/%s.html" % slug
        if numbered in index_text or ('data-slug="%s"' % slug) in index_text:
            problems.append("隐藏文章仍出现在收藏列表页：%s" % slug)
        if (docs / "curated" / ("%s.html" % slug)).exists():
            problems.append("隐藏文章的编号页仍然存在，可被按编号枚举：/curated/%s.html" % slug)
        if numbered in sitemap_text:
            problems.append("隐藏文章的编号 URL 仍在 sitemap 里：%s" % numbered)
        page = record["page"]
        if not page:
            notes.append("隐藏记录还没有页面地址（刚隐藏、还没构建）：%s" % slug)
            continue
        page_file = docs / page.lstrip("/")
        if not page_file.exists():
            problems.append("隐藏页文件不存在：%s（%s）" % (page, slug))
            continue
        head = page_file.read_text(encoding="utf-8", errors="replace")[:4000]
        if 'name="robots"' not in head or "noindex" not in head:
            problems.append("隐藏页缺少 noindex：%s" % page)
        if page in sitemap_text:
            problems.append("隐藏页 URL 仍出现在 sitemap：%s" % page)

    print(
        "隐藏记录 %d 条；列表页展示 %d 篇；sitemap 收藏条目 %d 条"
        % (len(records), index_text.count('class="curated-item"'), sitemap_text.count("/curated/"))
    )
    for note in notes:
        print("   提示：" + note)
    for problem in problems:
        print("   问题：" + problem)
    if problems:
        print("\n体检未通过：隐藏展示没有生效（详见上面的问题）")
        return 1
    print("体检通过：隐藏的文章不在列表里、不能按编号访问，也没有进 sitemap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
