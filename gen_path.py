# -*- coding: utf-8 -*-
"""生成学习线路页：mysql-path.html（38 站）与 redis-path.html（15 站）。

数据驱动：线路定义在 path_data.py，加新线路时定义 STAGES/REFERENCE/ORDER
并往 PATHS 里加一条即可。
"""
import os
import urllib.parse

from gen_articles import ROOT, SITE_BASE, build_html, scan_catalog
from path_data import (
    MYSQL_STAGES, MYSQL_REFERENCE, MYSQL_ORDER,
    REDIS_STAGES, REDIS_REFERENCE, REDIS_ORDER,
    ES_STAGES, ES_REFERENCE, ES_ORDER,
    K8S_STAGES, K8S_REFERENCE, K8S_ORDER,
)


def build_body(name, stages, refs, order, titles, base_dir):
    total = len(order)
    parts = []
    parts.append(
        '<div class="path-hero">'
        '<div class="eyebrow">学习线路 · %s</div>'
        '<h1>%s 学习线路：<br/>从原理到生产实战的 %d 站。</h1>'
        '<p class="lead">这条线路把本站全部 %d 篇 %s 文章按「认知顺序」重新组织：'
        '%d 站主线路建议按顺序读——每一站回答一个问题：<b>读完它你能解决什么</b>；'
        '另有 %d 篇随查手册放在最后，工作中直接翻，不必按顺序。</p>'
        '<div class="path-progressbar"><i style="width:0"></i></div>'
        '</div>' % (name, name, total, total + len(refs), name, total, len(refs))
    )

    step = 0
    for si, (stage_name, stage_desc, items) in enumerate(stages, 1):
        lis = []
        for stem, line in items:
            step += 1
            title = titles.get(base_dir + "/" + stem + ".html", stem)
            href = base_dir + "/" + urllib.parse.quote(stem + ".html")
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

    refs_html = []
    for stem, line in refs:
        title = titles.get(base_dir + "/" + stem + ".html", stem)
        href = base_dir + "/" + urllib.parse.quote(stem + ".html")
        refs_html.append(
            '<li><a href="%s"><strong>%s</strong><em>%s</em></a></li>'
            % (href, title, line)
        )
    parts.append(
        '<section class="path-ref">'
        '<h2>附 · 随查手册（%d 篇）</h2>'
        '<p>不按顺序读：线上出问题时，这些是直接照着操作的手册。</p>'
        '<ul class="ref-list">%s</ul>'
        '</section>' % (len(refs), "".join(refs_html))
    )
    return "\n".join(parts)


PATHS = [
    {
        "slug": "mysql-path",
        "name": "MySQL",
        "stages": MYSQL_STAGES, "refs": MYSQL_REFERENCE, "order": MYSQL_ORDER,
        "dir": "deep/mysql",
        "keywords": "MySQL, 学习路线, 学习线路, 事务, 锁, 索引, 高可用",
        "desc_t": "把本站全部 MySQL 文章按认知顺序组织成 %d 站学习线路：打底、事务与锁、索引与 SQL、"
                  "连接与容量、扩展与高可用、进阶专题，另附随查手册。",
    },
    {
        "slug": "redis-path",
        "name": "Redis",
        "stages": REDIS_STAGES, "refs": REDIS_REFERENCE, "order": REDIS_ORDER,
        "dir": "deep/redis",
        "keywords": "Redis, 学习路线, 学习线路, Pipeline, 缓存, Cluster, 哨兵, 高并发",
        "desc_t": "把本站全部 Redis 文章按认知顺序组织成 %d 站学习线路：打底、Pipeline 与网络往返、"
                  "数据结构与访问模式、过期与淘汰、集群与高可用，另附随查手册。",
    },
    {
        "slug": "es-path",
        "name": "Elasticsearch",
        "stages": ES_STAGES, "refs": ES_REFERENCE, "order": ES_ORDER,
        "dir": "deep/es",
        "keywords": "Elasticsearch, ES, 学习路线, 学习线路, 分片, 路由, ILM, Reindex, 冷热分层",
        "desc_t": "把本站全部 Elasticsearch 文章按认知顺序组织成 %d 站学习线路：写入链路、路由与分片分布、"
                  "索引生命周期 ILM、重建迁移与冷热分层，另附随查手册。",
    },
    {
        "slug": "k8s-path",
        "name": "K8s",
        "stages": K8S_STAGES, "refs": K8S_REFERENCE, "order": K8S_ORDER,
        "dir": "deep/k8s",
        "keywords": "K8s, K3s, 学习路线, 学习线路, StatefulSet, 哨兵, Ingress, HPA, 滚动更新",
        "desc_t": "把本站全部 K8s 文章按认知顺序组织成 %d 站学习线路：从 Compose 到 K3s、有状态服务部署、"
                  "流量入口与弹性，另附随查手册。",
    },
]


def main():
    catalog = scan_catalog()
    titles = {e["rel_url"]: e["title"] for e in catalog}
    for p in PATHS:
        body = build_body(p["name"], p["stages"], p["refs"], p["order"], titles, p["dir"])
        total = len(p["order"])
        doc = build_html(
            prefix="",
            title="%s 学习线路：从原理到生产实战的 %d 站" % (p["name"], total),
            body=body,
            active="learn",
            back_href="learn.html",
            back_label="返回学习线路",
            toc="",
            meta=None,
            canonical=SITE_BASE + p["slug"] + ".html",
            keywords=p["keywords"],
            jsonld='{"@context":"https://schema.org","@type":"Article",'
                   '"headline":"%s 学习线路","author":{"@type":"Person","name":"小番茄程序猿"},'
                   '"url":"' % p["name"] + SITE_BASE + p["slug"] + '.html","inLanguage":"zh-CN"}',
            related_html="",
            desc=p["desc_t"] % total,
        )
        out = os.path.join(ROOT, p["slug"] + ".html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(doc)
        print("%s.html generated, %d 站 + %d 篇随查手册" % (p["slug"], total, len(p["refs"])))


if __name__ == "__main__":
    main()
