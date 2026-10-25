#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_gmeek_issue_paging.py —— 让 Gmeek 全量重建时能取到**全部** open issue

## 事故记录（2026-10-22）

一次普通的 push 触发全量重建（runAll），线上从 510 篇文章 + 5 个单页板块
掉到 59 篇、3 个板块直接 404：

  * 个人页面      (label `link`，Issue #5)
  * 日日新        (label `refreshment`，Issue #12)
  * 莫斯科生存攻略 (label `quest in Moscow`，Issue #19)

根因不在页面代码，而在取 Issue 的方式：

GitHub 的 `GET /repos/{owner}/{repo}/issues` 正在灰度换成**游标分页**，
响应头 `Link` 里给的是 `?page=2&after=Y3Vyc29y...`，但直接请求 `?page=N`
从第 2 页起就返回空数组。实测这个仓库只能翻到第 3 页（约 300 个 issue，
编号 228 ~ 529），再往后一律空 —— 于是编号 1 ~ 227 的老 Issue 全部取不到。
Gmeek 的 `runAll()` 里只有一句 `issues = self.repo.get_issues()`，
拿到多少算多少，缺的部分不会有任何提示，直接被当成「这些文章不存在」。

为什么以前没暴露：Issue 事件（opened / edited）走的是 `runOne()`，
只重写那一篇、其余页面文件原样保留，所以老页面一直活着；
只有 runAll 才会 `cleanFile()` 清空 docs 再按 Issue 重建。这次因为改了
config.json 触发 push → runAll，隐患一次全炸。

## 补丁做的事

1. 注入 `_gm_all_open_issues(repo)`：先用列表接口尽量拿，再**按 issue 编号
   把区间里的缺口逐个补齐**（`repo.get_issue(n)` 单取不受游标分页影响，
   #100 / #250 实测都能正常返回）。列表接口将来修好了，补齐循环一次都不跑。
2. 把 `runAll()` 里的 `issues=self.repo.get_issues()` 换成上面这个函数。
3. 加一道保险：本次拿到的 issue 数明显少于 blogBase.json 缓存（默认跌破 80%）
   就**抛异常让构建失败**。构建失败 → docs 不会被覆盖 → 线上保持上一版完整内容，
   比「静默清空」好得多。阈值可用 `GMEEK_MIN_ISSUE_RATIO` 调整（设 0 关闭）。

用法（workflow 里，克隆完 Gmeek 之后、生成 HTML 之前）：
    python scripts/patch_gmeek_issue_paging.py /opt/Gmeek/Gmeek.py

脚本幂等：已注入过就直接退出。**找不到锚点会以非零码退出** —— 与
patch_gmeek_md_cache.py 的「尽力而为」不同：这一步静默失败的代价是线上文章被清空，
宁可让构建红一次。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

MARKER = "# ==== injected by patch_gmeek_issue_paging.py ===="
ANCHOR = "blog=GMEEK(options)"

# runAll() 里取 issue 的那两行（缩进 8 空格）。兼容 = 两边带空格的写法。
RUNALL_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)issues[ \t]*=[ \t]*self\.repo\.get_issues\(\)[ \t]*\r?\n"
    r"[ \t]*for issue in issues:[ \t]*$"
)

_HELPER = r'''__MARKER__
import json as _gm_json
import os as _gm_os


def _gm_cached_issue_count():
    """上一次构建留下的条目数（postListJson + singeListJson），用来兜底比较。"""
    try:
        with open("blogBase.json", "r", encoding="utf-8") as _f:
            _old = _gm_json.loads(_f.read())
    except Exception:
        return 0
    return len(_old.get("postListJson") or {}) + len(_old.get("singeListJson") or {})


def _gm_cached_max_number():
    """缓存里出现过的最大 issue 编号；列表接口彻底不可用时拿它当补齐上界。"""
    try:
        with open("blogBase.json", "r", encoding="utf-8") as _f:
            _old = _gm_json.loads(_f.read())
    except Exception:
        return 0
    best = 0
    for _key in list(_old.get("postListJson") or {}) + list(_old.get("singeListJson") or {}):
        if _key[:1] == "P" and _key[1:].isdigit():
            best = max(best, int(_key[1:]))
    return best


def _gm_is_pull_request(issue):
    """PR 也会出现在 issues 列表里，别把它当文章渲染成页面。

    **只能读 `_rawData`，不要碰 `issue.pull_request` 或 `issue.raw_data`。**

    这两个属性都会触发 PyGithub 的惰性补全：`pull_request` 走
    `_completeIfNotSet`，`raw_data` 走 `CompletableGithubObject.raw_data` 里的
    `_completeIfNeeded`。而 GitHub 的列表接口对**非 PR** 的 issue 根本不返回
    `pull_request` 字段，属性恒为 NotSet —— 等于给每一篇都补发一次
    `GET /issues/{n}`。520 篇就是 520 次串行往返（2026-10-25 实测：
    构建的 Generate 步骤因此从 19 秒涨到 275 秒）。

    `_rawData` 是构造对象时就填好的原始载荷，读它不发请求。
    """
    raw = getattr(issue, "_rawData", None) or {}
    try:
        return bool((raw.get("pull_request") or {}).get("url"))
    except Exception:
        return False


def _gm_all_open_issues(repo):
    """取全部 open issue。

    GitHub 的 issues 列表接口切到游标分页后，`?page=N` 从第 2 页起返回空，
    只能拿到最近约 300 个 issue，更老的整批取不到（link / refreshment /
    quest in Moscow 这几个单页就在其中，缺了它们对应页面会 404）。

    所以：先用列表接口尽量拿（能拿多少算多少，中途出错不中断），
    再按 issue 编号把 1..max 区间里的缺口逐个补齐。单编号查询不受分页影响。
    """
    found = {}
    latest = 0
    try:
        for issue in repo.get_issues(state="open"):
            if _gm_is_pull_request(issue):
                continue
            found[issue.number] = issue
            if issue.number > latest:
                latest = issue.number
            if len(found) >= 5000:
                break
    except Exception as _exc:  # 列表接口翻页中途断掉也继续，靠补齐兜住
        print("issues list paging stopped early: %s" % _exc)

    if not found:
        # 列表接口整个不可用（第一页就挂了）：拿缓存里的最大编号当上界，
        # 逐号扫一遍。多留 20 个余量，免得连最近新建的几篇也漏掉。
        latest = _gm_cached_max_number()
        if latest:
            latest += 20
        print("issues list unusable, scanning 1..%d by number instead" % latest)

    missing = [n for n in range(1, latest + 1) if n not in found]
    if missing:
        print("backfill %d issues by number (list returned %d, newest #%d)"
              % (len(missing), len(found), latest))
    backfilled = 0
    for number in missing:
        try:
            issue = repo.get_issue(number)
        except Exception:
            continue
        if issue.state != "open" or _gm_is_pull_request(issue):
            continue
        found[number] = issue
        backfilled += 1
    if missing:
        print("backfilled %d issues (#%d ~ #%d)" % (backfilled, min(missing), max(missing)))

    cached = _gm_cached_issue_count()
    try:
        ratio = float(_gm_os.environ.get("GMEEK_MIN_ISSUE_RATIO", "0.8"))
    except ValueError:
        ratio = 0.8
    if cached and ratio > 0 and len(found) < cached * ratio:
        raise RuntimeError(
            "refusing to rebuild: only %d issue(s) fetched, but blogBase.json has %d. "
            "Rebuilding now would wipe most of the site. "
            "Set GMEEK_MIN_ISSUE_RATIO=0 to override." % (len(found), cached)
        )

    print("total open issues: %d (list %d + backfill %d)"
          % (len(found), len(found) - backfilled, backfilled))
    # 按编号升序处理：结果与接口返回顺序无关，postListJson 之后会按时间重排
    return [found[n] for n in sorted(found)]


'''


def patch(path: pathlib.Path) -> int:
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print("[patch-paging] 已注入过，跳过：%s" % path)
        return 0

    match = RUNALL_RE.search(source)
    if not match:
        print("[patch-paging] 失败：在 %s 里找不到 runAll 的 `issues=self.repo.get_issues()`" % path)
        print("[patch-paging] Gmeek 可能改了实现，先确认再放行；本次构建已中止。")
        return 1

    if ANCHOR not in source:
        print("[patch-paging] 失败：找不到注入锚点 %r" % ANCHOR)
        return 1

    indent = match.group("indent")
    replacement = "%sfor issue in _gm_all_open_issues(self.repo):" % indent
    patched = source[:match.start()] + replacement + source[match.end():]

    patched = patched.replace(ANCHOR, _HELPER.replace("__MARKER__", MARKER) + ANCHOR, 1)
    path.write_text(patched, encoding="utf-8")
    print("[patch-paging] 已注入：按编号补齐 issue + 数量骤降保护")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gmeek_py", nargs="?", default="/opt/Gmeek/Gmeek.py")
    args = parser.parse_args()
    path = pathlib.Path(args.gmeek_py)
    if not path.is_file():
        print("[patch-paging] 失败：找不到 %s" % path)
        return 1
    return patch(path)


if __name__ == "__main__":
    sys.exit(main())
