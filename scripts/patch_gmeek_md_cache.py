#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_gmeek_md_cache.py —— 给 Gmeek 的 markdown 渲染加「本地缓存 + 限流重试」

背景：Gmeek 每次构建都会对每篇文章调用一次 GitHub 的 POST /markdown 接口。
文章少的时候没问题，一旦上百篇就会在几十秒内连发上百次请求，被 GitHub
的二级限流拒绝（403），整个构建失败——构建失败意味着博客不会更新。

这个脚本在构建时给 Gmeek.py 注入一小段包装逻辑：
  1. 渲染结果按正文哈希缓存到 .md-cache/，内容没变的文章直接复用（不再请求接口）
  2. 每次请求前加间隔（默认 2 秒），避免突发
  3. 遇到 403/429 自动等待后重试

用法（在 workflow 里，克隆完 Gmeek 之后、生成 HTML 之前执行）：
    python scripts/patch_gmeek_md_cache.py /opt/Gmeek/Gmeek.py

可用环境变量调节：
    GMEEK_MD_CACHE_DIR   缓存目录（默认 .md-cache，相对于 Gmeek 运行目录）
    GMEEK_MD_DELAY       每次请求前的间隔秒数（默认 2.0）
    GMEEK_MD_RETRY_WAIT  限流后首次等待秒数（默认 60，重试时线性递增）
    GMEEK_MD_MAX_ATTEMPTS 最大尝试次数（默认 8）

脚本是幂等的：重复执行不会重复注入；找不到锚点时会放弃注入并正常退出，
绝不因此让构建失败。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

MARKER = "# ==== injected by patch_gmeek_md_cache.py ===="
ANCHOR = "blog=GMEEK(options)"

_INJECT_BODY = r'''__MARKER__
import hashlib as _md_hl
import os as _md_os
import time as _md_t
from pathlib import Path as _MdPath

_MD_CACHE_DIR = _MdPath(_md_os.environ.get("GMEEK_MD_CACHE_DIR", ".md-cache"))
_MD_DELAY = float(_md_os.environ.get("GMEEK_MD_DELAY", "2.0"))
_MD_RETRY_WAIT = float(_md_os.environ.get("GMEEK_MD_RETRY_WAIT", "60"))
_MD_MAX_ATTEMPTS = int(_md_os.environ.get("GMEEK_MD_MAX_ATTEMPTS", "8"))
_MD_HITS = [0, 0]  # [缓存命中, 实际请求]


def _md_wrap(orig):
    """给 markdown2html 套一层：缓存 -> 限流 -> 重试。"""
    def wrapper(self, mdstr):
        try:
            _MD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        key = _md_hl.sha1(mdstr.encode("utf-8")).hexdigest()
        cache_file = _MD_CACHE_DIR / (key + ".html")
        try:
            if cache_file.exists():
                _MD_HITS[0] += 1
                return cache_file.read_text(encoding="utf-8")
        except Exception:
            pass

        last_error = None
        for attempt in range(_MD_MAX_ATTEMPTS):
            if _MD_DELAY:
                _md_t.sleep(_MD_DELAY)
            try:
                html = orig(self, mdstr)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                msg = str(exc)
                if ("403" in msg) or ("429" in msg) or ("rate limit" in msg.lower()):
                    wait = _MD_RETRY_WAIT * (attempt + 1)
                    print("markdown 接口受限，等待 {:.0f}s 后重试（第 {} 次）：{}".format(
                        wait, attempt + 1, msg[:140]), flush=True)
                    if wait:
                        _md_t.sleep(wait)
                    continue
                raise
            _MD_HITS[1] += 1
            try:
                cache_file.write_text(html, encoding="utf-8")
            except Exception:
                pass
            return html
        raise last_error

    return wrapper


GMEEK.markdown2html = _md_wrap(GMEEK.markdown2html)
print("[patch] markdown 缓存/限流已启用：cache={} delay={}s".format(_MD_CACHE_DIR, _MD_DELAY))
__MARKER__
'''

# 用 replace 而不是 format：注入的代码里含大量花括号，format 会误解析
INJECT = _INJECT_BODY.replace("__MARKER__", MARKER)


def patch(path: pathlib.Path) -> int:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[patch] {path} 已打过补丁，跳过")
        return 0
    if ANCHOR not in text:
        print(f"[patch] ⚠️ 在 {path} 里找不到锚点 `{ANCHOR}`，跳过注入"
              f"（Gmeek 上游可能改版，构建继续运行）")
        return 0
    path.write_text(text.replace(ANCHOR, INJECT + "\n" + ANCHOR, 1), encoding="utf-8")
    print(f"[patch] ✅ 已注入 markdown 缓存/限流逻辑：{path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="给 Gmeek.py 注入 markdown 缓存与限流重试")
    parser.add_argument("path", nargs="?", default="/opt/Gmeek/Gmeek.py",
                        help="Gmeek.py 路径")
    args = parser.parse_args(argv)
    target = pathlib.Path(args.path)
    if not target.exists():
        print(f"[patch] ⚠️ 找不到 {target}，跳过")
        return 0
    return patch(target)


if __name__ == "__main__":
    sys.exit(main())
