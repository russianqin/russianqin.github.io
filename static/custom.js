/* ===== 五环魔法师 文章页增强 =====
 * 功能：
 * 1. 在文章标题下注入 日期 + 标签 元信息条
 * 2. 在正文后注入 同系列「上一篇 / 下一篇」导航
 * 数据来源：/postList.json（Gmeek 构建时生成）
 */
(function () {
    "use strict";

    // 仅在文章页运行（/post/xxx.html）
    if (!/^\/post\/.+\.html$/.test(location.pathname)) return;

    function onReady(fn) {
        if (document.readyState !== "loading") fn();
        else document.addEventListener("DOMContentLoaded", fn);
    }

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    onReady(function () {
        fetch("/postList.json")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                // 按 issue 编号升序 = 时间正序（P1 最早）
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
                if (h1) {
                    var html = "📅 " + esc(cur.createdDate || "");
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

                var prev = sIdx > 0 ? series[sIdx - 1] : null;              // 更早的一篇
                var next = sIdx < series.length - 1 ? series[sIdx + 1] : null; // 更新的一篇
                if (!prev && !next) return;

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

                var postBody = document.getElementById("postBody");
                if (postBody && postBody.parentNode) {
                    postBody.parentNode.insertBefore(nav, postBody.nextSibling);
                }
            })
            .catch(function (e) {
                console.warn("custom.js: postList.json 加载失败", e);
            });
    });
})();
