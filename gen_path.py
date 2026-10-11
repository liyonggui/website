# -*- coding: utf-8 -*-
"""生成 mysql-path.html：MySQL 学习线路页（38 站主线路 + 6 篇随查手册）。"""
import os
import urllib.parse

from gen_articles import ROOT, SITE_BASE, TEMPLATE, build_html, scan_catalog
from path_data import MYSQL_STAGES, MYSQL_REFERENCE, MYSQL_ORDER


def build_body(titles):
    total = len(MYSQL_ORDER)
    parts = []
    parts.append(
        '<div class="path-hero">'
        '<div class="eyebrow">学习线路 · MySQL</div>'
        '<h1>MySQL 学习线路：<br/>从原理到生产治理的 %d 站。</h1>'
        '<p class="lead">这条线路把本站全部 %d 篇 MySQL 文章按「认知顺序」重新组织：'
        '%d 站主线路建议按顺序读——每一站回答一个问题：<b>读完它你能解决什么</b>；'
        '另有 %d 篇随查手册放在最后，工作中直接翻，不必按顺序。</p>'
        '<div class="path-progressbar"><i style="width:0"></i></div>'
        '</div>' % (total, total + len(MYSQL_REFERENCE), total, len(MYSQL_REFERENCE))
    )

    step = 0
    for si, (stage_name, stage_desc, items) in enumerate(MYSQL_STAGES, 1):
        lis = []
        for stem, line in items:
            step += 1
            title = titles.get("deep/mysql/" + stem + ".html", stem)
            href = "deep/mysql/" + urllib.parse.quote(stem + ".html")
            lis.append(
                '<li><a href="%s">'
                '<span class="step-no">%02d</span>'
                '<span class="step-body"><strong>%s</strong><em>%s</em></span>'
                '<span class="arr">→</span></a></li>'
                % (href, step, title, line)
            )
        parts.append(
            '<section class="path-stage">'
            '<div class="stage-head"><span class="stage-no">%d</span>'
            '<div class="stage-tt"><h2>%s</h2><p>%s</p></div>'
            '<span class="stage-cnt">%d 站</span></div>'
            '<ol class="path-list">%s</ol>'
            '</section>'
            % (si, stage_name, stage_desc, len(items), "".join(lis))
        )

    refs = []
    for stem, line in MYSQL_REFERENCE:
        title = titles.get("deep/mysql/" + stem + ".html", stem)
        href = "deep/mysql/" + urllib.parse.quote(stem + ".html")
        refs.append(
            '<li><a href="%s"><strong>%s</strong><em>%s</em></a></li>'
            % (href, title, line)
        )
    parts.append(
        '<section class="path-ref">'
        '<h2>附 · 随查手册（%d 篇）</h2>'
        '<p>不按顺序读：线上出问题时，这些是直接照着操作的手册。</p>'
        '<ul class="ref-list">%s</ul>'
        '</section>' % (len(MYSQL_REFERENCE), "".join(refs))
    )
    return "\n".join(parts)


def main():
    catalog = scan_catalog()
    titles = {e["rel_url"]: e["title"] for e in catalog}
    body = build_body(titles)
    doc = build_html(
        prefix="",
        title="MySQL 学习线路：从原理到生产治理的 %d 站" % len(MYSQL_ORDER),
        body=body,
        active="learn",
        back_href="learn.html",
        back_label="返回学习线路",
        toc="",
        meta=None,
        canonical=SITE_BASE + "mysql-path.html",
        keywords="MySQL, 学习路线, 学习线路, 事务, 锁, 索引, 高可用",
        jsonld='{"@context":"https://schema.org","@type":"Article",'
               '"headline":"MySQL 学习线路","author":{"@type":"Person","name":"小番茄程序猿"},'
               '"url":"' + SITE_BASE + 'mysql-path.html","inLanguage":"zh-CN"}',
        related_html="",
        desc="把本站全部 MySQL 文章按认知顺序组织成 %d 站学习线路：打底、事务与锁、索引与 SQL、"
             "连接与容量、扩展与高可用、进阶专题，另附随查手册。" % len(MYSQL_ORDER),
    )
    out = os.path.join(ROOT, "mysql-path.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print("mysql-path.html generated,", len(MYSQL_ORDER), "站 +", len(MYSQL_REFERENCE), "篇随查手册")


if __name__ == "__main__":
    main()
