---
title: "ILM 全面实操手册（针对 ES 7.2 的合并修订版）"
summary: "直接结论（要点）："
date: 2026-08-09
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - ILM
category: deep
series: "es"
slug: "ILM 全面实操手册（针对 ES 7.2 的合并修订版）"
reading_time: 30
featured: false
draft: false
---
直接结论（要点）：

- 我检查了你刚发的 7.2 文档片段，发现若干与 ES 7.2 版本兼容性、API/setting 名称、脚本健壮性以及操作风险的误导点（尤其是 data_tier、ILM snapshot、searchable snapshots/SLM、Math.abs 溢出、split/shrink 的约束与前置条件等）。我已修正示例脚本中的取模/溢出问题、补充了 7.2 可行的替代做法（使用 node attributes 做冷热分层分配）、标注需要升级到 >=7.10 才能用 data_tier/searchable snapshots/SLM 等新特性，以及把两份内容（你最初那份我之前修订的文档 + 你最新的 7.2 片段）合并成一份面向 ES 7.2 的、可直接落地的完整手册并在文中标注版本差异与注意事项。

下面先简短列出“在你最新片段里发现的问题 & 建议”，然后把修订并合并后的完整文档以一个文件块输出（可直接复制到知识库/Confluence）。

主要问题与建议（摘要）

1. data_tier / index.routing.allocation.data_tier 用法
   - 问题：你的示例中大量使用 index.routing.allocation.data_tier、freeze/searchable snapshots、SLM 等，ES data tiers / searchable snapshots / SLM 这些特性在 7.2 并不完整或不可用（data_tier/searchable snapshots 为 7.10+ 等；SLM 在更高版本）。  
   - 建议：针对 7.2，使用 node attributes（node.attr.box_type/hot/warm/cold）并用 index.routing.allocation.require.xxx 做分配；并在文档里显式标注哪些示例仅在 >=7.10 可用，建议升级或采用兼容替代方案（如手动 snapshot + 自研定时任务 / curator）。
2. Math.abs 溢出与 Painless 脚本
   - 问题：示例脚本中使用 Math.abs(...hashCode()) 可能产生 Integer.MIN_VALUE 问题，导致负数或异常行为。  
   - 建议：改为安全取模：int suffix = Math.floorMod(hash, N) 或 (hash & 0x7fffffff) % N。已对 reindex 脚本做了修正示例。
3. split/shrink 的约束描述需要更精确
   - 补充：split 要求索引是只读并且目标分片数必须是当前主分片数的整数倍；shrink 要求把所有分片移动到同一节点、索引为只读，并且目标分片数必须能被当前分片数整除；两者不能修改路由规则本身。
4. ILM 内置 snapshot action 与 SLM
   - 提醒：ILM 在 7.2 里没有“在 delete 前内置 snapshot”这一通用动作（snapshot ILM action 在更高版本中才存在），所以“ILM delete 前自动快照”这个操作在 7.2 不能直接写入 ILM，需要借助外部 SLM（若版本支持）或运维脚本在 delete 前手动做 snapshot。
5. searchable snapshots / SLM
   - 说明：searchable snapshots 与 SLM 不是 7.2 的可用特性，若想用这类功能需升级到支持版本（>=7.10/7.11 及更高）。
6. warm_to_hot.sh 脚本中对 settings 查询/过滤实现需改进
   - 建议：脚本示例应根据你集群实际 node attribute 名称来筛选索引设置，且要用 GET _settings 并用 jq 解析 settings 字段而不是直接按 url 过滤。
7. 路由变更风险的强调
   - 必须更明确：单纯在写入端改变 routing（新 routing）并不会迁移旧数据；要真正“重新哈希”历史数据，必须 Reindex（或使用迁移脚本/双写+追赶增量）并做别名原子切换。已把这一点放在显眼位置。

现在把两份文档合并、修订并适配 ES 7.2 的最终版本（包含修正、兼容替代与注释）。此文件同时保留对升级可获得更好功能的建议和回退方案。你可以直接复制使用；如果确认你的集群有具体 node attribute 名称或你愿意升级版本，我可以把模板和脚本进一步贴合你环境并生成一键执行脚本。

文档（合并修订版，针对 ES 7.2，并标注版本差异）

```markdown
# Elasticsearch 分片 / 路由 / ILM 全面实操手册（针对 ES 7.2 的合并修订版）

版本说明
- 本文档以 Elasticsearch 7.2 为主，示例和命令兼顾 7.x 常见用法。  
- 重要：部分更先进的特性（data tiers / searchable snapshots / SLM / ILM 内置 snapshot 动作等）在 7.2 中不可用或行为与高版本不同，我在文中明确标注并给出 7.2 的替代方案（通常是 node attributes + 手动 snapshot / 自研脚本）。推荐有条件尽快升级到 >=7.10 以使用 data tiers / searchable snapshots 等新特性。

---

## 一、索引 / 分片 / 节点 层级关系（概念回顾）

- 层级结构：Cluster → Index → Primary Shards + Replica Shards → shards 部署在 Node → Node 依托服务器磁盘/内存/CPU
- 核心两条约束：
  1. 一个分片（shard）永久只归属一个索引，分片不可跨索引共享。
  2. 同一分片的主分片与其副本不允许部署到同一节点（也可扩展为机架/可用区隔离）。

示例（3 节点、index order，9 主分片 + 1 副本 → 总分片 18）：
- 总分片 = 主分片数 × (副本数 + 1) = 9 × (1 + 1) = 18

说明：
- 节点可以承载任意索引分片；分片为索引的存储单元，分片数在创建索引后不可直接变更（需通过 split/shrink/reindex 等受限操作）。

---

## 二、路由寻址底层原理 & MurmurHash3

### 路由计算（核心逻辑）
ES 在写入/查找时根据 routingKey 的 UTF-8 bytes 使用 Murmur3 32-bit 哈希，然后对 primary shard count 取模得到 shard id。伪代码（安全取模）：

```java
String routingKey = (customRouting != null) ? customRouting : document_id;
int hash = Murmur3.hash32(routingKeyBytes); // Murmur3 on UTF‑8 bytes
int shardId = Math.floorMod(hash, number_of_primary_shards);
// 或：int shardId = (hash & 0x7fffffff) % number_of_primary_shards;
```

要点：

- 不要使用 Math.abs(hash) 直接取模（会在 hash == Integer.MIN_VALUE 时仍返回负数）。
- number_of_primary_shards 在索引创建后不能任意变更（需要通过 split 或 reindex 等受限流程）。
- 同一 routingKey 的计算结果在索引生命周期内保持一致（除非变更主分片数或重建索引）。

### MurmurHash3 版本用途

- 32-bit（MurmurHash3_x86_32）：用于 routing/hashing 到分片。
- 128-bit（MurmurHash3_x64_128）：用于更高精度的 cardinality/去重场景（更低碰撞概率）。

说明：哈希碰撞是正常现象；如果多 key 落同一分片，分片内部仍靠 _id 区分文档，不会直接覆盖数据。对路由变更要格外小心（见下文）。

### Routing 字符串长度规范

- 推荐长度 6 ~ 30 字符（参考值）。过长增加传输开销/URL 长度问题，过短或离散度低易发生哈希集中。

---

## 三、两类分片倾斜成因 + 路由二次打散解决方案

### 类型1：路由数据倾斜（业务层面）

- 原因：业务自定义 routing（省份/日期/商户ID）导致热点 key 长期落在单个分片。
- 典型场景：按日期或省份路由导致单天或少数省份分片数据远大于其它分片。

根治：路由二次后缀打散（写入端拆分热点 key）

- 写入：bizKey_suffix（如 guangdong_0, guangdong_1）
- 查询：对 N 个后缀并行检索并合并结果

改良的 Painless/Java 示例（用于 Reindex 生成新 routing，避免 Math.abs 溢出）：

```java
// Reindex 脚本示例（Painless）
int hash = ctx._source.orderId != null ? ctx._source.orderId.hashCode() : 0;
int suffix = Math.floorMod(hash, 3); // 更安全的取模写法
ctx._routing = ctx._source.province + "_" + suffix;
```

重要风险提示（必须显著强调）

- 单纯在新写入时改变 routing 无法迁移历史数据；若文档使用相同 `_id` 但不同 routing，将落到不同分片，可能导致“看上去存在重复或更新失效”。要把历史数据“真正重新哈希”，必须 Reindex（或在迁移期间做双写 + 增量追赶 + 别名切换）。

### 类型2：节点分片分配倾斜（集群层面）

- 原因：磁盘水位、balance 阈值、迁移并发限制、allocation filter、节点角色/属性导致短期无法迁移分片。
- 处理：调整并发、清理磁盘、检查 allocation awareness、调整 balance 阈值或扩容。

---

## 四、分片节点分配算法完整解析（补充与细化）

- 前置过滤：主副本不在同一节点 / 磁盘水位 / 节点角色 / 节点离线 / 单节点分片数限制。
- 候选节点打分：IndexBalance（权重）与 ShardBalance（权重）加权得到综合分数，低分优先分配。
- 均衡阈值：cluster.routing.allocation.balance.threshold（默认 1.0）控制是否触发 rebalance。
- 磁盘水位：low/high/flood_stage（默认值可配置；请以集群配置为准）。

并发配置示例（调优迁移并发）：

```http
PUT _cluster/settings
{
  "transient": {
    "cluster.routing.allocation.node_concurrent_incoming_recoveries": 2,
    "cluster.routing.allocation.node_concurrent_outgoing_recoveries": 2,
    "cluster.routing.allocation.cluster_concurrent_rebalance": 2,
    "indices.recovery.max_bytes_per_sec": "40mb"
  }
}
```

其他补充：

- 使用 allocation awareness（如 rack/zone）增加容错。
- index.routing.allocation.total_shards_per_node 可限制单节点最大分片数。

---

## 五、索引与节点容量规划规范（生产落地标准）

- 单分片建议容量：
  - 热数据：20~35 GB
  - 常规：35~50 GB
  - 冷归档（若使用搜索快照/对象存储可更大）：60~80 GB
- 主分片数估算：预计未来数据 ÷ 目标单分片容量（建议留 20–50% 弹性）
- 单节点分片上限：保守 ≤ 800，警戒 ≤ 1000（取决于 master heap、集群规模）
- 单索引大小上限：
  - 小集群（3~5 节点）：单索引 ≤ 1.5 TB
  - 中大型集群：单索引 ≤ 3 TB

---

## 六、ILM （Index Lifecycle Management）冷/温/热分层（7.2 兼容说明）

### 注意（版本兼容）

- ES 7.2 支持 ILM，但“data_tier”（index.routing.allocation.data_tier）和 searchable snapshots / SLM / ILM snapshot 动作在 7.2 中不可用或有差异。  
- 在 7.2 中实现冷热分层通常通过 node attributes（例如 node.attr.box_type=warm）并在索引设置中使用 index.routing.allocation.require.box_type: "warm" 来约束。

核心原则（与高版本相同）：

- 流转最小单位为整个索引；ILM 默认由内置轮询服务驱动；Hot→Warm→Cold→Delete 是单向流程（无自动回流）。

示例 ILM 策略（7.2 常用字段，rollover 使用 max_size）：

```json
PUT _ilm/policy/order_hot_warm_cold_policy
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {"max_age":"7d", "max_size":"45gb"},
          "set_priority":{"priority":100}
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "allocate":{"require":{"box_type":"warm"}}, 
          "forcemerge":{"max_num_segments":1},
          "shrink":{"number_of_shards":3},
          "set_priority":{"priority":50}
        }
      },
      "cold": {
        "min_age":"30d",
        "actions": {
          "allocate":{"require":{"box_type":"cold"}},
          "freeze": {},   // freeze 在 7.x 中存在，但注意其实现与高版本差异
          "set_priority":{"priority":10}
        }
      },
      "delete": {
        "min_age":"90d",
        "actions": {"delete": {}}
      }
    }
  }
}
```

- 注意：上例中 allocate 的 key 使用 box_type（即 node.attr.box_type）来兼容 7.2。你需要在节点启动参数或配置中设置 node.attr.box_type: hot/warm/cold。

### 关于在 delete 前自动 snapshot（7.2 限制）

- ILM 在 7.2 中通常没有内置 snapshot 动作；如果你想在 delete 前先保存快照，需要：
  1. 在 delete 前使用外部 SLM（若你的版本支持）或
  2. 在到期前通过运维脚本/调度任务（Crontab）调用 Snapshot API (`PUT _snapshot/...`) 做快照，然后再允许 ILM 删除，或
  3. 升级到支持 ILM snapshot action 或 SLM 的 ES 版本。

---

## 七、Reindex 数据重构全场景方案（兼容 7.2）

核心流程（零停机改造模版）

- 创建目标索引（new settings/mappings/ILM）→ 全量 Reindex（可用 Painless 脚本进行字段变换与生成新 routing）→ 双写（旧索引 + 新索引）→ 增量追赶 → 原子别名切换 → 停止双写 → 清理旧索引/归档。

Reindex 示例（含安全取模）：

```http
POST _reindex?slices=auto&requests_per_second=2000
{
  "source": {"index": "order_old"},
  "dest": {"index": "order_new"},
  "script": {
    "source": """
      int h = ctx._source.containsKey('orderId') ? ctx._source.orderId.hashCode() : 0;
      int suffix = Math.floorMod(h, 3);
      ctx._routing = ctx._source.province + "_" + suffix;
    """
  }
}
```

最佳实践：

- 使用 slices 并行化迁移：`?slices=auto` 或指定数值
- 限流：`requests_per_second` 控制迁移 IO
- 长时间任务使用 `wait_for_completion=false` 并监控 task API
- 迁移前务必做样本校验（文档总量、字段映射、抽样比对）

不可原地修改清单：

- 新增字段：可直接 PUT mapping
- 修改字段类型 / 重命名 / 修改 analyzer：必须重建索引 + Reindex

split/shrink 约束（补充）：

- split：目标主分片数必须为当前分片数的整数倍，且索引必须设置为只读（read-only）并满足 relocation 条件。
- shrink：必须把索引的所有分片聚集到单个节点（需要有节点能承载），索引需只读，目标分片数必须能被当前分片数整除。
- 两者都不会更改 routing 逻辑（不能改变路由规则）。

---

## 八、索引别名架构设计 + 索引上线初始化标准模板（含 rollovers）

核心认知

- 别名是虚拟引用（alias），业务读写应始终通过 alias 操作，物理索引可随时切换（原子操作）。
- 在索引创建之初就绑定业务别名，便于后续重构（Mapping/Shard/Route）无缝切换。

示例：创建索引并绑定别名（7.2 可用写法）：

```json
PUT /order_v1
{
  "settings": {
    "number_of_shards": 9,
    "number_of_replicas": 1,
    "index.lifecycle.name": "order_hot_warm_cold_policy",
    "index.routing.allocation.require.box_type": "hot"  // 7.2 使用 node.attr.box_type
  },
  "mappings": {
    "properties": {
      "orderId": {"type":"keyword"},
      "province": {"type":"keyword"},
      "createTime": {"type":"date", "format":"yyyy-MM-dd HH:mm:ss||epoch_millis"},
      "amount": {"type":"double"}
    }
  },
  "aliases": {
    "alias_order": {}
  }
}
```

原子切换 alias（示例）：

```http
POST _aliases
{
  "actions":[
    {"remove":{"index":"order_v1","alias":"alias_order"}},
    {"add":{"index":"order_v2","alias":"alias_order"}}
  ]
}
```

Rollver（时序索引）关键点

- 在 7.2 使用别名 + is_write_index 来做 rollover；模板需匹配 `order-*`。
- 保证写入别名始终指向 is_write_index: true 的物理索引。

---

## 九、Rollover 时序滚动索引（日志/时序）完整初始化（7.2）

步骤概要：

1. 建立 index template（pattern：order-*）以约束新索引的 shards/mappings/ILM/allocate.require。
2. 创建首个 index（order-000001）并把 `alias_order_roll` 设为 is_write_index:true。
3. 写入都通过 alias_order_roll 写入，rollover 条件触发自动新建 order-000002。

示例模板（7.2）：

```json
PUT _index_template/template_order_roll
{
  "index_patterns": ["order-*"],
  "template": {
    "settings": {
      "number_of_shards": 9,
      "number_of_replicas": 1,
      "index.lifecycle.name": "order_hot_warm_cold_policy",
      "index.routing.allocation.require.box_type": "hot"
    },
    "mappings": {
      "properties": {
        "orderId": {"type":"keyword"},
        "province": {"type":"keyword"},
        "createTime":{"type":"date","format":"yyyy-MM-dd HH:mm:ss||epoch_millis"},
        "amount":{"type":"double"}
      }
    }
  },
  "priority": 20
}
```

初始化首索引并别名写入：

```json
PUT /order-000001
{
  "aliases": {
    "alias_order_roll": {"is_write_index": true}
  }
}
```

注意：rollover 的管理/触发/ILM 配置要与模板一致。

---

## 十、ILM 反向回流（Warm/Cold 向 Hot 回退）—— 7.2 实操

核心认知：

- ILM 本质为单向流转；回流必须人工触发（修改 index.routing.allocation.require.box_type 为 hot，并暂停 ILM，等待搬迁完成，再恢复 ILM）。  
- 若你用了 shrink，收缩后的分片数量不可逆（只能 Reindex 还原）。

示例回退步骤（Warm → Hot）：

```http
GET your_index/_settings?filter_path=*.routing.allocation.require*

PUT your_index/_settings
{
  "index.routing.allocation.require.box_type": "hot"
}

POST your_index/_ilm/stop

# 等待 recovery 完成（查看 _cat/recovery?v）
# 回迁完毕后恢复生命周期
POST your_index/_ilm/start
```

注意：

- 若索引被 freeze（冷层常见），需要在回迁前移除 freeze（7.2 freeze 行为存在，注意版本差异）。
- delete 阶段的索引若被 ILM 删除则无法回退，唯一办法是从快照恢复（必须提前做快照）。

---

## 十一、快照 / 备份（7.2 实务）

重要提醒：

- ES 默认没有自动快照、没有“回收站”；在 ILM delete 前自动快照的能力 **在 7.2 中不可用**（若该功能对你重要，请考虑升级或使用外部脚本/调度工具做 delete 前快照）。

手动创建快照仓库（示例）：

```http
PUT _snapshot/es_backup_repo
{
  "type":"fs",
  "settings": {"location":"/data/es_snapshot","compress":true}
}
```

创建定时快照（7.2 没有 SLM，使用系统调度或 curator 等工具）：

- 使用 crontab 或 Jenkins 每日调用 Snapshot API（`PUT _snapshot/...`）。
- 保存至少 N 份备份并验证恢复流程。

恢复示例：

```http
GET _snapshot/es_backup_repo/_all

POST _snapshot/es_backup_repo/snap-name/_restore
{
  "indices":"order-000001",
  "include_global_state": false,
  "rename_pattern":"(.+)",
  "rename_replacement":"restored_$1"
}
```

---

## 十二、运维监控与告警（新增建议，便于生产）

建议至少监控：

- cluster health / status
- master heap / GC / large GC events
- cluster state size / pending tasks
- unassigned_shards / relocating_shards / initializing_shards
- disk usage / watermark hits per node
- recovery throughput / bytes/sec
- slow logs（search/index）及 query cache / fielddata 使用

建议告警：

- master heap > 70% 持续 5 分钟
- cluster state size 突增
- unassigned_shards > 0 且持续 10 分钟
- disk usage 超过 high watermark

---

## 十三、常见操作脚本与修正（示例）

1) 修正后的 Reindex 路由脚本（已展示于 Reindex 小节）
2) warm_to_hot.sh（注意：需根据你集群 node attribute 名称调整，且要用 GET _settings 并用 jq 解析 settings 字段）

脚本示例（改为通过 settings 字段判断 box_type 为 warm 的索引）：

```bash
#!/bin/bash
ES_HOST="http://127.0.0.1:9200"
# ES_USER/ES_PWD if needed

# 获取所有索引 settings 并筛选出 box_type=warm 的索引（需要 jq）
curl -s "$ES_HOST/_settings?pretty" | jq -r 'to_entries[] | select(.value.settings.index."routing.allocation.require".box_type == "warm") | .key' > warm_indices.txt

while read index; do
  [ -z "$index" ] && continue
  echo "处理索引: $index"
  curl -s -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d '{"index.routing.allocation.require.box_type":"hot"}'
  curl -s -X POST "$ES_HOST/$index/_ilm/stop"
  echo "$index 已申请回迁至 hot"
done < warm_indices.txt
```

说明：

- 上面 jq 表达式需根据你设置的实际 settings 键来调整（例如若你在节点配置里是 node.attr.box_type 那么 settings 路径为 index.settings.index.routing.allocation.require.box_type）。
- 如果你用的是 data_tier（>=7.10），脚本需要改用 data_tier 的 settings 字段。

---

## 十四、面试 / 总结要点（3 分钟版）

- ES 使用 MurmurHash3（32-bit）对 routing 字符串的 UTF-8 bytes 做哈希，取模得到 shard id；主分片数量一旦创建便不能随意修改。  
- 自定义 routing 导致热点是常见问题；解决方式通常为：路由二次打散 / 新建索引 + Reindex 并 atomic alias 切换（零停机）；rebalance 无法改变文档所属分片（只会搬迁分片到别的节点）。  
- ILM 管理索引生命周期（Hot→Warm→Cold→Delete），回流必须靠人工修改 allocation 设置并暂停 ILM； delete 阶段不可逆，必须事先做快照。  
- 在 ES 7.2 上，推荐使用 node attributes（node.attr.box_type）+ index.routing.allocation.require.* 做冷热层级分配；若想使用 data_tier / searchable snapshots / SLM 等新功能，应评估升级计划到 >=7.10。  

---

# 附：我已具体修改的地方（供审计）

- 修正了所有示例脚本中 Math.abs(...) 的用法，改用 Math.floorMod 或 bitmask，避免 Integer.MIN_VALUE 溢出问题。
- 把 index.routing.allocation.data_tier 的示例替换为兼容 7.2 的 node.attr + index.routing.allocation.require 示例，并在文中明确标出 data_tier 为 >=7.10 的特性。
- 标注 ILM delete 前自动 snapshot 在 7.2 中不可用，并给出两种替代方案（手动 snapshot 方案与独立 SLM 策略 / 调度脚本）。
- 补充 split/shrink 的前置条件与约束说明。
- 修正并改进 warm_to_hot.sh 示例实现的索引筛选逻辑（用 GET _settings + jq）。
- 合并并重构原两份内容的章节结构与示例，使文档成为一份完整可读的 7.2 运维手册。


