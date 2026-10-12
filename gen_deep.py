# -*- coding: utf-8 -*-
"""生成 deep.html：底层原理专栏索引（sticky 分类导航 + 文档卡片）。"""
import os, urllib.parse

from path_data import MYSQL_ORDER

ROOT = os.path.dirname(os.path.abspath(__file__))
DEEP = os.path.join(ROOT, "deep")

SERIES = [
    ("mysql", "MySQL 底层原理与治理",
     "事务隔离、锁机制、MVCC、索引、分库分表、读写分离、冷热分离——18 主场景 + 12 补测，外加 KingbaseES 锁与事务排查。", "M"),
    ("redis", "Redis 底层原理与压测",
     "数据类型底层结构（SDS / ziplist / skiplist 等）、架构演变，以及 11 个 k6 压测场景深挖（Pipeline、哨兵 vs 集群、三种 String 编码、热点 Key…）。", "R"),
    ("es", "Elasticsearch 底层与运维",
     "写入全流程、路由机制与 MurmurHash3 分片倾斜、ILM 索引生命周期与冷热分层、Rollover / Reindex / 别名回流全套。", "E"),
    ("bench", "高并发压测与方法论",
     "协同遗漏 Coordinated Omission、降级本地兜底与限速回放、Java 秒杀库存压测脚本设计、高并发压测执行清单。", "B"),
    ("k8s", "K8s / 云原生底层",
     "Redis 主从复制与哨兵、Ingress + HPA 弹性、滚动更新、compose vs k8s、K8s 学习路线图。", "K"),
    ("agent", "AI Agent 底层原理",
     "RAG / MCP / 护栏 / 评测的代码与原理详解，Agent 中间件选型，自研 Agent 工程化落地，phase1-6 阶段 README。", "A"),
]

SHORT = {"mysql": "MySQL", "redis": "Redis", "es": "Elasticsearch",
         "bench": "压测方法论", "k8s": "K8s 云原生", "agent": "Agent 原理"}

CORE = ["原理", "详解", "核心汇总", "实战手册", "架构演变", "数据类型原理", "写入全流程",
        "路由机制", "ILM", "Coordinated", "降级", "RAG代码", "MCP代码", "护栏代码",
        "评测代码", "中间件", "全链路", "工程化落地", "MVCC", "分库分表", "冷热分离",
        "事务隔离", "锁机制", "面试精华", "脑图速记", "总结汇总", "速查手册"]


def is_core(name):
    return any(k in name for k in CORE)


def disp(name):
    return name[:-3] if name.endswith(".md") else name


HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>深度技术 · 底层原理专栏 | 小番茄程序猿.dev</title>
  <meta name="description" content="MySQL / Redis / Elasticsearch 底层原理与治理、高并发压测方法论、K8s 底层、AI Agent 原理详解——项目背后的硬核底层知识库。" />
  <link rel="icon" type="image/svg+xml" href="assets/logo.svg" />
  <meta property="og:type" content="website" />
  <meta property="og:title" content="深度技术 · 底层原理专栏 | 小番茄程序猿.dev" />
  <meta property="og:description" content="数据库内核、压测方法论、云原生、AI Agent 原理——项目背后的硬核底层知识库。" />
  <meta property="og:url" content="https://liyonggui.github.io/website/deep.html" />
  <meta property="og:image" content="https://liyonggui.github.io/website/assets/logo.svg" />
  <link rel="stylesheet" href="assets/css/style.css?v=9" />
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
      <a class="brand" href="index.html">
        <img class="logo" src="assets/logo.svg" alt="logo" />
        <span>小番茄程序猿<span class="dot">.</span>dev</span>
      </a>
      <div class="nav-right">
        <nav class="nav-links" id="navLinks" data-prefix="" data-active="deep.html"></nav>
        <button class="theme-toggle" id="themeToggle" aria-label="切换主题">🌙</button>
        <button class="nav-toggle" id="navToggle" aria-label="菜单">☰</button>
      </div>
    </div>
  </header>

  <main>
    <section class="hero">
      <div class="wrap">
        <div class="eyebrow">底层原理 · 知识库</div>
        <h1>项目背后的<br />底层原理与方法论。</h1>
        <p class="lead">
          这里不是项目罗列，而是支撑那些项目跑起来、能量化的底层知识：数据库内核与治理、压测方法论、云原生底层，以及 AI Agent 的原理实现。
          每篇都已渲染为网页文章，点开即可阅读。
        </p>
        <div class="cta">
          <a class="btn btn-primary" href="#mysql">MySQL 底层 →</a>
          <a class="btn btn-ghost" href="#agent">Agent 原理</a>
        </div>
      </div>
    </section>

    <section class="first reveal">
      <div class="wrap">
        <div class="deep-layout">
          <aside class="deep-nav" id="deepNav">
            <h4>系列</h4>
"""

# 替换占位符用，避免与普通字符串里的 {} 冲突
GROUPS_PLACEHOLDER = "__GROUPS__"

TAIL = """        </aside>

        <div class="deep-content">
__GROUPS__
        </div>
      </div>
    </section>
  </main>

  <section class="giscus-wrap">
    <div class="wrap">
      <h2>交流区</h2>
      <p class="sub">对哪篇底层原理有疑问或想讨论，用 GitHub 账号留言即可；评论托管在仓库 Discussions，无广告、无追踪。</p>
      <div id="giscus-comments"></div>
    </div>
  </section>

  <script src="assets/js/nav.js?v=2"></script>
  <script src="assets/js/comments.js?v=2"></script>

  <footer>
    <div class="wrap">
      <span>© <span id="year"></span> 小番茄程序猿 · 全栈作品集与博客</span>
      <span class="foot-links"><a href="about.html">关于</a> · <a href="tags.html">标签</a> · <a href="archives.html">归档</a> · <a href="search.html">搜索</a> · <a href="rss.xml">RSS</a></span>
    </div>
  </footer>

  <script>
    document.getElementById("year").textContent = new Date().getFullYear();
    var th = document.getElementById("themeToggle");
    function syncThemeIcon() {
      th.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀" : "🌙";
    }
    syncThemeIcon();
    th.addEventListener("click", function () {
      var nxt = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", nxt);
      try { localStorage.setItem("theme", nxt); } catch (e) {}
      syncThemeIcon();
    });
    var t = document.getElementById("navToggle"), l = document.getElementById("navLinks");
    t.addEventListener("click", function () { l.classList.toggle("open"); });

    // 深度页 scroll-spy：高亮当前系列
    var navLinks = document.querySelectorAll("#deepNav a");
    var groups = document.querySelectorAll(".deep-group");
    if ("IntersectionObserver" in window && groups.length) {
      var spy = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (e.isIntersecting) {
            navLinks.forEach(function (a) { a.classList.toggle("active", a.dataset.target === e.target.id); });
          }
        });
      }, { rootMargin: "-20% 0px -70% 0px", threshold: 0 });
      groups.forEach(function (g) { spy.observe(g); });
    }
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (es) {
        es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
      }, { threshold: 0.08 });
      document.querySelectorAll(".reveal").forEach(function (el) { io.observe(el); });
    } else {
      document.querySelectorAll(".reveal").forEach(function (el) { el.classList.add("in"); });
    }
  </script>
</body>
</html>
"""

nav_html = []
groups_html = []
total = 0
for key, title, desc, ic in SERIES:
    d = os.path.join(DEEP, key)
    files = sorted([f for f in os.listdir(d) if f.endswith(".md") and not f.startswith(".")])
    if not files:
        continue
    count = len(files)
    total += count
    nav_html.append(
        '            <a href="#%s" data-target="%s"><span>%s</span><span class="cnt">%s</span></a>\n'
        % (key, key, SHORT[key], count))

    items = []
    for fn in files:
        href = "deep/%s/%s" % (key, urllib.parse.quote(fn[:-3] + ".html"))
        if fn == "README.md":
            label = "phase1 · 最小 Agent 工程 README" if key == "agent" else ("K3s 项目总览 README" if key == "k8s" else "项目总览 README")
        else:
            label = disp(fn)
        badge = ' <span class="badge">核心</span>' if is_core(fn) else ''
        items.append('              <a href="%s">%s%s</a>\n' % (href, label, badge))
    groups_html.append(
        '          <div class="deep-group" id="%s">\n'
        '            <h3><span class="ic">%s</span>%s%s</h3>\n'
        '            <div class="docs">\n%s            </div>\n'
        '          </div>\n'
        % (key, ic, title,
           ('<a class="group-link" href="%s">学习路线 →</a>'
            % {"mysql": "mysql-path.html", "redis": "redis-path.html",
               "es": "es-path.html", "k8s": "k8s-path.html"}[key]
            if key in ("mysql", "redis", "es", "k8s") else ""),
           "".join(items)))

html = HEAD + "".join(nav_html) + TAIL.replace(GROUPS_PLACEHOLDER, "".join(groups_html))
with open(os.path.join(ROOT, "deep.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("deep.html generated, total docs =", total)
