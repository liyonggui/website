# -*- coding: utf-8 -*-
"""
站点内容校验（落地方案 §6 升级版）：
1. md 元数据完整性：title / summary / date / tags / category 必填
2. HTML 结构：页头 h1 恰 1 个、正文无 h1
3. md ↔ html 对应存在
4. data/*.json 与 rss.xml 存在且可解析
任一失败打印清单并 exit 1（供 CI 使用）。
"""
import glob
import io
import json
import pathlib
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent


def parse_front_matter(text):
    """返回 (meta, body, err)；有 front matter 但解析失败时 err 给出原因（不静默退化）。"""
    if not text.startswith("---\n"):
        return {}, text, None
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text, "front matter 未闭合"
    front = text[4:end]
    body = text[end + len("\n---\n"):]
    try:
        import yaml
        data = yaml.safe_load(front)
        if isinstance(data, dict):
            return data, body, None
        return {}, body, "front matter 不是键值映射"
    except Exception as e:
        return {}, body, f"YAML 解析失败: {e}"


def is_draft(meta):
    return str(meta.get("draft", "")).strip().lower() in ("true", "yes", "1")


def check_md_quality():
    errors = []
    for path in list(ROOT.glob("blog/*.md")) + list(ROOT.glob("deep/*/*.md")):
        meta, body, err = parse_front_matter(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        if err:
            errors.append(f"{rel}: {err}")
            continue  # 元数据不可信，跳过后续必填项检查
        if is_draft(meta):
            continue  # 草稿：不要求必填项完整、不要求生成 HTML
        for field in ("title", "summary", "date", "tags", "category"):
            if not meta.get(field):
                errors.append(f"{rel}: front matter 缺少 {field}")
        if meta.get("category") not in ("blog", "deep", None):
            errors.append(f"{rel}: category 非法（应为 blog/deep）")
    return errors


def check_html_quality():
    errors = []
    for path in glob.glob("blog/*.html") + glob.glob("deep/*/*.html"):
        h = open(path, encoding="utf-8").read()
        head = re.findall(r'<div class="article-head">.*?<h1>(.*?)</h1>', h, re.S)
        m = re.search(r'<article class="prose">(.*?)</article>', h, re.S)
        body_h1 = m.group(1).count("<h1") if m else -1
        if len(head) != 1 or body_h1 != 0:
            errors.append(f"{path}: 页头 h1 数={len(head)} 正文 h1 数={body_h1} 异常")
    return errors


def check_missing_html():
    errors = []
    for md_path in list(ROOT.glob("blog/*.md")) + list(ROOT.glob("deep/*/*.md")):
        meta, _, _ = parse_front_matter(md_path.read_text(encoding="utf-8"))
        if is_draft(meta):
            continue  # 草稿不生成 HTML
        if not md_path.with_suffix(".html").exists():
            errors.append(f"{md_path.as_posix()}: 对应 HTML 未生成")
    return errors


def check_data_files():
    errors = []
    for name in ("articles.json", "tags.json", "archives.json", "search_index.json"):
        p = ROOT / "data" / name
        if not p.exists():
            errors.append(f"data/{name} 不存在（先运行 generate_index.py）")
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"data/{name} JSON 解析失败: {e}")
    if not (ROOT / "rss.xml").exists():
        errors.append("rss.xml 不存在（先运行 generate_index.py）")
    return errors


def check_index_urls():
    """索引里每篇文章的 url 必须对应实际存在的 HTML。"""
    errors = []
    p = ROOT / "data" / "articles.json"
    if not p.exists():
        return errors
    try:
        records = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return errors
    for r in records:
        url = r.get("url") or ""
        if url and not (ROOT / url).exists():
            errors.append(f"articles.json 指向不存在的页面: {url}")
    return errors


def check_xml():
    """rss.xml 与 sitemap.xml 必须是合法 XML。"""
    import xml.etree.ElementTree as ET
    errors = []
    for name in ("rss.xml", "sitemap.xml"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            ET.parse(p)
        except Exception as e:
            errors.append(f"{name} XML 解析失败: {e}")
    return errors


def check_static_h1():
    """顶层静态页与线路页：h1 恰好 1 个（标题层级统一）。"""
    pages = ["index.html", "projects.html", "blog.html", "deep.html", "learn.html",
             "about.html", "about-en.html", "community.html", "tags.html", "archives.html", "search.html",
             "mysql-path.html", "redis-path.html", "es-path.html", "k8s-path.html"]
    errors = []
    for p in pages:
        f = pathlib.Path(p)
        if not f.exists():
            errors.append(f"{p}: 静态页不存在")
            continue
        n = len(re.findall(r"<h1[\s>]", f.read_text(encoding="utf-8")))
        if n != 1:
            errors.append(f"{p}: h1 数={n}（应恰好 1 个）")
    return errors


PENDING_PAT = re.compile(r"(待补|待复测|待验证|待演练|TODO[:：]|^#{2,3} .*待办)")


def report_pending():
    """内容巡检（信息项，不计入失败）：列出文中明确的「待补」类未完成承诺。"""
    hits = []
    mds = list(ROOT.glob("blog/*.md")) + list(ROOT.glob("deep/*/*.md"))
    for md in mds:
        text = md.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if PENDING_PAT.search(line):
                snippet = line.strip()[:60]
                hits.append(f"{md.relative_to(ROOT)}:{i}  …{snippet}")
    return hits


def main():
    errors = []
    errors += check_md_quality()
    errors += check_html_quality()
    errors += check_static_h1()
    errors += check_missing_html()
    errors += check_data_files()
    errors += check_index_urls()
    errors += check_xml()

    if errors:
        print(f"站点校验失败（{len(errors)} 项）：")
        for e in errors:
            print(" -", e)
        raise SystemExit(1)

    print("站点校验通过：元数据 / HTML 结构 / 产物对应 / 索引文件 / URL / XML 全部正常")

    pending = report_pending()
    if pending:
        print(f"\n[巡检] 待补/待办标记 {len(pending)} 处（信息项，不影响通过）：")
        for h in pending:
            print("  ·", h)


if __name__ == "__main__":
    main()
