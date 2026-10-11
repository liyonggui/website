# -*- coding: utf-8 -*-
"""
把 blog/ 与 deep/ 下的所有 .md 渲染成带全站样式的文章页 (.html)。
用法：python gen_articles.py
- 输出同名 .html（与 .md 同目录），原 .md 保留作为源。
- 自动生成 assets/css/pygments.css（代码高亮，github 浅色风）。
- blog/ 文章高亮「博客」导航；deep/ 文章高亮「深度」导航。
"""
import os
import re
import markdown
from pygments.formatters import HtmlFormatter

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLE_CSS = os.path.join(ROOT, "assets", "css", "article.css")
PYG_CSS = os.path.join(ROOT, "assets", "css", "pygments.css")

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>__TITLE__ · 小番茄程序猿</title>
<meta name="description" content="__TITLE__ —— 小番茄程序猿的技术文章。"/>
<link rel="icon" type="image/svg+xml" href="__PREFIX__assets/logo.svg" />
<meta property="og:type" content="website" />
<meta property="og:title" content="__TITLE__ · 小番茄程序猿" />
<meta property="og:description" content="__TITLE__ —— 小番茄程序猿的技术文章。" />
<meta property="og:url" content="https://liyonggui.github.io/website/" />
<meta property="og:image" content="https://liyonggui.github.io/website/assets/logo.svg" />
<link rel="stylesheet" href="__PREFIX__assets/css/style.css"/>
<link rel="stylesheet" href="__PREFIX__assets/css/article.css"/>
<link rel="stylesheet" href="__PREFIX__assets/css/pygments.css"/>
<script>
  (function () {
    try {
      var t = localStorage.getItem("theme");
      if (!t) t = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", t);
    } catch (e) {}
  })();
</script>
</head>
<body>
<header class="nav">
  <div class="wrap nav-inner">
    <a class="brand" href="__PREFIX__index.html">
      <img class="logo" src="__PREFIX__assets/logo.svg" alt="logo" />
      <span>小番茄程序猿<span class="dot">.</span>dev</span>
    </a>
    <div class="nav-right">
      <nav class="nav-links" id="navLinks">
        <a href="__PREFIX__index.html"__A_HOME__>首页</a>
        <a href="__PREFIX__projects.html"__A_PROJ__>项目</a>
        <a href="__PREFIX__blog.html"__A_BLOG__>博客</a>
        <a href="__PREFIX__deep.html"__A_DEEP__>深度</a>
      </nav>
      <button class="theme-toggle" id="themeToggle" aria-label="切换主题">🌙</button>
      <button class="nav-toggle" id="navToggle" aria-label="菜单">☰</button>
    </div>
  </div>
</header>
<div class="reading-bar"><i></i></div>
<main>
  <div class="article-head">
    <a class="back" href="__BACK_HREF__">← __BACK_LABEL__</a>
    <h1>__TITLE__</h1>
  </div>
  <article class="prose">
__BODY__
  </article>
  <section class="giscus-wrap">
    <div class="wrap">
      <h2>交流区</h2>
      <p class="sub">用 GitHub 账号即可留言、互相回复；评论托管在仓库 Discussions，无广告、无追踪。</p>
      <div id="giscus-comments"></div>
    </div>
  </section>
  <script src="__PREFIX__assets/js/comments.js?v=2"></script>
</main>
<footer>
  <div class="wrap">
    <span>© <span id="year"></span> 小番茄程序猿 · 全栈作品集与博客</span>
    <span>纯静态站点 · 可免费部署于 GitHub Pages</span>
  </div>
</footer>
<script>
  document.getElementById("year").textContent = new Date().getFullYear();
  var th = document.getElementById("themeToggle");
  function syncThemeIcon(){ th.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀" : "🌙"; }
  syncThemeIcon();
  th.addEventListener("click", function(){
    var nxt = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", nxt);
    try { localStorage.setItem("theme", nxt); } catch(e){}
    syncThemeIcon();
  });
  var t = document.getElementById("navToggle"), l = document.getElementById("navLinks");
  t.addEventListener("click", function(){ l.classList.toggle("open"); });
  l.querySelectorAll("a").forEach(function(a){ a.addEventListener("click", function(){ l.classList.remove("open"); }); });
  var rb = document.querySelector(".reading-bar i");
  function updProg(){
    var h = document.documentElement;
    var sc = h.scrollTop || document.body.scrollTop;
    var max = h.scrollHeight - h.clientHeight;
    rb.style.width = (max > 0 ? (sc / max * 100) : 0) + "%";
  }
  document.addEventListener("scroll", updProg, { passive: true });
  updProg();
</script>
</body>
</html>
"""


def depth_prefix(subdir):
    """subdir 相对 ROOT：'blog' -> '../', 'deep/mysql' -> '../../'"""
    return "../" * (1 + subdir.count("/"))


def build_html(prefix, title, body, active, back_href, back_label):
    a = {"home": "", "proj": "", "blog": "", "deep": ""}
    if active in a:
        a[active] = ' class="active"'
    return (TEMPLATE
            .replace("__PREFIX__", prefix)
            .replace("__TITLE__", title)
            .replace("__BODY__", body)
            .replace("__BACK_HREF__", back_href)
            .replace("__BACK_LABEL__", back_label)
            .replace("__A_HOME__", a["home"])
            .replace("__A_PROJ__", a["proj"])
            .replace("__A_BLOG__", a["blog"])
            .replace("__A_DEEP__", a["deep"]))


def get_title(md_text, fallback):
    m = re.search(r"^#\s+(.+?)\s*$", md_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback


def md_to_html(text):
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "footnotes", "codehilite"],
        extension_configs={
            "codehilite": {"guess_lang": False, "css_class": "codehilite"}
        },
    )
    return md.convert(text)


def ensure_pygments_css():
    if os.path.exists(PYG_CSS):
        return
    css = HtmlFormatter(style="native").get_style_defs(".codehilite")
    with open(PYG_CSS, "w", encoding="utf-8") as f:
        f.write(css)


def process_dir(rel_dir, active, back_label):
    abs_dir = os.path.join(ROOT, rel_dir)
    if not os.path.isdir(abs_dir):
        return 0
    prefix = depth_prefix(rel_dir)
    back_href = prefix + ("blog.html" if active == "blog" else "deep.html")
    count = 0
    for fn in sorted(os.listdir(abs_dir)):
        if not fn.endswith(".md"):
            continue
        src = os.path.join(abs_dir, fn)
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        title = get_title(text, fn[:-3])
        body = md_to_html(text)
        out_name = fn[:-3] + ".html"
        out_path = os.path.join(abs_dir, out_name)
        html = build_html(prefix, title, body, active, back_href, back_label)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        count += 1
    return count


def main():
    ensure_pygments_css()
    total = 0
    # 博客文章
    total += process_dir("blog", "blog", "返回博客索引")
    # 深度技术各系列
    total += process_dir("deep/mysql", "deep", "返回深度技术")
    total += process_dir("deep/redis", "deep", "返回深度技术")
    total += process_dir("deep/es", "deep", "返回深度技术")
    total += process_dir("deep/bench", "deep", "返回深度技术")
    total += process_dir("deep/k8s", "deep", "返回深度技术")
    total += process_dir("deep/agent", "deep", "返回深度技术")
    print("生成文章页总数:", total)
    print("pygments.css:", "已存在" if os.path.exists(PYG_CSS) else "已生成")


if __name__ == "__main__":
    main()
