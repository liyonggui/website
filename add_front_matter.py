# -*- coding: utf-8 -*-
"""
给 blog/ 与 deep/ 下所有 .md 注入统一 YAML front matter（幂等：已有则跳过）。
字段推导规则：
- title:    文档首个 h1/h2 标题（与 gen_articles.extract_title 同规则），否则文件名
- summary:  标题后的第一个非空文本块（含 blockquote，剥掉 > 与行内标记），截 90 字
- date:     文件修改时间（YYYY-MM-DD）
- author:   小番茄程序猿
- tags:     按目录系列 + 标题关键词推导
- category: blog | deep
- series:   deep/<系列名> / 博客
- slug:     文件名 stem（站点 URL 仍以文件名为准，slug 仅作标识）
- reading_time: 中文字符数 / 400，向上取整，最少 1
- featured: blog 文章 true，deep false
- draft:    false
"""
import datetime
import io
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
AUTHOR = "小番茄程序猿"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SERIES_TAGS = {
    "mysql": ["MySQL"],
    "redis": ["Redis"],
    "es": ["Elasticsearch"],
    "bench": ["压测"],
    "k8s": ["K8s", "云原生"],
    "agent": ["AI Agent", "Spring AI"],
}
KEYWORD_TAGS = [
    ("Redis", ["Redis"]),
    ("MySQL", ["MySQL"]),
    ("Elasticsearch", ["ES", "Elasticsearch"]),
    ("Seata", ["Seata"]),
    ("Spring Cloud", ["Spring Cloud", "微服务"]),
    ("压测", ["压测", "benchmark", "k6", "性能"]),
    ("事务", ["事务", "MVCC", "隔离"]),
    ("锁", ["锁", "死锁", "间隙锁"]),
    ("索引", ["索引"]),
    ("分库分表", ["分库分表", "分片"]),
    ("RAG", ["RAG", "向量", "召回", "rerank"]),
    ("MCP", ["MCP"]),
    ("评测", ["评测", "RAGAS", "eval"]),
    ("Java 并发", ["线程池", "异步", "并发", "JUC"]),
    ("源码", ["源码", "原理"]),
    ("缓存", ["缓存", "热点", "Pipeline"]),
    ("ILM", ["ILM", "Rollover", "Reindex"]),
]


def md_paths():
    for base in ("blog", os.path.join("deep", "mysql"), os.path.join("deep", "redis"),
                 os.path.join("deep", "es"), os.path.join("deep", "bench"),
                 os.path.join("deep", "k8s"), os.path.join("deep", "agent")):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md") and not fn.startswith("."):
                yield os.path.join(d, fn)


def unescape_md(s):
    """去掉 markdown 转义符：\\+ \\\\. 等 → 原字符（部分原文标题带转义，进 YAML 会炸）。"""
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", s)


def first_title(body):
    """与 gen_articles.extract_title 同规则：首个非空行若是 h1/h2 则返回其文本。"""
    lines = body.splitlines()
    for line in lines[:10]:
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", s)
        if m:
            return unescape_md(re.sub(r"\s*#+\s*$", "", m.group(1)).strip())
        break
    return None


def first_summary(body, title):
    """标题后的第一个非空文本块（剥 blockquote/行内标记），截 90 字。"""
    started = title is None  # 无标题文档从头找
    buf = []
    for line in body.splitlines():
        s = line.strip()
        if not started:
            if re.match(r"^#{1,2}\s+", s):
                started = True
            continue
        if not s:
            if buf:
                break
            continue
        if re.match(r"^#{1,6}\s+", s) and not buf:
            continue  # 连续标题，跳过
        if re.match(r"^(-{3,}|={3,}|\|)", s):
            if buf:
                break
            continue
        buf.append(s)
        if sum(len(x) for x in buf) > 120:
            break
    text = " ".join(buf)
    text = re.sub(r"^>\s*", "", text)
    text = re.sub(r"[`*_\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return unescape_md(text)[:90] + ("…" if len(text) > 90 else "")


def derive_tags(category, series_key, title):
    tags = list(SERIES_TAGS.get(series_key, []))
    text = title or ""
    for tag, kws in KEYWORD_TAGS:
        if any(kw.lower() in text.lower() for kw in kws) and tag not in tags:
            tags.append(tag)
    if not tags:
        tags = ["技术笔记"]
    return tags[:5]


def yq(s):
    r"""YAML 双引号安全包装：先转义反斜杠（兼容 D:\path 类路径），再转义双引号。"""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def build_fm(md_path, category):
    rel = os.path.relpath(md_path, ROOT).replace("\\", "/")
    series_key = rel.split("/")[1] if rel.startswith("deep/") else None
    text = open(md_path, encoding="utf-8").read()
    if text.startswith("---"):
        return None  # 已有 front matter，幂等跳过

    title = first_title(text) or os.path.splitext(os.path.basename(md_path))[0]
    summary = first_summary(text, first_title(text))
    date = datetime.date.fromtimestamp(os.path.getmtime(md_path)).isoformat()
    tags = derive_tags(category, series_key, title)
    series = series_key if series_key else "博客"
    reading = max(1, math.ceil(len(re.sub(r"\s", "", text)) / 400))
    featured = "true" if category == "blog" else "false"
    slug = os.path.splitext(os.path.basename(md_path))[0]

    lines = [
        "---",
        f"title: {yq(title)}",
        f"summary: {yq(summary)}",
        f"date: {date}",
        f"author: {yq(AUTHOR)}",
        "tags:",
    ]
    lines += [f"  - {t}" for t in tags]
    lines += [
        f"category: {category}",
        f"series: {yq(series)}",
        f"slug: {yq(slug)}",
        f"reading_time: {reading}",
        f"featured: {featured}",
        "draft: false",
        "---",
        "",
    ]
    return "\n".join(lines) + text


def main():
    n = 0
    for p in md_paths():
        category = "blog" if os.sep + "blog" in p or "/blog/" in p.replace("\\", "/") else "deep"
        fm = build_fm(p, category)
        if fm is None:
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write(fm)
        n += 1
    print(f"已注入 front matter: {n} 篇")


if __name__ == "__main__":
    main()
