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
    AGENT_STAGES, AGENT_REFERENCE, AGENT_ORDER,
)


def build_body(name, stages, refs, order, titles, base_dir, fit=None, shortcut=None):
    total = len(order)
    parts = []
    parts.append(
        '<div class="path-hero">'
        '<div class="eyebrow">学习线路 · %s</div>'
        '<h2>%s 学习线路：<br/>从原理到生产实战的 %d 站。</h2>'
        '<p class="lead">这条线路把本站全部 %d 篇 %s 文章按「认知顺序」重新组织：'
        '%d 站主线路建议按顺序读——每一站回答一个问题：<b>读完它你能解决什么</b>；'
        '另有 %d 篇随查手册放在最后，工作中直接翻，不必按顺序。</p>'
        '<div class="path-progressbar"><i style="width:0"></i></div>'
        '</div>' % (name, name, total, total + len(refs), name, total, len(refs))
    )

    # 适合谁 / 前置知识 / 完成标准
    if fit:
        parts.append(
            '<div class="path-fit">'
            '<div class="fit-row"><span class="fit-k">适合谁</span><span class="fit-v">%s</span></div>'
            '<div class="fit-row"><span class="fit-k">前置知识</span><span class="fit-v">%s</span></div>'
            '<div class="fit-row"><span class="fit-k">完成标准</span><span class="fit-v">%s</span></div>'
            '</div>' % (fit["audience"], fit["prereq"], fit["outcome"])
        )

    # 轻量捷径：先读这几站（长线路减负入口）
    if shortcut:
        lis = []
        for i, stem in enumerate(shortcut["stems"], 1):
            title = titles.get(base_dir + "/" + stem + ".html", stem)
            href = base_dir + "/" + urllib.parse.quote(stem + ".html")
            lis.append('<li><a href="%s">%s →</a></li>' % (href, title))
        parts.append(
            '<div class="path-shortcut">'
            '<div class="sc-t">%s</div>'
            '<ol class="sc-list">%s</ol>'
            '<p class="sc-note">%s 线上救火不想按顺序？直接翻 <a href="#path-refs">随查手册</a>。</p>'
            '</div>' % (shortcut["title"], "".join(lis), name)
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
            '<div class="stage-tt"><h3>%s</h3><p>%s</p></div>'
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
        '<section class="path-ref" id="path-refs">'
        '<h3>附 · 随查手册（%d 篇）</h3>'
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
        "fit": {
            "audience": "想系统补 MySQL 底层原理的初中级后端；正在被连接池、锁等待、慢查询、主从延迟折磨的开发者；准备后端面试、需要把八股变成实测结论的人。",
            "prereq": "会基本 SQL 与建表，知道「事务、索引」是什么概念即可——不要求 InnoDB 源码基础，主线会从现象出发把原理讲透。",
            "outcome": "能独立完成连接池调优、锁等待/死锁治理、慢查询定位、主从延迟排障的完整闭环；能对隔离级别、分库分表、高可用方案做出有依据的选型判断。",
        },
        "shortcut": {
            "title": "38 站太长？先读这 5 站",
            "stems": ["场景1-连接池与超卖问题", "场景2-事务隔离级别", "场景5-索引失效与慢查询",
                      "场景11-主从延迟治理", "场景16-连接池参数调优"],
        },
    },
    {
        "slug": "redis-path",
        "name": "Redis",
        "stages": REDIS_STAGES, "refs": REDIS_REFERENCE, "order": REDIS_ORDER,
        "dir": "deep/redis",
        "keywords": "Redis, 学习路线, 学习线路, Pipeline, 缓存, Cluster, 哨兵, 高并发",
        "desc_t": "把本站全部 Redis 文章按认知顺序组织成 %d 站学习线路：打底、Pipeline 与网络往返、"
                  "数据结构与访问模式、过期与淘汰、集群与高可用，另附随查手册。",
        "fit": {
            "audience": "用 Redis 做缓存/计数并想知道性能上限在哪的后端；踩过 Pipeline 变慢、大 key、热点 key 坑的开发者。",
            "prereq": "用过 Redis 的基本数据类型（String/Hash/ZSet），对「网络往返 RTT」有直觉即可，不需要读过度盘源码。",
            "outcome": "能独立完成 Pipeline 批量优化与热点 key 治理；能对哨兵 vs 集群做写入选型；能看懂并复现本站压测数据。",
        },
        "shortcut": {
            "title": "15 站里最常被翻的 3 站",
            "stems": ["场景1-Lettuce-Pipeline压测问题排查与优化", "场景7-Cluster跨槽Pipeline错误轰炸",
                      "场景11-String三种编码int-embstr-raw压测"],
        },
    },
    {
        "slug": "es-path",
        "name": "Elasticsearch",
        "stages": ES_STAGES, "refs": ES_REFERENCE, "order": ES_ORDER,
        "dir": "deep/es",
        "keywords": "Elasticsearch, ES, 学习路线, 学习线路, 分片, 路由, ILM, Reindex, 冷热分层",
        "desc_t": "把本站全部 Elasticsearch 文章按认知顺序组织成 %d 站学习线路：写入链路、路由与分片分布、"
                  "索引生命周期 ILM、重建迁移与冷热分层，另附随查手册。",
        "fit": {
            "audience": "负责日志/检索类 ES 集群、被分片倾斜或 ILM 配置折磨过的开发者与运维；想在迁移重建时不停服的后端。",
            "prereq": "用过 ES 的 index 增删查，知道「分片、副本」是什么概念即可；Lucene 内部结构不要求提前掌握。",
            "outcome": "能独立完成分片倾斜的线上应急；能配置一套 ILM 生命周期并排障；能用 Reindex + 别名完成零停机迁移。",
        },
        "shortcut": {
            "title": "9 站里最常被翻的 3 站",
            "stems": ["ES7.17 写入全流程-2", "ES路由机制+MurmurHash3算法+分片倾斜全套完整版文档（生产落地）",
                      "一、ES分片倾斜线上应急处理步骤（可直接照着操作）"],
        },
    },
    {
        "slug": "k8s-path",
        "name": "K8s",
        "stages": K8S_STAGES, "refs": K8S_REFERENCE, "order": K8S_ORDER,
        "dir": "deep/k8s",
        "keywords": "K8s, K3s, 学习路线, 学习线路, StatefulSet, 哨兵, Ingress, HPA, 滚动更新",
        "desc_t": "把本站全部 K8s 文章按认知顺序组织成 %d 站学习线路：从 Compose 到 K3s、有状态服务部署、"
                  "流量入口与弹性，另附随查手册。",
        "fit": {
            "audience": "想把 docker-compose 项目搬上集群的后端；想让服务具备自动扩缩容与零停机更新的开发者。",
            "prereq": "用过 docker / docker-compose，理解镜像与容器的区别即可；不需要预先会写 YAML 复杂资源。",
            "outcome": "能独立在 K3s 上部署有状态服务（StatefulSet + PVC）并配好哨兵拓扑；能落地 Ingress、HPA 与零失败滚动更新。",
        },
        "shortcut": {
            "title": "7 站里最常被翻的 3 站",
            "stems": ["compose-vs-k8s", "redis-replication", "ROLLING-UPDATE"],
        },
    },
    {
        "slug": "agent-path",
        "name": "AI Agent",
        "stages": AGENT_STAGES, "refs": AGENT_REFERENCE, "order": AGENT_ORDER,
        "dir": "deep/agent",
        "keywords": "AI Agent, 学习路线, 学习线路, RAG, MCP, 护栏, 评测, 工程化",
        "desc_t": "把本站全部 AI Agent 文章按认知顺序组织成 %d 站学习线路：基础闭环与工具调用、"
                  "RAG 检索与评测、护栏容错与可观测、MCP 与工具接入、端到端工程化与质量评估，另附随查手册。",
        "fit": {
            "audience": "调过 LLM API 但没系统化工程化的开发者；想把 Agent 从 demo 推向生产、关心护栏/成本/评测的人。",
            "prereq": "会用 Java 或 Python 调用 LLM 接口，理解 prompt 与函数调用的基本概念；不需要框架使用经验。",
            "outcome": "能独立搭建带舱壁护栏、语义缓存与熔断的 Agent 服务；能设计 RAG 评测集并接入 CI 质量门禁；能用 MCP 把工具接入协议化。",
        },
        "shortcut": {
            "title": "8 站里最常被翻的 4 站",
            "stems": ["AI-Agent新手教学", "RAG代码与原理详解", "phase4-护栏代码与原理详解",
                      "phase6-评测代码与原理详解"],
        },
    },
]


def main():
    catalog = scan_catalog()
    titles = {e["rel_url"]: e["title"] for e in catalog}
    for p in PATHS:
        body = build_body(p["name"], p["stages"], p["refs"], p["order"], titles, p["dir"],
                          fit=p.get("fit"), shortcut=p.get("shortcut"))
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
