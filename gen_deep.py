import os, urllib.parse

ROOT = "D:/personal-website"
DEEP = os.path.join(ROOT, "deep")

SERIES = [
    ("mysql", "MySQL 底层原理与治理",
     "事务隔离、锁机制、MVCC、索引、分库分表、读写分离、冷热分离——18 主场景 + 12 补测，外加 KingbaseES 锁与事务排查。"),
    ("redis", "Redis 底层原理与压测",
     "数据类型底层结构（SDS / ziplist / skiplist 等）、架构演变，以及 11 个 k6 压测场景深挖（Pipeline、哨兵 vs 集群、三种 String 编码、热点 Key…）。"),
    ("es", "Elasticsearch 底层与运维",
     "写入全流程、路由机制与 MurmurHash3 分片倾斜、ILM 索引生命周期与冷热分层、Rollover / Reindex / 别名回流全套。"),
    ("bench", "高并发压测与方法论",
     "协同遗漏 Coordinated Omission、降级本地兜底与限速回放、Java 秒杀库存压测脚本设计、高并发压测执行清单。"),
    ("k8s", "K8s / 云原生底层",
     "Redis 主从复制与哨兵、Ingress + HPA 弹性、滚动更新、compose vs k8s、K8s 学习路线图。"),
    ("agent", "AI Agent 底层原理",
     "RAG / MCP / 护栏 / 评测的代码与原理详解，Agent 中间件选型，rdx-agent 工程化落地，phase1-6 阶段 README。"),
]

CORE = ["原理", "详解", "核心汇总", "实战手册", "架构演变", "数据类型原理", "写入全流程",
        "路由机制", "ILM", "Coordinated", "降级", "RAG代码", "MCP代码", "护栏代码",
        "评测代码", "中间件", "全链路", "工程化落地", "MVCC", "分库分表", "冷热分离",
        "事务隔离", "锁机制", "面试精华", "脑图速记", "总结汇总", "速查手册"]


def is_core(name):
    return any(k in name for k in CORE)


def disp(name):
    return name[:-3] if name.endswith(".md") else name


parts = []
parts.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>深度技术 · 底层原理专栏 | 小番茄程序猿.dev</title>
  <meta name="description" content="MySQL / Redis / Elasticsearch 底层原理与治理、高并发压测方法论、K8s 底层、AI Agent 原理详解——项目背后的硬核底层知识库。" />
  <link rel="stylesheet" href="assets/css/style.css" />
  <link rel="stylesheet" href="assets/css/deep.css" />
</head>
<body>
  <header class="nav">
    <div class="wrap nav-inner">
      <div class="brand">小番茄程序猿<span>.</span>dev</div>
      <nav class="nav-links" id="navLinks">
        <a href="index.html">首页</a>
        <a href="projects.html">项目</a>
        <a href="blog.html">博客</a>
        <a href="deep.html" class="active">深度</a>
      </nav>
      <button class="nav-toggle" id="navToggle" aria-label="菜单">☰</button>
    </div>
  </header>

  <main>
    <section class="hero">
      <div class="wrap">
        <div class="eyebrow">底层原理 · 知识库</div>
        <h1>项目背后的<br />底层原理与方法论。</h1>
        <p class="lead">
          这里不是项目罗列，而是支撑那些项目跑起来、能量化的底层知识：数据库内核与治理、压测方法论、云原生底层，以及 AI Agent 的原理实现。
          每篇都是可打开的 Markdown 原文，建议在桌面端阅读。
        </p>
        <div class="cta">
          <a class="btn btn-primary" href="#mysql">MySQL 底层 →</a>
          <a class="btn btn-ghost" href="#agent">Agent 原理</a>
        </div>
      </div>
    </section>
""")

total = 0
for key, title, desc in SERIES:
    d = os.path.join(DEEP, key)
    files = sorted([f for f in os.listdir(d) if f.endswith(".md") and not f.startswith(".")])
    if not files:
        continue
    total += len(files)
    parts.append(f'    <section id="{key}">\n      <div class="wrap">\n')
    parts.append(f'        <div class="section-head">\n          <h2>{title}</h2>\n          <p>{desc}</p>\n        </div>\n')
    parts.append('        <ul class="doc-list">\n')
    for fn in files:
        href = f"deep/{key}/{urllib.parse.quote(fn)}"
        if fn == "README.md":
            label = "phase1 · 最小 Agent 工程 README" if key == "agent" else ("K3s 项目总览 README" if key == "k8s" else "项目总览 README")
        else:
            label = disp(fn)
        badge = ' <span class="badge core">核心</span>' if is_core(fn) else ''
        parts.append(f'          <li><a href="{href}" target="_blank" rel="noopener">{label}</a>{badge}</li>\n')
    parts.append('        </ul>\n      </div>\n    </section>\n')

parts.append(f"""  </main>

  <section class="giscus-wrap">
    <div class="wrap">
      <h2>交流区</h2>
      <p class="sub">对哪篇底层原理有疑问或想讨论，用 GitHub 账号留言即可；评论托管在仓库 Discussions，无广告、无追踪。</p>
      <div id="giscus-comments"></div>
    </div>
  </section>

  <script src="assets/js/comments.js"></script>

  <footer>
    <div class="wrap">
      <span>© <span id="year"></span> 小番茄程序猿 · 全栈作品集与博客</span>
      <span>纯静态站点 · 可免费部署于 GitHub Pages</span>
    </div>
  </footer>

  <script>
    document.getElementById("year").textContent = new Date().getFullYear();
    var t = document.getElementById("navToggle"), l = document.getElementById("navLinks");
    t.addEventListener("click", function () {{ l.classList.toggle("open"); }});
    l.querySelectorAll("a").forEach(function (a) {{ a.addEventListener("click", function () {{ l.classList.remove("open"); }}); }});
  </script>
</body>
</html>
""")

with open(os.path.join(ROOT, "deep.html"), "w", encoding="utf-8") as f:
    f.write("".join(parts))
print("deep.html generated, total docs =", total)
