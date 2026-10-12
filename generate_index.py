# -*- coding: utf-8 -*-
"""
内容索引生成器：读取 blog/ 与 deep/ 下所有 .md 的 front matter，
生成 data/articles.json、tags.json、archives.json、search_index.json 与 rss.xml。
用法：python generate_index.py
- URL 采用站点相对路径（如 blog/xxx.html），兼容 GitHub Pages Project Pages 子路径。
- RSS 链接用绝对地址（含 /website/ 前缀），日期转 RFC 822。
- draft: true 的文章不进任何索引。
"""
import datetime
import email.utils
import html
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SITE_URL = "https://liyonggui.github.io/website/"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def parse_front_matter(text):
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


def get_title_from_md(body):
    m = re.search(r"^#{1,2}\s+(.+?)\s*#*\s*$", body, re.M)
    return m.group(1).strip() if m else None


def collect_md_files():
    files = []
    for base in (ROOT / "blog", ROOT / "deep"):
        if base.exists():
            files += sorted(p for p in base.rglob("*.md") if not p.name.startswith("."))
    return files


def build_record(md_path):
    text = md_path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    rel = md_path.relative_to(ROOT).as_posix()

    title = str(meta.get("title") or get_title_from_md(body) or md_path.stem).strip()
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(t) for t in tags]

    return {
        "title": title,
        "summary": str(meta.get("summary") or "").strip(),
        "date": str(meta.get("date") or ""),
        "tags": tags,
        "category": str(meta.get("category") or ("blog" if rel.startswith("blog/") else "deep")),
        "series": str(meta.get("series") or ("博客" if rel.startswith("blog/") else rel.split("/")[1])),
        "slug": str(meta.get("slug") or md_path.stem),
        "reading_time": meta.get("reading_time") or "",
        "featured": bool(meta.get("featured", False)),
        "draft": bool(meta.get("draft", False)),
        "url": rel.replace(".md", ".html"),
        "source": rel,
    }


def build_articles_json():
    records = [build_record(p) for p in collect_md_files()]
    records = [r for r in records if not r["draft"]]
    records.sort(key=lambda x: (x.get("date") or "", x.get("title")), reverse=True)
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "articles.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return records


def build_tags_json(records):
    tag_map = defaultdict(list)
    for r in records:
        for tag in r.get("tags", []):
            tag_map[tag].append(r)
    tags = [
        {"name": name, "count": len(items)}
        for name, items in sorted(tag_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    (DATA_DIR / "tags.json").write_text(
        json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tags


def build_archives_json(records):
    year_map = defaultdict(list)
    for r in records:
        if r.get("date"):
            year_map[str(r["date"])[:4]].append(r)
    archives = [
        {"year": year, "items": items}
        for year, items in sorted(year_map.items(), reverse=True)
    ]
    (DATA_DIR / "archives.json").write_text(
        json.dumps(archives, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return archives


def build_search_index(records):
    index = [
        {
            "title": r["title"],
            "summary": r["summary"],
            "tags": r.get("tags", []),
            "url": r["url"],
            "series": r["series"],
            "category": r["category"],
            "date": r["date"],
        }
        for r in records
    ]
    (DATA_DIR / "search_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index


def build_rss(records):
    items = []
    for r in records[:20]:
        try:
            y, m, d = (r["date"] + "-01-01")[:10].split("-")
            pub = email.utils.format_datetime(datetime.datetime(int(y), int(m), int(d)))
        except Exception:
            pub = ""
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(r['title'])}</title>\n"
            f"      <link>{enc_url(r['url'])}</link>\n"
            f"      <guid>{enc_url(r['url'])}</guid>\n"
            f"      <description>{html.escape(r['summary'])}</description>\n"
            f"      <pubDate>{pub}</pubDate>\n"
            "    </item>"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>\n'
        "  <title>小番茄程序猿 · 技术站点</title>\n"
        f"  <link>{SITE_URL}</link>\n"
        "  <description>高并发后端与 AI Agent 工程化的技术博客与深度系列</description>\n"
        + "\n".join(items)
        + "\n</channel></rss>\n"
    )
    (ROOT / "rss.xml").write_text(rss, encoding="utf-8")


STATIC_PAGES = [
    ("", "1.0"), ("projects.html", "0.9"), ("about.html", "0.7"),
    ("blog.html", "0.8"), ("deep.html", "0.9"), ("learn.html", "0.9"),
    ("mysql-path.html", "0.8"), ("redis-path.html", "0.8"),
    ("tags.html", "0.5"), ("archives.html", "0.5"), ("search.html", "0.5"),
]


def enc_url(rel):
    """sitemap 协议要求 URL 中非 ASCII 字符做 UTF-8 百分号编码（路径保留 /）。"""
    return SITE_URL + quote(rel, safe="/")


def build_sitemap(records):
    urls = [(SITE_URL + p, "weekly", pri, "") for p, pri in STATIC_PAGES]
    for f in sorted((ROOT / "project").glob("*.html")):
        urls.append((SITE_URL + "project/" + f.name, "monthly", "0.6", ""))
    for r in records:
        urls.append((enc_url(r["url"]), "monthly", "0.6", r["date"]))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, freq, pri, lastmod in urls:
        lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        lines.append(
            f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq>"
            f"<priority>{pri}</priority>{lm}</url>"
        )
    lines.append("</urlset>")
    (ROOT / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sitemap.xml：{len(urls)} 个 URL")


def main():
    records = build_articles_json()
    build_tags_json(records)
    build_archives_json(records)
    build_search_index(records)
    build_rss(records)
    build_sitemap(records)
    print(f"索引生成完成：{len(records)} 篇文章 → data/*.json + rss.xml + sitemap.xml")


if __name__ == "__main__":
    main()
