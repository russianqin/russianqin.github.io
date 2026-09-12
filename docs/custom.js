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

    onReady(function () {
        initToTop();
        initLightbox();
        if (isPost) enhancePost();
    });
})();
