# -*- coding: utf-8 -*-
"""校验 112 篇文章页：页头 h1 恰 1 个，正文 prose 内不再含 h1。"""
import glob
import io
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

bad = []
total = 0
for path in glob.glob("blog/*.html") + glob.glob("deep/*/*.html"):
    total += 1
    h = open(path, encoding="utf-8").read()
    head = re.findall(r'<div class="article-head">.*?<h1>(.*?)</h1>', h, re.S)
    m = re.search(r'<article class="prose">(.*?)</article>', h, re.S)
    body_h1 = m.group(1).count("<h1") if m else -1
    if len(head) != 1 or body_h1 != 0:
        bad.append((path, len(head), body_h1))

print(f"文章页总数: {total}")
print(f"异常(页头h1!=1 或 正文含h1): {len(bad)}")
for p, a, b in bad[:10]:
    print("  ", p, "head=", a, "body_h1=", b)

# 抽样展示两篇的页头标题
for p in ["blog/01-Redis-String三种编码压测.html",
          "deep/mysql/KingbaseES-锁与事务排查手册.html"]:
    h = open(p, encoding="utf-8").read()
    t = re.search(r'<div class="article-head">.*?<h1>(.*?)</h1>', h, re.S)
    m = re.search(r'<article class="prose">(.*?)</article>', h, re.S)
    print(f"\n{p}\n  页头标题: {t.group(1).strip() if t else '(无)'}\n  正文 h1 数: {m.group(1).count('<h1') if m else '?'}")
