---
title: "ILM 全面实操手册（修订版）"
summary: "结论先行：我已经逐项检查并修改了你的文档，找出若干技术不准确或需要补充的点（尤其是路由哈希取模、路由变更的风险、ILM/rollover 字段与模板写法、shrink/split …"
date: 2026-08-07
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - ILM
category: deep
series: "es"
slug: "ILM 全面实操手册（修订版）"
reading_time: 30
featured: false
draft: false
---
结论先行：我已经逐项检查并修改了你的文档，找出若干技术不准确或需要补充的点（尤其是路由哈希取模、路由变更的风险、ILM/rollover 字段与模板写法、shrink/split 约束、备份/可搜索快照等），并把改正与补充后的完整文档一起给出。下面先列出“问题 & 建议修改点”，再给出修订后的完整文档（可直接复制到 Markdown/知识库）。

主要问题与我做的改动（要点）

1. MurmurHash / 取模实现的细节（重要）
   - 问题：原文示例使用 Math.abs(hash) % shards，会在 hash == Integer.MIN_VALUE 时仍返回负数或异常情况（Math.abs(Integer.MIN_VALUE) == Integer.MIN_VALUE），这是经典 bug。
   - 处理：改为安全写法：int shardId = Math.floorMod(hash, number_of_primary_shards) 或 (hash & 0x7fffffff) % number_of_primary_shards，并说明 ES 用 UTF-8 bytes 做 Murmur3 hash。
2. 路由变更风险（关键修改）
   - 问题：文中写到“路由二次打散仅修改 _routing 字段，_id、业务主键保持不变，ES与MySQL可以依靠唯一主键对账” —— 这有误导风险。
   - 说明/更正：如果你在写入时改变 routing 而不对历史数据做 reindex，使用相同 _id 但不同 routing 会落在不同分片，可能导致“逻辑重复/更新失效/查不到原记录”等问题。必须明确：要变更已存在文档的路由（让已存量跟新路由一致），必须 reindex 或在更新/查询时显式使用原始 routing；否则只能用于新写入数据，或在做双写+Reindex迁移后切换别名。
3. MurmurHash 版本描述（微调/补充）
   - 原文总体正确（32 位用于路由、128 位用于更高精度的去重），但补充说明 ES 的实现细节（routing 用 Murmur3 32-bit 对 UTF-8 字节哈希；cardinality/去重可能使用 128-bit 哈希作为 HLL/hash 输入）并建议注意版本与 ES 版本关联。
4. Java 路由打散工具改进
   - 改进：用 Long.hashCode + Math.floorMod 做取模，避免 Math.abs 溢出，并增加空值校验与注释；同时补充并发/分布式唯一性注意事项（不要把 _id 与不同 routing 混用造成重复）。
5. ILM 策略里的 rollover 配置项与 index template 写法
   - 问题：示例中使用的 rollover 字段名（如 max_primary_shard_size）和 index.routing.allocation.data_tier 写法在不同 ES 版本间有差异，可能导致误用。
   - 处理：统一用兼容写法示例： rollover: { "max_age": "7d", "max_size":"45gb" }；template 中 use "index.routing.allocation.require.data_tier": "hot"（或根据集群版本改为 data_tier 相关 setting），并提醒 “请按你的 ES 版本校验字段名”。
6. shrink / split / reindex 的约束 & 可选替代策略
   - 补充：明确 shrink/split 的前置条件（read-only/index on single node/all shards relocated to one node 等）、split API 可在一定约束下放大主分片数（必须为当前分片数的倍数并且索引为只读），并建议在大型或生产索引上优先使用 reindex/rollover+alias 的零停机迁移流程。
7. 关于分片大小建议：补充可搜索快照、snapshot lifecycle（SLM）
   - 建议增加关于 searchable snapshots（把冷数据用对象存储挂回查询）与 SLM 的内容，作为冷归档成本控制与恢复策略。
8. 监控与运维建议（新增）
   - 补充监控指标（master heap、cluster state size、pending tasks、large cluster state diffs、shard count per node、unassigned shards、recovery throughput、disk watermark hits、fielddata/query cache 等），以及告警建议。
9. 其它细节修正与措辞优化
   - 如“单分片容量建议范围”的理由补强、分片数量估算公式中加入“预留 20–50% 弹性”的建议、单节点分片上限建议补充对堆内存影响的说明、ILM poll_interval 默认值确认说明并提示各版本差异等。

我还做的具体修改（实现）

- 修正/替换了代码片段（路由哈希取模、Java 工具类），并加入更健壮的实现与注释。
- 调整 ILM 示范 JSON（使用更兼容的 rollover 参数与 index template 的 data_tier 设置），并在注释里提醒“按 ES 版本检查字段名”。
- 新增关于：split API、shrink 要求、reindex 并发(slice)、reindex requests_per_second、snapshot/SLM、searchable snapshots、分配意识（rack/zone awareness）、监控项、以及变更 routing 的风险与迁移流程。
- 语言与结构上做了统一排版、增加小节标题，便于阅读和落地执行。

下面给出修订后完整文档（已合并修正与补充）。你可以直接复制使用；我在各关键点保留了注释/说明以便实践时参考。

```markdown
# Elasticsearch 分片 / 路由 / ILM 全面实操手册（修订版）

> 版本说明：本文档为实践型运维手册，兼顾 Elasticsearch 常见版本差异（7.x / 8.x）。示例 API/字段在不同 ES 版本可能有细微差别（例如 rollover 的某些字段名、data_tier 相关设置等），上线前请以集群实际版本的官方文档为准。

---

## 一、索引 / 分片 / 节点 层级关系（概念回顾）

- 层级结构：Cluster → Index → Primary Shards + Replica Shards → shards 部署在 Node → Node 依托服务器磁盘/内存/CPU
- 核心两条约束：
  1. 一个分片（shard）永久只归属一个索引（分片物理文件独立），**不能跨索引共享分片**。
  2. 同一分片的主分片与其副本**不允许**部署到同一节点（或同一机架/可用区），避免单点故障导致数据不可用。

示例（3 节点、index order，9 主分片 + 1 副本 → 总分片 18）：
- 总分片 = 主分片数 × (副本数 + 1) = 9 × (1 + 1) = 18
- 理论均分：3 节点，每节点承载 6 个该索引的分片（实际受其他索引/磁盘水位/allocations 策略影响）

说明：
- 节点：承载任意索引分片的资源容器
- 分片：索引的存储单元；分片归属创建后固定（不可随意改主分片数量）

---

## 二、路由寻址底层原理 & MurmurHash3

### 1) 路由计算（核心逻辑）
索引一下写入/查找时底层计算分片的核心思路（伪代码）：

```java
// routingKey = (user-provided routing != null) ? routing : document_id;
int hash = Murmur3.hash32(routingKeyBytes); // Murmur3 on UTF-8 bytes -> 32-bit signed int
// 安全取模（避免 Math.abs 溢出问题）
int shardId = Math.floorMod(hash, number_of_primary_shards);
// 也可用: int shardId = (hash & 0x7fffffff) % number_of_primary_shards;
```

要点：

- ES 对 routing 使用 Murmur3 (32-bit) 对 routingKey 的 UTF‑8 字节做哈希，然后对主分片数取模得到目标主分片 id。
- 注意不要用 Math.abs(hash) 直接取模（可能因 Integer.MIN_VALUE 导致负值/异常）。使用 Math.floorMod(...) 或 bitmask 更安全。

硬性约束：

1. number_of_primary_shards 在索引创建后“不能任意改动”。虽然有 split API（必须满足可拆分条件）与 reindex 可实现变更，但不能直接在线修改主分片数。
2. number_of_replicas 可动态调整，不影响路由。
3. 同一 routingKey 的运算结果（hash → shardId）在索引生命周期内保持一致（除非更改主分片数并用 split/reindex 手段迁移）。

### 2) MurmurHash3 两个版本的使用场景（补充）

- MurmurHash3_x86_32（32-bit）：用于路由分片计算，目标是“打散有序ID”以均匀分布。
- MurmurHash3_x64_128（128-bit）：常用于需要更低碰撞概率的聚合/去重场景（作为哈希输入用于 cardinality/去重），降低去重误差。

说明：哈希碰撞正常存在。碰撞导致多个不同 routingKey 落入同一分片，但文档内仍以 _id 区分，不会直接覆盖（注意上面的 routing 更改带来的风险点见下）。

### 3) Routing 长度规范（建议）

- 过长或过短都不理想。推荐 6 ~ 30 字符（参考值）。
- 注意：实际哈希基于 UTF‑8 bytes，非字符数；且超长 routing 可能导致 URL 长度或网络传参问题（如果 routing 被放在 URL）。
- 如果业务 routing 是数值或雪花 ID，优先 hash/encode 为字符串再使用。

### 4) 路由二次打散（与 MySQL 的兼容性说明）

- 核心思想（写入端打散）：bizKey_suffix（如 guangdong_0、guangdong_1）将热点路由拆分到多个分片上，缓解写入倾斜。
- 严正说明（极重要）：如果你的历史数据已用旧 routing 写入，且你开始用新的 routing 写入新数据，**不能**指望 ES 自动把两者当作同一条记录。具体风险：
  - 使用相同 `_id` 但不同 `routing` 会造成同一索引里存在“重复记录”分布在不同分片上（ES 的主键定位是通过 routing → shard 到达分片后才基于 _id 更新/覆盖）。
  - 若需要把历史数据的 routing 一并变更，必须 reindex（或在写入时同时双写新/旧 routing 并做迁移与对账），然后原子切换 alias。
- 结论：路由打散适用于新写入的热点分散；对于已有数据要变更 routing，必须走 reindex/迁移流程，且严格测试。

---

## 三、两类分片倾斜成因 + 路由二次打散解决方案

### 类型1：路由数据倾斜（业务层面）

- 成因：业务自定义 routing（省份/日期/大客户ID 等）导致热点 key 长期落在单个分片。
- 典型场景：按日期或省份路由导致单天或少数省份分片数据远大于其它分片。
- 根治（常用）：路由二次后缀打散（原 bizKey_0..n）
  - 写入：使用 bizKey + "_" + suffix（suffix 可用 snowflake id.hash() % N）
  - 查询：若需要查询某 bizKey 全量，需要对 N 个后缀并行检索（或使用 routing 列表参数）
- 风险与代价：查询 IO 与聚合复杂度会上升；聚合需要按后缀合并或在文档中保留原始 bizKey 字段用于聚合。

改良的 Java 示例（更健壮）：

```java
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

public class RoutingSplitUtil {
    // 写入生成打散 routing（更安全的取模）
    public static String getSplitRouting(String bizKey, int splitNum, Long snowId) {
        if (splitNum <= 1 || bizKey == null || snowId == null) return bizKey;
        int hash = Long.hashCode(snowId);
        int suffix = Math.floorMod(hash, splitNum); // 安全取模，避免 Math.abs 溢出问题
        return bizKey + "_" + suffix;
    }

    // 查询时遍历所有后缀（用于批量检索）
    public static List<String> listAllRouting(String bizKey, int splitNum) {
        List<String> list = new ArrayList<>();
        if (splitNum <= 1 || bizKey == null) {
            list.add(bizKey);
            return list;
        }
        for (int i = 0; i < splitNum; i++) {
            list.add(bizKey + "_" + i);
        }
        return list;
    }
}
```

注意：

- 在聚合/去重/关联场景中，保留原始业务主键字段（如 bizKey）作为文档字段是必须的，方便跨后缀聚合。
- 对于需要「在同一 _id 上频繁更新」的业务，不建议随意改变 routing 逻辑，否则会出现更新丢失或重复。

### 类型2：节点分片分配倾斜（集群层面）

- 成因：分片分配受限（磁盘水位、均衡阈值、搬迁速率、节点角色、allocation awareness 等），短期内迁移受限会导致分片分布不均。
- 诊断与处置：检查 allocation/filter/watermark、cluster.routing.allocation.* 配置与搬迁并发参数、pending migrations、rebalance 阈值。

---

## 四、分片节点分配算法完整解析（补充与细化）

### 1) 分配流程

- 前置过滤（优先级最高）：同主副本同节点/磁盘水位/节点角色/节点离线/单节点分片上限等。
- 候选节点打分：IndexBalance（0.55）与 ShardBalance（0.45）加权得分（ES 默认权重可以配置），得分越低越优先。
- rebalance 受 cluster.routing.allocation.balance.threshold 等阈值控制。

补充：

- 使用 allocation awareness（如 rack/az 属性）可强制把主副本分散到不同机架/可用区，避免单机架故障影响可用性。
- 当集群为 yellow（没有足够节点放置副本）时需要扩容节点或降低副本设置。

### 2) 磁盘水位（默认值可配置）

- low（默认 85%）：开始避免砖入并尝试迁出分片
- high（默认 90%）：禁止迁入新分片
- flood_stage（默认 95%）：索引会被设置只读，防止写入导致磁盘满
  （注：默认阈值可能随 ES 版本不同，请以集群当前配置为准）

### 3) 分片迁移并发配置（示例）

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

- 上线策略：高峰期调低并发与带宽；低峰期适当抬高以加速收敛。

### 4) 额外补充

- index.routing.allocation.total_shards_per_node 可以限制单节点承载的总分片数。
- 定期合并（forcemerge）和 shrink 操作会带来大量 IO，请避开业务高峰。

---

## 五、容量规划与分片数建议（实践版）

### 1) 单分片容量建议（依据场景）

- 热数据（高写高查）：20 ~ 35 GB / 分片
- 常规业务：35 ~ 50 GB / 分片
- 冷归档（极少查询）：可放宽至 60 ~ 80 GB（建议使用 searchable snapshots 优化成本）

说明：分片过大增大迁移与恢复成本；分片过多则增加 cluster state、内存及元数据压力。推荐「宁多勿少」的原则，但要为单分片恢复/迁移速度留空间。

### 2) 主分片数估算

- 建议主分片数 ≈ 预计未来 N 年数据总量 ÷ 目标单分片容量（例如 50GB）
- 建议保留弹性（20%-50%）以应对数据增长，避免频繁 reindex

### 3) 副本策略

- 副本数量可动态调整；在单节点集群上副本不会分配（cluster 状态为 yellow）。
- 副本不仅用于高可用，也用于增加并行查询吞吐（更多副本可分担搜索压力）。

### 4) 单节点分片上限

- 保守：<= 800；警戒：<= 1000
- 分片过多会导致 master heap 和 cluster state 较大，影响稳定性与分配效率。

### 5) 单索引总体上限（参考）

- 3~5 节点小集群：单索引总量 ≤ 1.5 TB
- 中大型集群：单索引 ≤ 3 TB
- 时序数据应使用 Rollover + ILM 拆分索引

---

## 六、ILM（Index Lifecycle Management）冷/温/热分层（完整手册）

### 6.1 核心原则（必须理解）

1. 流转最小单位：整个索引（不是单条文档）
2. 计时基准：
   - 普通静态索引：以索引创建时间计时
   - Rollover 索引：以每个滚动生成的新索引的创建时间计时
3. 调度模式：ILM 以内置服务周期性轮询（默认 polling interval），并非严格 cron；默认间隔视 ES 版本（通常为 10m），可调整。
4. 流转不可逆：Hot → Warm → Cold → Delete（若要回流需人工或自研流程）
5. 流转本质：修改 indexing allocation 标签（data_tier/allocate.require）以触发分片重新分配到对应节点池

### 6.2 ILM 优于自研 Cron 的理由（补充）

- 内置幂等、失败自动重试、与迁移限流配置联动、低维护开销
- 唯一短板：轮询周期带来的时间误差（优点：避免整点抖动）

### 6.3 ILM 参数速查与推荐（精简）

- indices.lifecycle.poll_interval：默认 ~10m（按集群规模调整）
  - 测试 1m；时效敏感 3~5m；大型集群 15~20m

Hot 阶段（示例）：

- rollover：max_age / max_size / max_docs（常用 max_size / max_age）
- set_priority: 恢复优先级

Warm 阶段：

- min_age: 7d（示例）
- allocate.require.data_tier: "warm"
- forcemerge.max_num_segments: 1（根据查询频率启用）
- shrink: 目标 shards（注意 shrink 是不可逆并有前置条件）
- readonly: 如果确实无写入

Cold 阶段：

- min_age: 30d（示例）
- allocate.require.data_tier: "cold"
- freeze: 可选（长期归档开启）
- set_priority: 10 之类

Delete 阶段：

- min_age: 按合规设置（示例 90d），并强制先做 snapshot 以防误删

### 6.4 ILM 策略示例（更通用的 rollover 写法）

> 注意：某些字段名（如 max_primary_shard_size）在不同版本间存在差异，以下使用最常见的 max_size。发布前请以集群版本官方文档为准。

```json
PUT _ilm/policy/order_hot_warm_cold_policy
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {
            "max_age": "7d",
            "max_size": "45gb"
          },
          "set_priority": {"priority": 100}
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "allocate": {"require":{"data_tier":"warm"}},
          "forcemerge": {"max_num_segments": 1},
          "shrink": {"number_of_shards": 3},
          "set_priority": {"priority": 50}
        }
      },
      "cold": {
        "min_age": "30d",
        "actions": {
          "allocate": {"require":{"data_tier":"cold"}},
          "freeze": {},
          "set_priority": {"priority": 10}
        }
      },
      "delete": {
        "min_age": "90d",
        "actions": {"delete": {}}
      }
    }
  }
}
```

### 6.5 索引模板绑定 ILM（示例）

> 使用 _index_template（可组合模版）或 legacy 模板根据版本选择

```json
PUT _index_template/template_order
{
  "index_patterns": ["order_*"],
  "template": {
    "settings": {
      "number_of_shards": 9,
      "number_of_replicas": 1,
      "index.lifecycle.name": "order_hot_warm_cold_policy",
      "index.routing.allocation.require.data_tier": "hot"
    },
    "mappings": {
      "properties": {
        "orderId": {"type": "keyword"},
        "province": {"type": "keyword"},
        "createTime": {"type": "date"}
      }
    }
  },
  "priority": 10
}
```

> 注意：部分集群/版本也可使用 `index.routing.allocation.include` / `index.routing.allocation.require` / data_tier 相关字段，按集群版本确认。

### 6.6 ILM 实操注意事项（八条）— 保留并补充

1. ILM 无法自动解决路由层面的热点（路由打散必须由业务或 reindex 实施）。
2. shrink 操作不可逆，且有前置条件（索引需只读且可迁移至单节点上进行 shrink）。
3. ILM 有轮询延迟（默认轮询间隔），短时间误差属正常。
4. forcemerge IO 高，请错峰执行或禁用。
5. 集群必须部署对应 data_tier 节点（hot/warm/cold），否则流转会卡住。
6. 不要随意手工修改索引 data_tier 或 allocation 标签，避免分片乱跑。
7. freeze 适合长期归档但会降低查询速度。
8. Delete 阶段建议先确保 SLM 快照可恢复再启用。

### 6.7 ILM 常见故障排查（快速对照）

- 索引未流转至下一阶段：可能缺乏对应 data_tier 节点或分片搬迁阻塞。命令：GET /index_name/_ilm/explain
- Rollover 不触发：确认写入是否走 alias，确认阈值配置是否合理。命令：GET /_alias/your_alias
- 冻结后查询慢：检查 index settings 的 frozen flag。命令：GET index/_settings?filter_path=*.frozen

---

## 七、Reindex & 迁移全场景方案（补充细化）

### 核心流程（零停机改造模版）

新建目标索引（含新 mapping/setting/ILM） → 执行 Reindex（可用 Painless 脚本转换）→ 双写阶段（业务同时写旧索引与新索引）→ 增量追赶（时间窗口或按 ID 范围重跑）→ 原子别名切换 → 观察与关闭双写 → 删除旧索引/归档

示例 Reindex（脚本转换）：

```http
POST _reindex
{
  "source": {"index": "order_v1"},
  "dest": {"index": "order_v2"},
  "script": {
    "source": """
      if (ctx._source.containsKey('o_id')) {
        ctx._source.order_id = String.valueOf(ctx._source.remove('o_id'));
      }
      if (ctx._source.containsKey('create_dt')) {
        ctx._source.create_time = ctx._source.remove('create_dt');
      }
    """
  },
  "conflicts": "proceed"  // 视需求选择
}
```

最佳实践：

- 大量数据迁移使用 slices 并行化：POST _reindex?slices=10
- 限流：requests_per_second 设置（例如 1000），避免 saturate 网络/磁盘
- 长时间迁移使用 wait_for_completion=false 并监控 task API
- 在跨集群迁移时使用 reindex from remote（注意带宽）

### 不可原地修改清单

- 新增字段：可直接 PUT mapping
- 修改已有字段类型/重命名/改变 analyzer：必须重建索引并 Reindex

---

## 八、备份 / 快照 / 冷归档（补充）

- 强烈建议对核心业务数据使用 Snapshot（到远端仓库，如 S3）的 SLM（Snapshot Lifecycle Management），并在 delete 阶段前确保有可用最小保留快照策略。
- 可搜索快照（Searchable Snapshots）：将冷数据使用对象存储成本更低的方式保留并允许查询，是冷归档的重要手段，配合 ILM cold 阶段可显著降低成本。
- 恢复演练：定期演练从快照恢复，验证策略有效性。

---

## 九、监控与告警（新增建议）

推荐监控指标（至少包含）：

- cluster health / status
- number_of_nodes / master-eligible nodes
- master heap usage / GC / large GC events
- cluster state size / pending tasks
- unassigned_shards / relocating_shards / initializing_shards
- indices recovery throughput / bytes/sec
- disk usage 与 watermark hits（per node）
- fielddata / query cache / request cache 消耗
- slow logs（search/ indexing）

告警示例：

- master heap > 70% 连续 5min
- cluster state size 突增 > 2x baseline
- unassigned_shards > 0 且时间 > 10min
- disk usage > high watermark（90%）

---

## 十、常见问答（FAQ 快速提示）

Q: 索引主分片创建后能否增加？  
A: 不能直接修改。可选方案：split API（满足条件）或新建索引 + reindex。

Q: 改 routing 会影响已写文档吗？  
A: 会。写入时 routing 决定 shard。变更 routing 要么 reindex（把文档重新写入新 routing），要么在读写时兼顾旧 routing，否则会产生分布式重复或更新丢失。

Q: 分片数应多还是少？  
A: 既不宜过多（元数据、Heap 问题），也不宜过少（单分片过大迁移成本高）。按预计规模与单分片目标容量折中，建议略偏多并保留弹性。

---

# 结束语（落地提示）

我已把文档中的技术漏洞与实践盲点修正并补充了运维/迁移/监控相关内容。建议下一步：

- 基于你们集群的 ES 版本（请回复版本号，例如 7.10 / 7.17 / 8.4 等），我可以把示例 API/字段（如 rollover 参数、index.template 写法、data_tier 配置）逐条对齐到你们版本的准确语法并输出最终可跑的 API 脚本。
- 若需要，我可以把“修订后文档”拆成可直接执行的 runbook（例如：路由打散上生产、ILM policy 上线步骤、reindex 零停机脚本、监控与告警 playbook）。

如果你要我继续把示例按你们的 ES 版本调整，请回复你们的 Elasticsearch 主版本号（例如：7.10 / 7.17 / 8.6）。
