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


def check_md_quality():
    errors = []
    for path in list(ROOT.glob("blog/*.md")) + list(ROOT.glob("deep/*/*.md")):
        meta, body, err = parse_front_matter(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        if err:
            errors.append(f"{rel}: {err}")
            continue  # 元数据不可信，跳过后续必填项检查
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


def main():
    errors = []
    errors += check_md_quality()
    errors += check_html_quality()
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


if __name__ == "__main__":
    main()
