/* ===== 五环魔法师 前端增强 =====
 * 1. 所有页面：右下角「回到顶部」按钮
 * 2. 所有页面：正文图片点击放大（灯箱，支持 Esc / 点击空白关闭）
 * 3. 文章页：标题下注入 日期 + 标签 元信息条
 * 4. 文章页：正文后注入 同系列「上一篇 / 下一篇」导航
 * 数据来源：/postList.json（Gmeek 构建时生成）
 */
(function () {
    "use strict";

    var isPost = /^\/post\/.+\.html$/.test(location.pathname);

    function onReady(fn) {
        if (document.readyState !== "loading") fn();
        else document.addEventListener("DOMContentLoaded", fn);
    }

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    /* ---------- 回到顶部 ---------- */
    function initToTop() {
        var btn = document.createElement("button");
        btn.id = "toTop";
        btn.type = "button";
        btn.title = "回到顶部";
        btn.setAttribute("aria-label", "回到顶部");
        btn.innerHTML = "&uarr;";
        btn.addEventListener("click", function () {
            if (window.scrollTo) window.scrollTo({ top: 0, behavior: "smooth" });
            else window.scrollTo(0, 0);
        });
        document.body.appendChild(btn);

        var update = function () {
            if (window.pageYOffset > 600) btn.classList.add("show");
            else btn.classList.remove("show");
        };
        window.addEventListener("scroll", update, { passive: true });
        update();
    }

    /* ---------- 图片灯箱 ---------- */
    function captionOf(img) {
        var para = img.closest ? img.closest("p") : null;
        var next = para && para.nextElementSibling;
        if (next && next.tagName === "BLOCKQUOTE") {
            return (next.textContent || "").replace(/^[\s\u2191\u2b06>]+/, "").trim();
        }
        return "";
    }

    function initLightbox() {
        var overlay = document.createElement("div");
        overlay.className = "img-lightbox";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "图片查看");

        var big = document.createElement("img");
        big.alt = "";

        var close = document.createElement("button");
        close.className = "img-lightbox-close";
        close.type = "button";
        close.setAttribute("aria-label", "关闭图片");
        close.innerHTML = "&times;";

        var cap = document.createElement("div");
        cap.className = "img-lightbox-caption";

        overlay.appendChild(big);
        overlay.appendChild(close);
        overlay.appendChild(cap);
        document.body.appendChild(overlay);

        function hide() {
            overlay.classList.remove("show");
            document.body.classList.remove("lightbox-open");
        }

        overlay.addEventListener("click", hide);
        big.addEventListener("click", function (e) { e.stopPropagation(); });
        close.addEventListener("click", hide);
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") hide();
        });

        document.addEventListener("click", function (e) {
            var target = e.target;
            if (!target || target.tagName !== "IMG") return;
            if (!target.closest("#postBody")) return;
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; /* 保留「在新标签打开」 */

            e.preventDefault();
            var link = target.parentNode && target.parentNode.tagName === "A" ? target.parentNode : null;
            big.src = link && link.href ? link.href : target.src;
            big.alt = target.alt || "";
            var text = captionOf(target);
            cap.textContent = text;
            cap.style.display = text ? "block" : "none";
            overlay.classList.add("show");
            document.body.classList.add("lightbox-open");
        });
    }

    /* ---------- 文章页：元信息条 + 同系列上下篇 ---------- */
    function enhancePost() {
        fetch("/postList.json")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                /* 按 issue 编号升序 = 时间正序（P1 最早） */
                var posts = Object.keys(data)
                    .filter(function (k) { return /^P\d+$/.test(k); })
                    .map(function (k) {
                        var p = data[k];
                        p._num = parseInt(k.slice(1), 10);
                        return p;
                    })
                    .sort(function (a, b) { return a._num - b._num; });

                var curPath = decodeURIComponent(location.pathname);
                var idx = -1;
                for (var i = 0; i < posts.length; i++) {
                    if (curPath === decodeURIComponent("/" + posts[i].postUrl)) { idx = i; break; }
                }
                if (idx < 0) return;
                var cur = posts[idx];

                /* ---- 1) 元信息条 ---- */
                var h1 = document.querySelector("h1.postTitle");
                if (h1 && !document.querySelector(".post-meta")) {
                    var html = "\uD83D\uDCC5 " + esc(cur.createdDate || "");
                    (cur.labels || []).forEach(function (lb) {
                        html += ' <a class="post-label" href="/tag.html#' + encodeURIComponent(lb) + '">' + esc(lb) + "</a>";
                    });
                    var meta = document.createElement("div");
                    meta.className = "post-meta";
                    meta.innerHTML = html;
                    h1.parentNode.insertBefore(meta, h1.nextSibling);
                }

                /* ---- 2) 同系列上一篇 / 下一篇 ---- */
                var firstLabel = (cur.labels && cur.labels.length) ? cur.labels[0] : null;
                var series = firstLabel
                    ? posts.filter(function (p) { return p.labels && p.labels[0] === firstLabel; })
                    : posts;

                var sIdx = -1;
                for (var j = 0; j < series.length; j++) {
                    if (series[j]._num === cur._num) { sIdx = j; break; }
                }
                if (sIdx < 0) return;

                var prev = sIdx > 0 ? series[sIdx - 1] : null;                 // 更早的一篇
                var next = sIdx < series.length - 1 ? series[sIdx + 1] : null;  // 更新的一篇
                if (!prev && !next) return;

                var postBody = document.getElementById("postBody");
                if (!postBody || !postBody.parentNode) return;
                if (document.querySelector(".prev-next")) return;

                var nav = document.createElement("div");
                nav.className = "prev-next";

                var prevHtml = '<div class="pn-cell pn-prev">';
                if (prev) {
                    prevHtml += '<span class="pn-hint">← 上一篇</span>' +
                        '<a class="pn-title" href="/' + prev.postUrl + '">' + esc(prev.postTitle) + "</a>";
                }
                prevHtml += "</div>";

                var nextHtml = '<div class="pn-cell pn-next">';
                if (next) {
                    nextHtml += '<span class="pn-hint">下一篇 →</span>' +
                        '<a class="pn-title" href="/' + next.postUrl + '">' + esc(next.postTitle) + "</a>";
                }
                nextHtml += "</div>";

                nav.innerHTML = prevHtml + nextHtml;
                postBody.parentNode.insertBefore(nav, postBody.nextSibling);
            })
            .catch(function (e) {
                console.warn("custom.js: postList.json 加载失败", e);
            });
    }

    /* ---------- 分享本页 ---------- */
    function shareTitle() {
        // 优先用页面自己的标题（文章页 / 收藏页 / 笔记页的 h1），首页没有 h1 就用站点标题
        var node = document.querySelector("h1.postTitle") || document.querySelector("#content h1");
        var title = node && node.textContent ? node.textContent.trim() : "";
        if (!title) title = (document.title || "").trim();
        return title.replace(/[*`]/g, "").replace(/\s+/g, " ").trim();
    }

    function fallbackCopy(text) {
        try {
            var area = document.createElement("textarea");
            area.value = text;
            area.setAttribute("readonly", "");
            area.style.position = "fixed";
            area.style.top = "-1000px";
            document.body.appendChild(area);
            area.select();
            var ok = document.execCommand("copy");
            document.body.removeChild(area);
            return ok;
        } catch (e) {
            return false;
        }
    }

    function copyText(text) {
        return new Promise(function (resolve) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(
                    function () { resolve(true); },
                    function () { resolve(fallbackCopy(text)); }
                );
            } else {
                resolve(fallbackCopy(text));
            }
        });
    }

    function initShare() {
        if (document.getElementById("shareBtn")) return;

        var url = location.href;
        var title = shareTitle();
        var isMobile = /Android|iPhone|iPad|iPod|Mobile|HarmonyOS|MicroMessenger/i.test(navigator.userAgent);
        var canNative = typeof navigator.share === "function";

        var btn = document.createElement("button");
        btn.id = "shareBtn";
        btn.type = "button";
        btn.title = "分享本页";
        btn.setAttribute("aria-label", "分享本页");
        btn.innerHTML =
            '<svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">' +
            '<path fill="currentColor" d="M11 2.5a2.5 2.5 0 1 1 .78 1.81l-4.4 2.2a2.5 2.5 0 0 1 0 1.98l4.4 2.2a2.5 2.5 0 1 1-.6 1.27l-4.4-2.2a2.5 2.5 0 1 1 0-4.52l4.4-2.2A2.5 2.5 0 0 1 11 2.5Zm-7.5 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2Zm7.5-4a1 1 0 1 0 0 2 1 1 0 0 0 0-2Zm0 9a1 1 0 1 0 0 2 1 1 0 0 0 0-2Z"/>' +
            "</svg>";
        document.body.appendChild(btn);

        var panel = document.createElement("div");
        panel.id = "sharePanel";
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-label", "分享本页");
        panel.innerHTML =
            '<div class="share-head">分享本页</div>' +
            '<button class="share-item" type="button" data-act="copyLink"><span>📋</span>复制链接</button>' +
            '<button class="share-item" type="button" data-act="copyTitle"><span>🔗</span>复制标题 + 链接</button>' +
            '<a class="share-item" data-act="x" target="_blank" rel="noopener noreferrer"><span>𝕏</span>分享到 X</a>' +
            '<a class="share-item" data-act="weibo" target="_blank" rel="noopener noreferrer"><span>微博</span>分享到微博</a>' +
            '<a class="share-item" data-act="mail"><span>✉️</span>邮件分享</a>' +
            (canNative ? '<button class="share-item" type="button" data-act="native"><span>📱</span>系统分享</button>' : "") +
            '<div class="share-hint" id="shareHint"></div>';
        document.body.appendChild(panel);

        var hint = panel.querySelector("#shareHint");
        var hintTimer = null;

        function say(text) {
            if (!hint) return;
            hint.textContent = text;
            if (hintTimer) clearTimeout(hintTimer);
            hintTimer = setTimeout(function () { hint.textContent = ""; }, 2400);
        }

        function close() { panel.classList.remove("show"); }

        function open() {
            var x = panel.querySelector('[data-act="x"]');
            var weibo = panel.querySelector('[data-act="weibo"]');
            var mail = panel.querySelector('[data-act="mail"]');
            if (x) x.href = "https://twitter.com/intent/tweet?text=" + encodeURIComponent(title) + "&url=" + encodeURIComponent(url);
            if (weibo) weibo.href = "https://service.weibo.com/share/share.php?title=" + encodeURIComponent(title) + "&url=" + encodeURIComponent(url);
            if (mail) mail.href = "mailto:?subject=" + encodeURIComponent(title) + "&body=" + encodeURIComponent(url);
            panel.classList.add("show");
        }

        function nativeShare() {
            return navigator.share({ title: title, url: url }).catch(function (err) {
                if (err && err.name === "AbortError") return true;
                return false;
            });
        }

        btn.addEventListener("click", function () {
            if (isMobile && canNative) {
                nativeShare().then(function (ok) { if (!ok) open(); });
                return;
            }
            if (panel.classList.contains("show")) close();
            else open();
        });

        panel.addEventListener("click", function (event) {
            var node = event.target.closest("[data-act]");
            if (!node) return;
            var act = node.getAttribute("data-act");
            if (act === "copyLink") {
                copyText(url).then(function (ok) { say(ok ? "链接已复制 ✓" : "复制失败，请手动复制"); });
            } else if (act === "copyTitle") {
                copyText(title + "\n" + url).then(function (ok) { say(ok ? "标题和链接已复制 ✓" : "复制失败，请手动复制"); });
            } else if (act === "native") {
                nativeShare();
            } else {
                close();
            }
        });

        document.addEventListener("click", function (event) {
            if (!panel.classList.contains("show")) return;
            if (panel.contains(event.target) || btn.contains(event.target)) return;
            close();
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") close();
        });
    }

    /* ---------- 莫斯科生存攻略：只给这一页加样式钩子和页内跳转 ---------- */
    function initMoscowPage() {
        if (!/quest\s*in\s*moscow\.html$/i.test(decodeURI(location.pathname))) return;
        document.body.classList.add("page-moscow");

        var content = document.getElementById("content");
        var body = content && content.querySelector(".markdown-body");
        if (!body) return;

        var heads = Array.prototype.slice.call(body.querySelectorAll("h1, h2"));
        if (heads.length < 2) return;

        var toc = document.createElement("div");
        toc.className = "moscow-toc";
        var label = document.createElement("span");
        label.className = "moscow-toc-title";
        label.textContent = "快速跳转";
        toc.appendChild(label);

        heads.forEach(function (head, index) {
            var id = "moscow-" + (index + 1);
            head.id = id;
            var link = document.createElement("a");
            link.href = "#" + id;
            link.textContent = head.textContent.trim();
            toc.appendChild(link);
        });

        body.parentNode.insertBefore(toc, body);
    }

    /* ---------- 评论区自动展开（滚动到附近才加载） ---------- */
    // 评论组件来自 GitHub（utteranc.es + api.github.com），首屏就加载会让文章页
    // 多背两个跨域请求。这里改成滚动到评论区上方约 400px 时再拉起。
    function initAutoComments() {
        var button = document.getElementById("cmButton");
        var box = document.getElementById("comments");
        if (!button || !box) return;
        if (box.querySelector("iframe")) return;            // 已经加载过了
        if (typeof window.openComments !== "function") return;  // 页面没有加载器就不管

        function load() {
            if (box.querySelector("iframe")) return;
            // 评论区上方的提示：想回复某人就用 @
            if (!box.querySelector(".cm-hint")) {
                var hint = document.createElement("p");
                hint.className = "cm-hint";
                hint.textContent = "想回复某条评论：在评论框里写 @对方的用户名，对方就会收到 GitHub 通知。";
                box.appendChild(hint);
            }
            try {
                window.openComments();
            } catch (error) {
                return;                                      // 失败就让读者自己点按钮
            }
            button.style.display = "none";                   // 评论已经展开，按钮不再需要
        }

        if ("IntersectionObserver" in window) {
            var observer = new IntersectionObserver(function (entries) {
                for (var i = 0; i < entries.length; i++) {
                    if (entries[i].isIntersecting) {
                        observer.disconnect();
                        load();
                        return;
                    }
                }
            }, { rootMargin: "400px 0px" });
            // #comments 此时是空的（高度 0），换成有高度的按钮当触发点更稳
            var trigger = box.getBoundingClientRect().height ? box : button;
            observer.observe(trigger);
        } else {
            load();                                          // 老浏览器直接加载，行为跟以前一样
        }
    }

    onReady(function () {
        initToTop();
        initLightbox();
        initShare();
        initAutoComments();
        initMoscowPage();
        if (isPost) enhancePost();
    });
})();
