# -*- coding: utf-8 -*-
"""生成 project/<slug>.html：16 个项目详细页。

- 复用 gen_articles 的 TEMPLATE 与 build_html（导航/页脚/样式全站一致）
- 数据来自 project_data.PROJECTS
- 卡片在 projects.html 中已改为 <a> 指向这些页面
"""
import html
import pathlib
import urllib.parse

from gen_articles import ROOT, SITE_BASE, build_html, scan_catalog
from project_data import PROJECTS, TECH


def build_body(idx, slug, family, title, metric, lead, paras, highlights, related, titles):
    parts = []
    parts.append(
        '<div class="path-hero">'
        '<div class="eyebrow">项目详情 · %s</div>'
        '<h1>%s</h1>'
        '<p class="lead">%s<span class="metric" style="margin-left:10px">%s</span></p>'
        "</div>"
        % (html.escape(family), html.escape(title), html.escape(lead), html.escape(metric))
    )

    # 项目概述
    parts.append('<section class="path-stage">')
    parts.append('<h2>项目概述</h2>')
    for p in paras:
        parts.append('<p style="max-width:760px;line-height:1.85;color:var(--ink-soft)">%s</p>' % html.escape(p))
    parts.append("</section>")

    # 技术亮点与难点攻坚
    tech = TECH.get(slug, [])
    if tech:
        parts.append('<section class="path-stage">')
        parts.append("<h2>技术亮点与难点攻坚</h2>")
        lis = []
        for h, problem, solution in tech:
            lis.append(
                '<li><div class="step-body"><strong>%s</strong>'
                '<em><b>难点：</b>%s</em>'
                '<em><b>方案：</b>%s</em></div></li>'
                % (html.escape(h), html.escape(problem), html.escape(solution))
            )
        parts.append('<ul class="ref-list">%s</ul>' % "".join(lis))
        parts.append("</section>")

    # 关键数据 / 亮点
    if highlights:
        parts.append('<section class="path-stage">')
        parts.append("<h2>关键数据与亮点</h2>")
        lis = []
        for h, d in highlights:
            lis.append(
                '<li><div class="step-body"><strong>%s</strong><em>%s</em></div></li>'
                % (html.escape(h), html.escape(d))
            )
        parts.append('<ul class="ref-list">%s</ul>' % "".join(lis))
        parts.append("</section>")

    # 相关文章
    if related:
        parts.append('<section class="path-stage">')
        parts.append("<h2>相关文章</h2>")
        lis = []
        for rel, line in related:
            href = "../" + urllib.parse.quote(rel)
            t = titles.get(rel, rel.rsplit("/", 1)[-1][:-5])
            lis.append(
                '<li><a href="%s"><strong>%s</strong><em>%s</em><span class="arr">→</span></a></li>'
                % (href, html.escape(t), html.escape(line))
            )
        parts.append('<ul class="ref-list">%s</ul>' % "".join(lis))
        parts.append("</section>")

    # 上下项目导航
    prev_p = PROJECTS[idx - 1] if idx > 0 else None
    next_p = PROJECTS[idx + 1] if idx < len(PROJECTS) - 1 else None
    prev_html = (
        '<a class="pn-prev" href="%s.html"><span class="pn-dir">← 上一个项目</span>'
        '<span class="pn-title">%s</span></a>' % (prev_p[1], html.escape(prev_p[2]))
        if prev_p else "<span></span>"
    )
    next_html = (
        '<a class="pn-next" href="%s.html"><span class="pn-dir">下一个项目 →</span>'
        '<span class="pn-title">%s</span></a>' % (next_p[1], html.escape(next_p[2]))
        if next_p else "<span></span>"
    )
    parts.append(
        '<nav class="path-nav"><div class="wrap">'
        '<span class="pn-label">项目详情 · 第 %d/%d 个</span>'
        '<div class="pn-links">%s%s</div>'
        "</div></nav>" % (idx + 1, len(PROJECTS), prev_html, next_html)
    )
    return "\n".join(parts)


def main():
    catalog = scan_catalog()
    titles = {e["rel_url"]: e["title"] for e in catalog}
    (pathlib.Path(ROOT) / "project").mkdir(exist_ok=True)
    n = 0
    for idx, (family, slug, title, metric, lead, paras, highlights, related) in enumerate(PROJECTS):
        body = build_body(idx, slug, family, title, metric, lead, paras, highlights, related, titles)
        doc = build_html(
            prefix="../",
            title="%s · 小番茄程序猿项目详情" % title,
            body=body,
            active="proj",
            back_href="projects.html",
            back_label="返回项目清单",
            toc="",
            meta=None,
            canonical=SITE_BASE + "project/" + slug + ".html",
            keywords="%s, 项目详情, 高并发, AI Agent" % title,
            desc=lead,
        )
        out = pathlib.Path(ROOT) / "project" / (slug + ".html")
        out.write_text(doc, encoding="utf-8")
        n += 1
    print("project/ 详细页生成: %d 个" % n)


if __name__ == "__main__":
    main()
