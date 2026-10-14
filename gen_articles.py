# -*- coding: utf-8 -*-
"""
把 blog/ 与 deep/ 下的所有 .md 渲染成带全站样式的文章页 (.html)。
用法：python gen_articles.py
- 输出同名 .html（与 .md 同目录），原 .md 保留作为源。
- 自动生成 assets/css/pygments.css（代码高亮，github 浅色风）。
- blog/ 文章高亮「博客」导航；deep/ 文章高亮「深度」导航。
"""
import html
import json
import os
import re
import urllib.parse

import markdown
from pygments.formatters import HtmlFormatter

from path_data import MYSQL_ORDER, REDIS_ORDER, ES_ORDER, K8S_ORDER, AGENT_ORDER
import doc_roles

try:
    import yaml
except ImportError:
    yaml = None

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLE_CSS = os.path.join(ROOT, "assets", "css", "article.css")
PYG_CSS = os.path.join(ROOT, "assets", "css", "pygments.css")
SITE_BASE = "https://liyonggui.github.io/website/"

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>__TITLE__ · 小番茄程序猿</title>
<meta name="description" content="__DESC__"/>
<meta name="keywords" content="__KEYWORDS__"/>
<link rel="canonical" href="__CANONICAL__"/>
<link rel="alternate" type="application/rss+xml" title="RSS" href="__PREFIX__rss.xml" />
<link rel="icon" type="image/svg+xml" href="__PREFIX__assets/logo.svg" />
<meta property="og:type" content="article" />
<meta property="og:title" content="__TITLE__ · 小番茄程序猿" />
<meta property="og:description" content="__DESC__" />
<meta property="og:url" content="__CANONICAL__" />
<meta property="og:image" content="https://liyonggui.github.io/website/assets/logo.svg" />
<script type="application/ld+json">__JSONLD__</script>
<link rel="stylesheet" href="__PREFIX__assets/css/style.css?v=11"/>
<link rel="stylesheet" href="__PREFIX__assets/css/article.css?v=2"/>
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
      <nav class="nav-links" id="navLinks" data-prefix="__PREFIX__" data-active="__ACTIVE__"></nav>
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
__META__
  </div>
  <div class="article-layout">
    <article class="prose">
__BODY__
    </article>
    <aside class="toc-side">__TOC__</aside>
  </div>
__PATHNAV__
__RELATED__
  <section class="giscus-wrap">
    <div class="wrap">
      <h2>交流区</h2>
      <p class="sub">用 GitHub 账号即可留言、互相回复；评论托管在仓库 Discussions，无广告、无追踪。</p>
      <div id="giscus-comments"></div>
    </div>
  </section>
  <script src="__PREFIX__assets/js/nav.js?v=4"></script>
  <script src="__PREFIX__assets/js/comments.js?v=2"></script>
</main>
<footer>
  <div class="wrap">
    <span>© <span id="year"></span> 小番茄程序猿 · 全栈作品集与博客</span>
    <span class="foot-links"><a href="__PREFIX__about.html">关于</a> · <a href="__PREFIX__tags.html">标签</a> · <a href="__PREFIX__archives.html">归档</a> · <a href="__PREFIX__search.html">搜索</a> · <a href="__PREFIX__rss.xml">RSS</a></span>
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


def parse_front_matter(text):
    """解析 YAML front matter；无/解析失败返回 ({}, 原文)。"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    front = text[4:end]
    body = text[end + len("\n---\n"):]
    if yaml is None:
        return {}, body
    try:
        data = yaml.safe_load(front) or {}
        if not isinstance(data, dict):
            return {}, body
        return data, body
    except Exception:
        return {}, body


def build_meta_row(meta, title, prefix):
    """文章页头部的元信息行：日期 · 系列 · 阅读时长 · 标签（链到标签页）。"""
    if not meta:
        return ""
    parts = []
    if meta.get("date"):
        parts.append(f'<span class="m-item">{html.escape(str(meta["date"]))}</span>')
    if meta.get("series"):
        parts.append(f'<span class="m-item">{html.escape(str(meta["series"]))} 系列</span>')
    if meta.get("reading_time"):
        try:
            parts.append(f'<span class="m-item">约 {int(meta["reading_time"])} 分钟阅读</span>')
        except (TypeError, ValueError):
            pass
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if tags:
        chips = "".join(
            f'<a class="m-tag" href="{prefix}tags.html?t={html.escape(str(t), quote=True)}">{html.escape(str(t))}</a>'
            for t in tags
        )
        parts.append(f'<span class="m-tags">{chips}</span>')
    meta_row = '<div class="meta-row">' + "".join(parts) + "</div>" if parts else ""
    role = meta.get("role")
    if role:
        cls, label, note = role
        meta_row += (f'<div class="doc-role"><span class="role-badge {cls}">{html.escape(label)}</span>'
                     f'<span class="role-note">{html.escape(note)}</span></div>')
    return meta_row


def depth_prefix(subdir):
    """subdir 相对 ROOT：'blog' -> '../', 'deep/mysql' -> '../../'"""
    return "../" * (1 + subdir.count("/"))


def build_html(prefix, title, body, active, back_href, back_label, toc="", meta=None,
               canonical="", keywords="", jsonld="", related_html="", desc="", pathnav=""):
    a = {"home": "", "proj": "", "blog": "", "deep": "", "about": "", "learn": ""}
    if active in a:
        a[active] = ' class="active"'
    active_map = {"home": "index.html", "proj": "projects.html", "blog": "blog.html",
                  "deep": "deep.html", "learn": "learn.html", "about": "about.html"}
    meta = meta or {}
    if not desc:
        desc = str(meta.get("summary") or "").strip() or f"{title} —— 小番茄程序猿的技术文章"
    return (TEMPLATE
            .replace("__PREFIX__", prefix)
            .replace("__ACTIVE__", active_map.get(active, ""))
            .replace("__TITLE__", html.escape(title))
            .replace("__DESC__", html.escape(desc))
            .replace("__KEYWORDS__", html.escape(keywords, quote=True))
            .replace("__CANONICAL__", canonical)
            .replace("__JSONLD__", jsonld)
            .replace("__META__", build_meta_row(meta, title, prefix))
            .replace("__BODY__", body)
            .replace("__TOC__", toc)
            .replace("__PATHNAV__", pathnav)
            .replace("__RELATED__", related_html)
            .replace("__BACK_HREF__", back_href)
            .replace("__BACK_LABEL__", back_label)
            .replace("__A_HOME__", a["home"])
            .replace("__A_PROJ__", a["proj"])
            .replace("__A_BLOG__", a["blog"])
            .replace("__A_DEEP__", a["deep"])
            .replace("__A_ABOUT__", a["about"])
            .replace("__A_LEARN__", a["learn"]))


def get_title(md_text, fallback):
    m = re.search(r"^#\s+(.+?)\s*$", md_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback


def extract_title(text, fallback):
    """取文档最前面的 h1/h2 标题作为页头标题，并从正文移除该行，
    避免正文再渲染一次造成标题重复。
    - `# 标题` 或 `## 标题` 均可（有的文档首行写的是 ##）。
    - 仅当文档第一个非空行就是标题时才剔除；否则保持原样。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines[:10]):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", s)
        if m:
            t = re.sub(r"\s*#+\s*$", "", m.group(1)).strip()
            # 部分原文标题带 markdown 转义（如 ES7\.17、\+），页头是纯文本需还原
            t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", t)
            rest = "\n".join(lines[:i] + lines[i + 1:]).lstrip("\n")
            return t, rest
        break  # 首个非空行不是标题：不动正文
    return fallback, text


def demote_h1(text):
    """把正文剩余的 `# ` 标题降级为 `## `（页头已有 h1，正文不应再出现 h1）。
    跳过 ``` 代码围栏内的行，避免改动 shell 注释等内容。"""
    out = []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code and re.match(r"^#\s+", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def _toc_items(tokens):
    """从 toc_tokens 全递归抽取条目（文档序）-> [(level, id, name)]。
    level 1 视作 2（正文 h1 会在输出层降级为 h2，目录同步收录）。"""
    items = []

    def walk(toks):
        for tk in toks:
            lv = tk["level"]
            if lv in (1, 2, 3):
                items.append((2 if lv == 1 else lv, tk["id"], tk["name"]))
            walk(tk.get("children", []))

    walk(tokens)
    return items


def build_toc(md):
    """生成 TOC HTML；标题太少（h2 < 2）则返回空串，不显示目录。"""
    items = _toc_items(md.toc_tokens)
    h2 = sum(1 for lv, _, _ in items if lv == 2)
    if h2 < 2:
        return ""
    lis = []
    for lv, hid, name in items:
        cls = "lv2" if lv == 2 else "lv3"
        lis.append(
            f'      <li class="{cls}"><a href="#{hid}">{html.escape(name)}</a></li>'
        )
    return (
        '<details class="toc" open>\n'
        '  <summary>目录</summary>\n'
        '  <nav>\n'
        '    <ul>\n' + "\n".join(lis) + '\n    </ul>\n'
        '  </nav>\n'
        '</details>'
    )


def md_to_html(text):
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "footnotes", "codehilite"],
        extension_configs={
            "codehilite": {"guess_lang": False, "css_class": "codehilite"}
        },
    )
    body = md.convert(text)
    # 兜底：个别文档代码围栏不配对，md 层降级会漏；输出层强制正文无 h1。
    # toc 扩展同样给 h1 分配了 id，替换后锚点仍然有效。
    body = body.replace("<h1", "<h2").replace("</h1>", "</h2>")
    return body, build_toc(md)


def ensure_pygments_css():
    if os.path.exists(PYG_CSS):
        return
    css = HtmlFormatter(style="native").get_style_defs(".codehilite")
    with open(PYG_CSS, "w", encoding="utf-8") as f:
        f.write(css)


ARTICLE_DIRS = ["blog", "deep/mysql", "deep/redis", "deep/es", "deep/bench", "deep/k8s", "deep/agent", "deep/prj"]


def default_series(rel_dir):
    if rel_dir == "blog":
        return "博客"
    return rel_dir.split("/")[1]


def scan_catalog():
    """扫描全部 md 的 front matter，构建文章目录（供相关文章推荐使用）。"""
    entries = []
    for rel_dir in ARTICLE_DIRS:
        abs_dir = os.path.join(ROOT, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for fn in sorted(os.listdir(abs_dir)):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(abs_dir, fn), encoding="utf-8") as f:
                text = f.read()
            meta, _ = parse_front_matter(text)
            if str(meta.get("draft", "")).strip().lower() in ("true", "yes", "1"):
                continue
            tags = meta.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            entries.append({
                "rel_url": rel_dir + "/" + fn[:-3] + ".html",
                "title": str(meta.get("title") or fn[:-3]).strip(),
                "series": str(meta.get("series") or default_series(rel_dir)),
                "tags": [str(t) for t in tags],
                "date": str(meta.get("date") or ""),
            })
    return entries


def find_related(catalog, rel_url, series, tags, limit=5):
    """相关文章推荐：同系列 +3，每个共同标签 +2；不足时回退同系列，再回退最新文章。"""
    own_tags = set(tags)
    scored = []
    for e in catalog:
        if e["rel_url"] == rel_url:
            continue
        score = 0
        if e["series"] == series:
            score += 3
        score += 2 * len(own_tags & set(e["tags"]))
        if score > 0:
            scored.append((score, e["date"], e))
    if not scored:
        same = [e for e in catalog if e["series"] == series and e["rel_url"] != rel_url]
        scored = [(1, e["date"], e) for e in same]
        if not scored:
            scored = [(0, e["date"], e) for e in catalog if e["rel_url"] != rel_url]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e for _, _, e in scored[:limit]]


def build_related_html(related, prefix):
    if not related:
        return ""
    lis = []
    for e in related:
        lis.append(
            f'        <li><a href="{prefix}{e["rel_url"]}">'
            f'<span class="rel-title">{html.escape(e["title"])}</span>'
            f'<span class="rel-meta">{html.escape(e["series"])} 系列</span></a></li>'
        )
    return (
        '  <section class="related">\n'
        '    <div class="wrap">\n'
        '      <h3>相关文章</h3>\n'
        '      <ul class="related-list">\n' + "\n".join(lis) + '\n      </ul>\n'
        '    </div>\n'
        '  </section>\n'
    )


# 学习线路导航配置：目录 -> (ORDER, 线路页, 线路名)
PATH_NAV_CONF = {
    "deep/mysql": (MYSQL_ORDER, "mysql-path.html", "MySQL 学习线路"),
    "deep/redis": (REDIS_ORDER, "redis-path.html", "Redis 学习线路"),
    "deep/es": (ES_ORDER, "es-path.html", "ES 学习线路"),
    "deep/k8s": (K8S_ORDER, "k8s-path.html", "K8s 学习线路"),
    "deep/agent": (AGENT_ORDER, "agent-path.html", "AI Agent 学习线路"),
}


def build_path_nav(idx, path_titles, prefix, dir_key):
    """学习线路的上一站/下一站导航（idx 从 0 起）。"""
    order, path_page, path_label = PATH_NAV_CONF[dir_key]
    total = len(order)
    stem = order[idx]
    prev_stem = order[idx - 1] if idx > 0 else None
    next_stem = order[idx + 1] if idx < total - 1 else None

    def _link(stem_, cls, dir_txt, fallback):
        t = path_titles.get(dir_key + "/" + stem_ + ".html", stem_)
        href = prefix + dir_key + "/" + urllib.parse.quote(stem_ + ".html")
        return (f'<a class="{cls}" href="{href}">'
                f'<span class="pn-dir">{dir_txt}</span>'
                f'<span class="pn-title">{html.escape(t or fallback)}</span></a>')

    if prev_stem:
        prev_html = _link(prev_stem, "pn-prev", "← 上一站", "上一站")
    else:
        prev_html = (f'<a class="pn-prev pn-home" href="{prefix}{path_page}">'
                     f'<span class="pn-dir">← 上一站</span>'
                     f'<span class="pn-title">线路总览</span></a>')
    if next_stem:
        next_html = _link(next_stem, "pn-next", "下一站 →", "下一站")
    else:
        next_html = (f'<a class="pn-next pn-home" href="{prefix}{path_page}">'
                     f'<span class="pn-dir">下一站 →</span>'
                     f'<span class="pn-title">回到线路总览</span></a>')
    return (
        '  <nav class="path-nav"><div class="wrap">'
        f'<span class="pn-label">{path_label} · 第 {idx + 1}/{total} 站</span>'
        f'<div class="pn-links">{prev_html}{next_html}</div>'
        '</div></nav>\n'
    )


def build_jsonld(title, desc, rel_url, meta, tags, series):
    data = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": title,
        "description": desc,
        "url": SITE_BASE + rel_url,
        "mainEntityOfPage": SITE_BASE + rel_url,
        "author": {"@type": "Person", "name": "小番茄程序猿"},
        "publisher": {"@type": "Person", "name": "小番茄程序猿"},
        "image": SITE_BASE + "assets/logo.svg",
        "inLanguage": "zh-CN",
    }
    if meta.get("date"):
        data["datePublished"] = str(meta["date"])
    kws = tags + ([series] if series else [])
    if kws:
        data["keywords"] = ", ".join(dict.fromkeys(kws))
    # 转义 < 防止提前闭合 script 标签
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def process_dir(rel_dir, active, back_label, catalog):
    abs_dir = os.path.join(ROOT, rel_dir)
    if not os.path.isdir(abs_dir):
        return 0
    prefix = depth_prefix(rel_dir)
    back_href = prefix + ("blog.html" if active == "blog" else "projects.html" if active == "proj" else "deep.html")
    count = 0
    for fn in sorted(os.listdir(abs_dir)):
        if not fn.endswith(".md"):
            continue
        src = os.path.join(abs_dir, fn)
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        meta, text = parse_front_matter(text)
        role = doc_roles.role_for(rel_dir, fn[:-3])
        if role:
            meta["role"] = role
        if str(meta.get("draft", "")).strip().lower() in ("true", "yes", "1"):
            # 草稿不生成公开 HTML（索引同样排除）；若此前发布过，清除残留页面
            stale = os.path.join(abs_dir, fn[:-3] + ".html")
            if os.path.exists(stale):
                os.remove(stale)
                print("草稿下线，已删除残留 HTML:", os.path.relpath(stale, ROOT))
            continue
        heading, text = extract_title(text, fn[:-3])
        title = str(meta.get("title") or heading or fn[:-3]).strip()
        body, toc = md_to_html(demote_h1(text))
        out_name = fn[:-3] + ".html"
        out_path = os.path.join(abs_dir, out_name)

        rel_url = rel_dir + "/" + out_name
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        tags = [str(t) for t in tags]
        series = str(meta.get("series") or default_series(rel_dir))
        desc = str(meta.get("summary") or "").strip() or f"{title} —— 小番茄程序猿的技术文章"
        related = find_related(catalog, rel_url, series, tags)
        pathnav = ""
        conf = PATH_NAV_CONF.get(rel_dir)
        if conf and fn[:-3] in conf[0]:
            path_titles = {e["rel_url"]: e["title"] for e in catalog}
            pathnav = build_path_nav(conf[0].index(fn[:-3]), path_titles, prefix, rel_dir)
        html_doc = build_html(
            prefix, title, body, active, back_href, back_label, toc, meta,
            canonical=SITE_BASE + rel_url,
            keywords=", ".join(dict.fromkeys(tags + [series])),
            jsonld=build_jsonld(title, desc, rel_url, meta, tags, series),
            related_html=build_related_html(related, prefix),
            desc=desc,
            pathnav=pathnav,
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        count += 1
    return count


def main():
    ensure_pygments_css()
    catalog = scan_catalog()
    total = 0
    # 博客文章
    total += process_dir("blog", "blog", "返回博客索引", catalog)
    # 深度技术各系列
    total += process_dir("deep/mysql", "deep", "返回深度技术", catalog)
    total += process_dir("deep/redis", "deep", "返回深度技术", catalog)
    total += process_dir("deep/es", "deep", "返回深度技术", catalog)
    total += process_dir("deep/bench", "deep", "返回深度技术", catalog)
    total += process_dir("deep/k8s", "deep", "返回深度技术", catalog)
    total += process_dir("deep/agent", "deep", "返回深度技术", catalog)
    total += process_dir("deep/prj", "proj", "返回项目清单", catalog)
    print("生成文章页总数:", total)
    print("pygments.css:", "已存在" if os.path.exists(PYG_CSS) else "已生成")


if __name__ == "__main__":
    main()
