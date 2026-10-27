#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_gmeek_plist_order.py —— 让首页文章列表在同一秒的文章里按编号定序

## 问题

Gmeek 的 `createPlistHtml()` 用这一行给首页（以及 pageN.html）排序：

    self.blogBase["postListJson"]=dict(
        sorted(..., key=lambda x:(x[1]["top"], x[1]["createdAt"]), reverse=True))

键里只有「置顶」和「创建时间（秒）」。一批文章如果是同一秒内连续创建的，
`createdAt` 完全相同，Python 的稳定排序在相等键上保持**输入顺序**；而输入顺序
是 issue 编号升序（`_gm_all_open_issues()` 返回 `sorted(found)`，见
patch_gmeek_issue_paging.py）。于是同一批文章变成「组间倒序、组内正序」：

    薄肌记录 05 → 06 → 07 → 03 → 04 → 01 → 02      （2026-10-27 实例）

肉眼就是乱的。这不是排序方向写错，是排序键不够用。

## 补丁做的事

1. 注入 `_gm_post_number(key)`：把 `"P535"` 这类键换成 `535`，非法键返回 0。
2. 把上面那行的排序键换成 `(top, createdAt, 编号)` —— 时间戳相同就按编号兜底。
   效果：同一批上传的文章整体读起来是连贯的倒序（07 → 01），和全站「最新在前」
   一致；后续单独发布的文章没有同秒竞争，行为不变。

**只动首页那一处**（第 214 行的 `createPlistHtml`）。RSS 和 blogBase.json 里
那两处 `createdAt` 排序不动：它们不决定读者看到的顺序，`sync_topics.py` 自己也
会按板块的 `order` 重排。

脚本是幂等的；找不到锚点只告警、不中止构建 —— 排序错了顶多难看，不该让站点挂掉。

用法（workflow 里，克隆完 Gmeek、打完另外两个补丁之后）：
    python scripts/patch_gmeek_plist_order.py /opt/Gmeek/Gmeek.py
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

MARKER = "# ==== injected by patch_gmeek_plist_order.py ===="
ANCHOR = "blog=GMEEK(options)"

# 只匹配 createPlistHtml 里那一处：键是 (top, createdAt) 的二元组。
PLIST_KEY_RE = re.compile(
    r'key=lambda x:\(x\[1\]\["top"\],x\[1\]\["createdAt"\]\)'
)

NEW_KEY = 'key=lambda x:(x[1]["top"],x[1]["createdAt"],_gm_post_number(x[0]))'

_HELPER = r'''__MARKER__
def _gm_post_number(key):
    """首页排序的兜底键：同一秒创建的多篇文章靠 issue 编号定序。

    Gmeek 只按 (top, createdAt) 排，同秒的几篇在稳定排序下会保持插入顺序
    （= 编号升序），于是「组间倒序、组内正序」，读起来是乱的。
    编号在同一个仓库里单调递增，放进排序元组就能让整批文章连贯倒序。
    """
    try:
        if key[:1] == "P" and key[1:].isdigit():
            return int(key[1:])
    except Exception:
        pass
    return 0


'''


def patch(path: pathlib.Path) -> int:
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print("[patch-plist] 已注入过，跳过：%s" % path)
        return 0

    match = PLIST_KEY_RE.search(source)
    if not match:
        print("[patch-plist] ⚠️ 找不到 createPlistHtml 的排序键，跳过注入"
              "（Gmeek 上游可能改版，构建继续运行）")
        return 0

    if ANCHOR not in source:
        print("[patch-plist] ⚠️ 找不到注入锚点 %r，跳过" % ANCHOR)
        return 0

    patched = source[:match.start()] + NEW_KEY + source[match.end():]
    patched = patched.replace(ANCHOR, _HELPER.replace("__MARKER__", MARKER) + ANCHOR, 1)
    path.write_text(patched, encoding="utf-8")
    print("[patch-plist] 已注入：首页列表同秒文章按 issue 编号兜底排序")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="给 Gmeek 首页列表加编号兜底排序")
    parser.add_argument("gmeek_py", nargs="?", default="/opt/Gmeek/Gmeek.py")
    args = parser.parse_args()
    path = pathlib.Path(args.gmeek_py)
    if not path.is_file():
        print("[patch-plist] ⚠️ 找不到 %s，跳过" % path)
        return 0
    return patch(path)


if __name__ == "__main__":
    sys.exit(main())
