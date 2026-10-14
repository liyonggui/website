/* 全站统一导航组件（单一数据源）
 * 页面只需占位：<nav class="nav-links" id="navLinks" data-prefix="" data-active="learn.html"></nav>
 *   data-prefix : 页面到站点根的相对前缀（文章页为 ../../ 之类，由生成器注入）
 *   data-active : 当前页所属栏目（对应下方 items 的链接名），缺省按文件名自动匹配
 *   data-lang   : "en" 时渲染英文栏目名（英文门面页 about-en.html 使用），缺省中文
 * 新增/调整栏目只改这里一份，然后重跑生成器。
 */
(function () {
  var nav = document.getElementById("navLinks");
  if (!nav) return;
  var prefix = nav.getAttribute("data-prefix") || "";
  var cur = (location.pathname.split("/").pop() || "").toLowerCase();
  if (!cur) cur = "index.html";
  var target = nav.getAttribute("data-active") || cur;
  var en = nav.getAttribute("data-lang") === "en" ? 1 : 0;
  var items = [
    ["index.html", "首页", "Home"],
    ["projects.html", "项目", "Projects"],
    ["blog.html", "博客", "Blog"],
    ["deep.html", "深度", "Deep Dives"],
    ["learn.html", "学习", "Learning"],
    ["community.html", "同行", "Community"],
    ["about.html", "关于", "About"]
  ];
  nav.innerHTML = items.map(function (it) {
    var cls = it[0] === target ? ' class="active"' : "";
    return '<a href="' + prefix + it[0] + '"' + cls + ">" + (en ? it[2] : it[1]) + "</a>";
  }).join("");
})();

/* 滚动吸顶收缩：滚过 24px 后页头变矮（CSS 0.4s 过渡，动画偏慢不突兀） */
(function () {
  var nav = document.querySelector(".nav");
  if (!nav) return;
  var ticking = false;
  function update() {
    var y = window.scrollY || document.documentElement.scrollTop || 0;
    nav.classList.toggle("scrolled", y > 24);
    ticking = false;
  }
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, { passive: true });
  update();
})();
