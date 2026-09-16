#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Markdown → GitHub Issue 同步（博客的「本地写作」链路）。

博客由 Gmeek 从仓库里 open 的 Issue 生成，所以「在电脑上写 md 就能发文」的
前提是先把 md 变成 Issue。本脚本负责这件事，并且是幂等的：

    推送 posts/*.md → 本脚本建/更新/关闭对应 Issue → Gmeek 生成页面 → 部署

两个子命令：
    publish   读 posts/**.md，新建 / 更新 / 关闭对应 Issue，维护 data/local-posts.json
    assets    把 posts/images 里被正文引用的图片复制到 docs/local-images/，清理多余文件

约定：
    * posts/ 下以 _ 或 . 开头的文件、README.md 不参与发布（模板命名为 _模板.md）
    * front matter 支持 title / tags 两个字段，其余字段原样忽略
    * 正文里写 ![](images/xxx.jpg) 指向 posts/images/xxx.jpg；
      发布时链接会改写为 https://russianqin.github.io/local-images/xxx.jpg，
      构建时再把图片复制到站点，图就永久跟着博客走
    * md 是唯一真源：在 GitHub 网页上直接改 Issue，会被下一次构建覆盖回去
    * tags 里禁止出现 config.json 里 singlePage 的页面名，否则会把现成的独立页面顶掉

用法：
    python scripts/sync_local_posts.py publish           # CI / 本地（需 GITHUB_TOKEN）
    python scripts/sync_local_posts.py publish --check   # 只看会做什么，不写任何东西
    python scripts/sync_local_posts.py assets docs       # 复制图片（构建后执行）
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SITE = "https://russianqin.github.io"
DEFAULT_REPO = "russianqin/russianqin.github.io"
DEFAULT_POSTS_DIR = "posts"
DEFAULT_MAP_PATH = "data/local-posts.json"
IMAGE_SUBDIR = "images"
IMAGE_OUT_DIR = "local-images"
API_VERSION = "2022-11-28"
USER_AGENT = "russianqin-blog-local-posts/1.0 (+https://russianqin.github.io)"
MAX_BODY_BYTES = 65000  # GitHub Issue 正文上限 65536 字符，留一点余量
SKIP_NAMES = ("readme.md", "readme.markdown")
POST_SUFFIX = (".md", ".markdown")

FRONT_MATTER_RE = re.compile(r"^---[ \t]*\n(?P<meta>.*?)\n---[ \t]*(?:\n|$)", re.S)
KEY_RE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_\-]*)[ \t]*:[ \t]*(?P<value>.*?)[ \t]*$")
ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(?P<value>.+?)[ \t]*$")
# 图片引用：markdown 的 ![alt](path "title") 和 html 的 <img src="path">
MD_IMAGE_RE = re.compile(
    r"(!\[[^\]]*\]\()\s*(?P<ref><[^>]*>|[^)\s]+)(?P<tail>(?:\s+\"[^\"]*\")?\s*\))"
)
# 宽容模式：路径里带空格、又没写尖括号时（不严格符合 Markdown 规范）也认，
# 前提是这个文件真的存在于 posts/images/ 下。
LOOSE_MD_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()(?P<inner>[^)\n]*?)(?P<tail>\s*\))")
HTML_IMAGE_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])(?P<ref>[^\"']+)([\"'])", re.I)


def log(msg):
    print("[local] %s" % msg)


def read_text(path):
    """按 UTF-8 读；记事本存成 GBK / UTF-16 时也能正常读出来，并统一成 LF 换行。"""
    data = Path(path).read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = None
        for encoding in ("utf-8-sig", "gbk"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def normalize(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


# ------------------------------------------------------------ front matter

def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _split_list(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    parts = [_scalar(part) for part in value.split(",")]
    return [part for part in parts if part]


def parse_front_matter(text):
    """返回 (meta, body)。meta 里 tags 一律是列表，未写则为 None。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text

    meta = {}
    list_key = None
    for raw in match.group("meta").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        item = ITEM_RE.match(line)
        if item and list_key:
            meta[list_key].append(_scalar(item.group("value")))
            continue

        kv = KEY_RE.match(line)
        if not kv:
            continue
        key = kv.group(1).lower()
        value = kv.group("value").strip()

        if key in ("tag", "tags", "labels"):
            if value:
                meta["tags"] = _split_list(value)
                list_key = None
            else:
                meta["tags"] = []
                list_key = "tags"
            continue

        list_key = None
        meta[key] = _scalar(value) if value else ""

    body = text[match.end():].lstrip("\n")
    return meta, body


# ------------------------------------------------------------------ images

class LocalImages(object):
    """把正文里的本地图片路径换成站内 URL，并记录真正被引用的文件。"""

    def __init__(self, root, site, posts_dir=DEFAULT_POSTS_DIR):
        self.root = Path(root)
        self.site = site.rstrip("/")
        self.posts_dir = posts_dir
        self.used = set()
        self.missing = []

    @property
    def source_dir(self):
        return self.root / self.posts_dir / IMAGE_SUBDIR

    def name_of(self, ref):
        """把 md 里的相对路径映射成 posts/images 下的相对文件名；外链返回 None。"""
        ref = (ref or "").strip()
        if not ref or "://" in ref or ref.startswith(("//", "#", "data:", "mailto:")):
            return None
        ref = ref.split("#", 1)[0].split("?", 1)[0]
        while ref.startswith("./"):
            ref = ref[2:]
        ref = ref.lstrip("/")
        prefix = self.posts_dir.strip("/") + "/"
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
        if ref.startswith(IMAGE_SUBDIR + "/"):
            ref = ref[len(IMAGE_SUBDIR) + 1:]
        return ref or None

    def url(self, name):
        return "%s/%s/%s" % (self.site, IMAGE_OUT_DIR, urllib.parse.quote(name))

    def local_path(self, name):
        return self.source_dir / name

    def rewrite(self, text):
        def convert(ref):
            name = self.name_of(ref)
            if name is None:
                return ref
            if not self.local_path(name).is_file():
                if ref not in self.missing:
                    self.missing.append(ref)
                return ref
            self.used.add(name)
            return self.url(name)

        def md_repl(match):
            raw = match.group("ref")
            if raw.startswith("<") and raw.endswith(">"):
                inner, wrapped = raw[1:-1], True
            else:
                inner, wrapped = raw, False
            new = convert(inner)
            if new == inner:
                return match.group(0)
            head = match.group(1)
            tail = match.group("tail")
            return head + ("<%s>" % new if wrapped else new) + tail

        def html_repl(match):
            new = convert(match.group("ref"))
            if new == match.group("ref"):
                return match.group(0)
            return match.group(1) + new + match.group(3)

        def loose_repl(match):
            inner = match.group("inner").strip()
            if inner.startswith("<") and inner.endswith(">"):
                inner = inner[1:-1]
            new = convert(inner)
            if new == inner:
                return match.group(0)
            return match.group(1) + new + match.group("tail")

        text = MD_IMAGE_RE.sub(md_repl, text)
        text = HTML_IMAGE_RE.sub(html_repl, text)
        return LOOSE_MD_IMAGE_RE.sub(loose_repl, text)

    def scan(self, body):
        """只收集引用，不作替换。"""
        self.rewrite(body)


# --------------------------------------------------------------- 收集文章

def collect_posts(posts_dir, images, posts_dir_name=DEFAULT_POSTS_DIR):
    posts = []
    for path in sorted(posts_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in POST_SUFFIX:
            continue
        name = path.name
        if name.startswith(("_", ".")) or name.lower() in SKIP_NAMES:
            continue

        rel = path.relative_to(posts_dir.parent).as_posix()
        meta, body = parse_front_matter(read_text(path))
        title = (meta.get("title") or "").strip() or path.stem
        tags = meta.get("tags")
        body = images.rewrite(body)
        posts.append(
            {
                "key": rel,
                "file": path,
                "title": title,
                "tags": tags,
                "body": body,
            }
        )
    return posts


# ----------------------------------------------------------------- GitHub

class GitHub(object):
    def __init__(self, repo, token, api_base):
        self.repo = repo
        self.token = token
        self.api_base = api_base.rstrip("/")

    def request(self, method, path, payload=None):
        url = self.api_base + path
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", "Bearer %s" % self.token)
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("GitHub API %s %s 失败：HTTP %s %s" % (method, path, exc.code, detail[:500]))
        return json.loads(raw) if raw.strip() else None

    def issues(self):
        out = []
        page = 1
        while True:
            batch = self.request(
                "GET",
                "/repos/%s/issues?state=all&per_page=100&page=%d" % (self.repo, page),
            ) or []
            for item in batch:
                if "pull_request" in item:
                    continue
                out.append(item)
            if len(batch) < 100:
                break
            page += 1
        return out

    def labels(self):
        names = set()
        page = 1
        while True:
            batch = self.request(
                "GET",
                "/repos/%s/labels?per_page=100&page=%d" % (self.repo, page),
            ) or []
            for item in batch:
                names.add(item.get("name"))
            if len(batch) < 100:
                break
            page += 1
        return names

    def create_label(self, name, color="c5def5"):
        return self.request(
            "POST", "/repos/%s/labels" % self.repo, {"name": name, "color": color}
        )

    def create_issue(self, title, body, labels):
        return self.request(
            "POST",
            "/repos/%s/issues" % self.repo,
            {"title": title, "body": body, "labels": labels},
        )

    def update_issue(self, number, title, body, labels, state="open"):
        payload = {"title": title, "body": body, "state": state}
        if labels is not None:
            payload["labels"] = labels
        return self.request("PATCH", "/repos/%s/issues/%d" % (self.repo, number), payload)

    def close_issue(self, number):
        return self.request("PATCH", "/repos/%s/issues/%d" % (self.repo, number), {"state": "closed"})


# -------------------------------------------------------------- 映射表读写

def load_map(path):
    if not Path(path).is_file():
        return {}
    try:
        data = json.loads(read_text(path))
    except ValueError:
        log("警告：%s 不是合法 JSON，按空映射处理" % path)
        return {}
    posts = data.get("posts") if isinstance(data, dict) else None
    if isinstance(posts, dict):
        return posts
    return data if isinstance(data, dict) else {}


def save_map(path, mapping):
    payload = {
        "version": 1,
        "note": "本文件由 scripts/sync_local_posts.py 自动维护：md 文件 → Issue 编号。",
        "posts": dict(sorted(mapping.items())),
    }
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def map_number(entry):
    if isinstance(entry, dict):
        value = entry.get("issue")
    else:
        value = entry
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ publish

def run_publish(args):
    root = Path(__file__).resolve().parent.parent
    posts_dir = root / args.posts
    map_path = root / args.map_path
    if not posts_dir.is_dir():
        log("没有找到 %s 目录，跳过（先在仓库里建 posts/ 再放 md 文件）" % args.posts)
        return 0

    blocked = load_single_page_names(root)
    images = LocalImages(root, args.site, args.posts)
    posts = collect_posts(posts_dir, images, args.posts)
    log("解析到本地文章 %d 篇" % len(posts))
    for item in images.missing:
        log("警告：正文引用了不存在的图片 %s，这次保留原链接" % item)

    errors = 0
    titles = {}
    for post in posts:
        titles.setdefault(post["title"].strip(), []).append(post["key"])
        if len(post["body"]) > MAX_BODY_BYTES:
            log(
                "错误：%s 正文 %d 字，超过单个 Issue 上限，请拆成多篇"
                % (post["key"], len(post["body"]))
            )
            errors += 1
        tags = post["tags"] or []
        hit = [tag for tag in tags if tag.strip() in blocked]
        if hit:
            log(
                "错误：%s 的 tags 用了独立页面名 %s，会顶掉现有页面，请换一个标签名"
                % (post["key"], "、".join(hit))
            )
            errors += 1
    for title, keys in sorted(titles.items()):
        if len(keys) > 1:
            log("错误：这几篇的标题都是「%s」，会生成同一个网页，请改标题：%s" % (title, "、".join(keys)))
            errors += 1
    if errors:
        log("有 %d 处问题，先修好再推送" % errors)
        return 1

    mapping = load_map(map_path)
    token = args.token
    if not token and not args.check:
        log("缺少 GITHUB_TOKEN，无法创建/更新 Issue（可加 --check 只做本地检查）")
        return 1

    gh = GitHub(args.repo, token or "", args.api_base) if token else None
    issues_by_number = {}
    issues_by_title = {}
    known_labels = set()
    if gh:
        for issue in gh.issues():
            issues_by_number[issue["number"]] = issue
            if (issue.get("user") or {}).get("login", "").endswith("[bot]"):
                issues_by_title.setdefault(issue["title"].strip(), issue)
        known_labels = gh.labels()
        log("GitHub 上现有 Issue %d 个，标签 %d 个" % (len(issues_by_number), len(known_labels)))
    else:
        log("未提供 GITHUB_TOKEN，只做本地检查（不会写入任何东西）")

    current_keys = set()
    created = updated = unchanged = 0
    new_labels = set()
    for post in posts:
        current_keys.add(post["key"])
        number = map_number(mapping.get(post["key"]))
        issue = issues_by_number.get(number) if number else None
        if issue is None:
            issue = issues_by_title.get(post["title"].strip())

        tags = post["tags"]
        label_names = None if tags is None else sorted(set(tags))
        if label_names:
            for name in label_names:
                if name not in known_labels:
                    new_labels.add(name)

        if gh is None:
            # 没有 token：只按映射表如实报告，不猜
            if number:
                log("[已知] %s → Issue #%d（无法联网核对内容变化）" % (post["key"], number))
            else:
                log("[新] %s → %s（首次发布会新建 Issue）" % (post["key"], post["title"]))
            continue

        if issue is None:
            log("[新] %s → %s" % (post["key"], post["title"]))
            created += 1
            if args.check:
                continue
            result = gh.create_issue(post["title"], post["body"], label_names or [])
            mapping[post["key"]] = {"issue": result["number"]}
            issues_by_number[result["number"]] = result
            issues_by_title[result["title"].strip()] = result
            continue

        number = issue["number"]
        same_body = normalize(issue.get("body")) == normalize(post["body"])
        same_title = (issue.get("title") or "").strip() == post["title"].strip()
        current_labels = sorted(label["name"] for label in issue.get("labels") or [])
        same_labels = label_names is None or current_labels == (label_names or [])
        reopened = issue.get("state") != "open"
        if same_body and same_title and same_labels and not reopened:
            mapping[post["key"]] = {"issue": number}
            unchanged += 1
            continue

        log(
            "[改] %s → Issue #%d（%s）"
            % (
                post["key"],
                number,
                "、".join(
                    [
                        "正文" if not same_body else "",
                        "标题" if not same_title else "",
                        "标签" if not same_labels else "",
                        "重新打开" if reopened else "",
                    ]
                ).strip("、")
                or "无变化",
            )
        )
        updated += 1
        mapping[post["key"]] = {"issue": number}
        if args.check:
            continue
        gh.update_issue(number, post["title"], post["body"], label_names)

    closed = 0
    dropped = []
    for key in sorted(mapping):
        if key in current_keys:
            continue
        number = map_number(mapping[key])
        issue = issues_by_number.get(number) if number else None
        if number is None:
            dropped.append(key)
            continue
        if gh is not None and issue is None:
            log("[清理] %s 记录的是 Issue #%d，但它在 GitHub 上已经不存在了，从映射表移除" % (key, number))
            dropped.append(key)
            continue
        if issue is not None and issue.get("state") == "open":
            log("[删] %s 已不在仓库里 → 关闭 Issue #%d" % (key, number))
            closed += 1
            if not args.check:
                gh.close_issue(number)
        mapping[key] = {"issue": number}
    for key in dropped:
        mapping.pop(key, None)

    if new_labels:
        log("需要新建的标签：%s" % "、".join(sorted(new_labels)))
        if not args.check:
            for name in sorted(new_labels):
                try:
                    gh.create_label(name)
                except RuntimeError as exc:
                    log("警告：创建标签 %s 失败：%s" % (name, exc))

    if not args.check:
        save_map(map_path, mapping)
        log("映射表已更新 → %s" % args.map_path)

    log(
        "完成：新建 %d，更新 %d，未变 %d，关闭 %d，清理 %d%s"
        % (
            created,
            updated,
            unchanged,
            closed,
            len(dropped),
            "（检查模式，未写入）" if args.check else "",
        )
    )
    return 0


def load_single_page_names(root):
    path = root / "config.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(read_text(path))
    except ValueError:
        return set()
    names = data.get("singlePage") or []
    return set(str(name).strip() for name in names if str(name).strip())


# ------------------------------------------------------------------- assets

def run_assets(args):
    root = Path(__file__).resolve().parent.parent
    posts_dir = root / args.posts
    docs = Path(args.docs)
    if not docs.is_absolute():
        docs = root / docs
    if not docs.is_dir():
        log("找不到 docs 目录（%s），跳过图片同步" % args.docs)
        return 0
    out_dir = docs / IMAGE_OUT_DIR

    if not posts_dir.is_dir():
        log("没有找到 %s 目录，跳过图片同步" % args.posts)
        return 0

    images = LocalImages(root, args.site, args.posts)
    posts = collect_posts(posts_dir, images, args.posts)
    log("文章 %d 篇，引用本地图片 %d 张" % (len(posts), len(images.used)))
    for item in images.missing:
        log("警告：正文引用了不存在的图片 %s，这次保留原链接" % item)

    copied = 0
    for name in sorted(images.used):
        source = images.local_path(name)
        target = out_dir / name
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        copied += 1

    removed = 0
    if out_dir.is_dir():
        for path in sorted(out_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(out_dir).as_posix()
            if rel not in images.used:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass

    log("图片同步完成：复制 %d，清理 %d → %s/" % (copied, removed, IMAGE_OUT_DIR))
    return 0


# --------------------------------------------------------------------- 入口

def main():
    parser = argparse.ArgumentParser(description="本地 Markdown → GitHub Issue 同步")
    parser.add_argument("command", nargs="?", choices=("publish", "assets"), default="publish")
    parser.add_argument("target", nargs="?", help="assets 子命令的 docs 目录（等价于 --docs）")
    parser.add_argument("--posts", default=DEFAULT_POSTS_DIR, help="本地 md 目录（默认 posts）")
    parser.add_argument("--map", dest="map_path", default=DEFAULT_MAP_PATH, help="md ↔ Issue 映射表")
    parser.add_argument("--docs", default="docs", help="assets 子命令的输出目录")
    parser.add_argument("--site", default=os.environ.get("SITE_BASE_URL") or DEFAULT_SITE)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    parser.add_argument(
        "--api-base", default=os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    )
    parser.add_argument("--check", action="store_true", help="只检查并打印计划，不写入任何东西")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    args = parser.parse_args()

    if args.target and args.command == "assets":
        args.docs = args.target
    if args.command == "assets":
        return run_assets(args)
    return run_publish(args)


if __name__ == "__main__":
    sys.exit(main())
