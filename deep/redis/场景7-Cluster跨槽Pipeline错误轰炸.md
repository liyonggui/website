---
title: "场景 7: Cluster 跨槽 Pipeline 错误轰炸"
summary: "Redis Cluster 核心规则: - 16384 个 hash slot（slot = CRC16(key) % 16384） - 每个 key 根据 CRC16 哈希只属于…"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - Redis
  - 缓存
category: deep
series: "redis"
slug: "场景7-Cluster跨槽Pipeline错误轰炸"
reading_time: 34
featured: false
draft: false
---
## 场景 7: Cluster 跨槽 Pipeline 错误轰炸

### 7.1 背景与问题

```
Redis Cluster 核心规则:
  - 16384 个 hash slot（slot = CRC16(key) % 16384）
  - 每个 key 根据 CRC16 哈希只属于一个 slot
  - Pipeline 模式下，一条 Pipeline 中的所有 key 必须属于同一个 slot
  - 如果 Pipeline 包含跨 slot 的 key → Redis 直接拒绝，返回 CROSSSLOT 错误

生产现象:
  - 代码发了一条 Pipeline，里面塞了 100 个 SETEX
  - key 是随机生成的（如 "user:123", "order:456", "log:789"）
  - 100 个 key 落在不同 slot → Redis 返回 "CROSSSLOT Keys in request don't hash to the same slot"
  - 客户端的 Pipeline 批量提交全部失败，一条命令都没执行
  - 如果不处理，几十万/秒的 Pipeline 请求全部变成 CROSSSLOT 异常日志轰炸
```

### 7.2 CROSSSLOT 仅发生在"单通道"直连场景

**核心结论：命令最终被发送到"不拥有所有 key 的单个节点"就会触发 CROSSSLOT。**

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     两种连接方式的根本区别                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  场景 A: 集群客户端（Lettuce ClusterClient / JedisCluster）              │
│                                                                         │
│    Pipeline: SET user:1 v + SET order:2 v + SET log:3 v                 │
│      ↓ 客户端分析每个 key → slot                                        │
│      ↓ 按 slot 分组成子 Pipeline                                        │
│      ↓ slot A → 节点 1;  slot B → 节点 2;  slot C → 节点 3             │
│    → 自动路由到各自节点，全部成功 ✅  CROSSSLOT = 0                      │
│                                                                         │
│  场景 B: 单节点直连（绕过集群客户端）                                     │
│                                                                         │
│    Pipeline: SET user:1 v + SET order:2 v + SET log:3 v                 │
│      ↓ 所有命令发送到同一个 Redis 节点                                   │
│      ↓ 该节点发现 3 个 key 在 3 个不同 slot                              │
│      ↓ 该节点只拥有其中一部分 slot，拒绝执行                              │
│    → 返回 CROSSSLOT ❌（全部失败，0 条执行）                              │
│                                                                         │
│  场景 C: 集群客户端 + 多 key 命令（MSET / DEL / MGET）                    │
│                                                                         │
│    MSET k1 v1 k2 v2   (k1→slot A, k2→slot B)                            │
│      ↓ Lettuce/Jedis 自动拆分: SET k1 v1 → 节点1 + SET k2 v2 → 节点2   │
│    → 跨槽 MSET 被拆分执行，全部成功 ✅  CROSSSLOT = 0                    │
│                                                                         │
│  场景 D: 集群客户端 + Lua / MULTI 事务（硬限制，不可拆分）                 │
│                                                                         │
│    EVAL script 2 k1 k2 ...  (k1→slot A, k2→slot B)                      │
│      ↓ 集群客户端无法把单个 Lua 脚本拆成多节点执行（无法保证原子性）      │
│    → 跨槽 → 触发 CROSSSLOT ❌ 或客户端本地报错                           │
│                                                                         │
│    MULTI → SET k1 v1 → SET k2 v2 → EXEC                                 │
│      ↓ 同上，事务不可拆分                                                │
│    → 跨槽 → 触发 CROSSSLOT ❌                                           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

#### 7.2.1 CROSSSLOT 的其他触发场景

不只是"显式绕开集群客户端"，以下情况同样会触发：

| 场景 | 原因 |
|:--|:--|
| 不支持 Cluster 的代理（如旧版 Twemproxy） | 代理层无 slot 感知，多 key 命令全发往同一后端 |
| 错误配置的负载均衡（LB/DNS 指向集群单节点） | 客户端以为是集群，实际所有请求打到同一个节点 |
| 集群客户端内拿到单节点连接来做 Pipeline | `connection.sync()` 而非 `clusterClient.connect()` 的 cluster-aware API |
| 直接用 `redis-cli -h 单节点 -p 6379` 跑多 key 命令 | 直连必然单通道 |

> **本质：任何时候命令最终发往"不拥有所有相关 key 的单个 Redis 节点"→ CROSSSLOT。**

### 7.3 CROSSSLOT 触发条件

| 条件 | 集群客户端 | 单节点直连 |
|:--|:--|:--|
| 单条命令（SET key value） | ✅ 自动路由 → 成功 | ✅ 单 key 无关 slot |
| Pipeline 内 key 跨不同 slot | ✅ **自动按 slot 分组发往各节点 → 成功** | ❌ **触发 CROSSSLOT** |
| MSET / DEL / MGET 跨 slot | ✅ **自动拆分为单 key 命令 → 成功** | ❌ **触发 CROSSSLOT** |
| **Lua 脚本操作多个 key 跨 slot** | ❌ **无法拆分脚本保持原子性 → CROSSSLOT 或客户端报错** | ❌ 触发 CROSSSLOT |
| **MULTI/EXEC 事务跨 slot** | ❌ **无法拆分事务保持原子性 → CROSSSLOT 或客户端报错** | ❌ 触发 CROSSSLOT |

> **关键区别**：MSET/MGET/DEL 是"无状态组合命令"，客户端可以安全拆分为多条单 key 命令再合并结果。
> 但 Lua 和 MULTI/EXEC 是**有原子性要求的**，客户端无法把单个脚本/事务拆成多个节点并发执行并保证原子性，
> 因此跨槽的 Lua/事务在任何客户端下都会失败。
> **Lua / MULTI 必须用 hash tag 确保所有 key 在同一 slot。**

**总结：对于 MSET/DEL/MGET 和单 key Pipeline，正确使用集群客户端即可 CROSSSLOT = 0。对于 Lua/MULTI，必须用 hash tag 确保同槽。**

### 7.4 哈希标签（Hash Tag）：性能优化而非错误修复

**重要纠正：集群客户端（Lettuce/Jedis）自动处理跨槽路由 → CROSSSLOT = 0，Hash Tag 不是修复 CROSSSLOT 的工具，而是性能优化手段。**

#### 7.4.1 `{}` 槽位锚定机制

```
Hash Tag 规则:
  只对 { } 包裹的部分计算 CRC16，{} 外的前后缀不影响槽位

  key = "{user:1001}:orders"            → CRC16("user:1001")
  key = "prefix:{user:1001}:suffix"     → CRC16("user:1001")  ← 花括号位置无所谓
  key = "{order:5001}:items"            → CRC16("order:5001")
  key = "{order:5001}:logs"             → CRC16("order:5001")  ← 同槽

  → 可以通过 {} 自主指定"按什么分片"，实现：
    {分片键}:唯一标识后缀

  Hash tag 保证 keys 落在相同 slot —— 在给定集群拓扑下，相同 slot 的 keys 一定在同一个节点。
  但在 slot 重分配 / reshard 期间，slot 所属节点可能发生变化。
  （因此 hash tag 的"同槽 = 同节点"在拓扑稳定时成立，集群拓扑变更时需要关注迁移窗口。）
```

#### 7.4.2 Hash Tag 真实价值：性能优化

| | 不加 hash tag（MSET_RANDOM） | 加 hash tag（MSET_HASHTAG） |
|:--|:--|:--|
| **MSET 行为** | 客户端自动拆分为多条 SET | 单条 MSET 直发 |
| **网络往返** | 2 次（拆分到 2 个节点） | 1 次 |
| **cmd/s** | ~202k | **~356-431k** |
| **延迟** | 1.74ms | **0.86ms** |
| **性能提升** | - | **~1.9x** |

```
跨槽 MSET k1 v1 k2 v2 (k1→slot A, k2→slot B):
  客户端拆分: SET k1 v1 → 节点1
              SET k2 v2 → 节点2
  → 2 条命令、2 次网络往返、2 次等待

同槽 MSET {tag}k1 v1 {tag}k2 v2 (都在 slot X):
  客户端直发: MSET k1 v1 k2 v2 → 节点1
  → 1 条命令、1 次网络往返、1 次等待
```

**什么时候值得加 hash tag：**
- **多 key 写命令**（MSET、DEL）→ **值得**，拆分开销大，收益 ~2x
- **Pipeline 单 key 命令**（SETEX、HGET）→ **不需要**，集群客户端已按 slot 自动分组，无明显收益
- **Lua / MULTI 事务** → **必须加 hash tag！** 这是 Redis 硬限制——所有通过 KEYS[] 指定的 key 必须位于同一 slot。集群客户端无法拆分 Lua 脚本或事务（无法保证原子性），跨槽必报 CROSSSLOT。解决方案：用 `{分片键}` 强制 key 同槽，或使用 `KEYS[]` 数组传 key 让客户端校验

### 7.5 压测架构

```
Lettuce 集群客户端 — CROSSSLOT = 0（自动路由）:
  RANDOM:  Pipeline SETEX cs:随机key → 跨槽 → 客户端自动按 slot 分组发送 → 成功 ✅
  HASHTAG: Pipeline SETEX {tagN}:cs:随机key → 同槽 → 高效 Pipeline → 成功 ✅
  MSET:    MSET k1 v1 k2 v2 → 跨槽 → 客户端自动拆分 → 成功 ✅
  HGET:    Pipeline HGET → 跨槽 → 客户端自动路由 → 成功 ✅

RAW 单节点直连 — CROSSSLOT = 100%（完全爆炸）:
  RAW_MSET:  MSET x:1 v y:2 v → 不同前缀 → 跨槽 → CROSSSLOT ❌
  RAW_MGET:  MGET x:1  y:2    → 不同前缀 → 跨槽 → CROSSSLOT ❌
  RAW_DEL:   DEL  x:1  y:2    → 不同前缀 → 跨槽 → CROSSSLOT ❌
  RAW_LUA:   EVAL 操作 2 key  → 不同前缀 → 跨槽 → CROSSSLOT ❌
  RAW_MULTI: MULTI + 2 SETEX  → 不同前缀 → 跨槽 → CROSSSLOT ❌
```

```
Lettuce 集群客户端 — RANDOM 模式:
  ┌──── Pipeline 100 keys ────┐
  │ cs:12345 → slot A ──┐     │
  │ cs:67890 → slot B ──┤     │ ← 客户端按 slot 分组
  │ cs:11111 → slot C ──┘     │ ← 发往不同节点
  └───────────────────────────┘
  → 各节点各自执行 → 全部成功 ✅

RAW 单节点直连 — CROSSSLOT 模式:
  ┌──── Pipeline 100 keys ────┐
  │ x:12345 → slot A          │
  │ y:67890 → slot B   ✗      │ ← 同一节点收到跨槽命令
  │ x:11111 → slot C          │ ← 该节点不拥有所有 slot
  └───────────────────────────┘
  → CROSSSLOT → 全部拒绝 ❌
```

### 7.6 REST 端点

```
GET  /cluster/metrics         -- 当前指标快照
POST /cluster/start           -- 启动压测 (?concurrency=4&batchSize=100&mode=...)
POST /cluster/stop            -- 停止压测
POST /cluster/preload          -- 预载 HSET 数据 (50 万 key, HGET 模式前置)
POST /cluster/sweep            -- 自动遍历 batchSize 梯度
GET  /cluster/sweep/results    -- Sweep 对比报告

mode 参数:
  集群客户端（CROSSSLOT=0）:  RANDOM, HASHTAG, MSET, MSET_TAG, HGET
  单节点直连（CROSSSLOT=100%）: RAW_MSET, RAW_MGET, RAW_DEL, RAW_LUA, RAW_MULTI
```

> **Pipeline 关键提醒**：必须使用集群客户端提供的 cluster-aware pipeline/async API（如 Lettuce 的 `StatefulRedisClusterConnection.async()`）。
> 如果用 `clusterClient.getPartitions().getNode(0).connect()` 拿单节点连接再做 pipeline，
> 等同于"单通道直连"，同样会触发 CROSSSLOT。

### 7.7 测试方法

#### 7.7.1 启动 Redis Cluster

```bash
# 使用 cluster profile 启动 6 节点集群
docker-compose --profile cluster up -d

# 等待约 15 秒（init 容器运行 cluster create）
# 验证集群状态
docker exec redis-cluster-1 redis-cli -p 6379 CLUSTER INFO | grep cluster_state
# → cluster_state:ok

docker exec redis-cluster-1 redis-cli -p 6379 CLUSTER NODES
# → 应显示 3 masters + 3 slaves
```

#### 7.7.2 集群客户端模式 — CROSSSLOT = 0（自动路由）

```bash
# 1) Pipeline SETEX 随机 key（跨槽也无问题）
curl -X POST "http://localhost:8082/cluster/start?concurrency=4&batchSize=100&mode=RANDOM"
curl -s http://localhost:8082/cluster/metrics
# crossSlotErrors: 0   ← 集群客户端自动按 slot 分组发送
# cmd/s: ~170k          ← 正常吞吐

# 2) MSET 随机 key（跨槽也无问题）
curl -X POST "http://localhost:8082/cluster/start?concurrency=4&batchSize=100&mode=MSET"
# crossSlotErrors: 0   ← MSET 被自动拆分为单条 SET
# cmd/s: ~200k

# 3) Pipeline HGET（先预载 50 万 HSET key + 再压测）
curl -X POST "http://localhost:8082/cluster/preload"
curl -X POST "http://localhost:8082/cluster/start?concurrency=4&batchSize=100&mode=HGET"
# crossSlotErrors: 0   ← 读操作同样自动路由
# cmd/s: ~390k
```

#### 7.7.3 RAW 单节点直连 — CROSSSLOT 轰炸

```bash
# 绕过集群客户端，直连单节点 → CROSSSLOT 100%
curl -X POST "http://localhost:8082/cluster/start?concurrency=4&batchSize=100&mode=RAW_MSET"

curl -s http://localhost:8082/cluster/metrics
# crossSlotErrors: ~1,500,000 (每5秒 150 万条 CROSSSLOT!)
# cmd/s: 0.4               ← 几乎全部失败

# 其他 RAW 模式同样 100% CROSSSLOT:
# RAW_MGET, RAW_DEL, RAW_LUA, RAW_MULTI
```

#### 7.7.4 Sweep 遍历：RAW 模式下 CROSSSLOT 与 batchSize 的关系

```bash
# 用 RAW_MSET 模式遍历 batchSize 梯度，看 CROSSSLOT 如何随批次增大而线性增长
curl -X POST "http://localhost:8082/cluster/sweep?concurrency=4"

# 等待约 140 秒（7 步 × 20s/步），查看结果
curl -s http://localhost:8082/cluster/sweep/results

# 预期:
# batchSize=10:   CROSSSLOT ~10K/s
# batchSize=25:   CROSSSLOT ~25K/s
# batchSize=50:   CROSSSLOT ~52K/s
# batchSize=100:  CROSSSLOT ~100K/s
# batchSize=200:  CROSSSLOT ~206K/s
# batchSize=400:  CROSSSLOT ~410K/s
# batchSize=800:  CROSSSLOT ~820K/s
# ↑ batchSize 越大 → CROSSSLOT 线性增长
# 但用集群客户端模式跑 sweep → CROSSSLOT 始终 = 0
```

### 7.8 实际压测数据对比

#### Lettuce 集群客户端 — 全部 0 CROSSSLOT

| # | 模式 | CROSSSLOT | cmd/s | 平均延迟 | 说明 |
|:--|:--|:--|:--|:--|:--|
| ① | LETTUCE_RANDOM (SETEX) | **0** | ~167-171k | 2.23ms | 跨槽 Pipeline 自动分组路由 |
| ② | LETTUCE_HASHTAG (SETEX) | **0** | ~152-174k | 2.24ms | hashtag 同槽 Pipeline |
| ③ | LETTUCE_MSET_RANDOM | **0** | ~202-227k | 1.74ms | 跨槽 MSET 自动拆分 |
| ④ | LETTUCE_MSET_HASHTAG | **0** | **~356-431k** | **0.86ms** | 同槽 MSET 无需拆分，最快 |
| ⑤ | LETTUCE_HGET | **0** | **~391k** | **0.94ms** | Pipeline HGET 跨槽读 |

> **测试条件说明**（供复现参考）：
> - 硬件: Docker 单机，4 core / 8G 内存分配给集群节点
> - Redis 版本: 7.x（docker-compose `redis:7-alpine`）
> - 网络: Docker bridge 内网（`cluster-net`），延迟 < 0.1ms
> - 客户端: Lettuce 6.x，4 并发线程，batchSize=100
> - 并发/批次: 4 并发 × 100 cmd/batch，自动 flush
> - value/payload: 约 100 bytes（简单字符串）
> - 集群拓扑: 3 主 3 从，单机部署
>
> 注：不同环境（网络、硬件、value 大小、并发数）的绝对数值会有差异，关注相对趋势即可。

#### RAW 单节点直连 — 100% CROSSSLOT

| # | 模式 | CROSSSLOT (每5秒) | cmd/s | 结论 |
|:--|:--|:--|:--|:--|
| ① | RAW_MSET | ~146-159 万 | 0.4 | MSET 跨槽 → CROSSSLOT |
| ② | RAW_MGET | ~150 万 | 0.4 | MGET 跨槽 → CROSSSLOT |
| ③ | RAW_DEL | ~158 万 | 0.4 | DEL 跨槽 → CROSSSLOT |
| ④ | RAW_LUA | ~118-154 万 | 0.4 | Lua 跨槽 → CROSSSLOT |
| ⑤ | RAW_MULTI | ~3.5-5.6 万 | 0.4 | MULTI 跨槽 → CROSSSLOT |

### 7.9 对比总结

| | 集群客户端 | 单节点直连 |
|:--|:--|:--|
| **跨槽 Pipeline** | ✅ 自动按 slot 分组发送 | ❌ CROSSSLOT 轰炸 |
| **跨槽 MSET/MGET/DEL** | ✅ 自动拆分为单 key 命令 | ❌ CROSSSLOT 轰炸 |
| **跨槽 Lua / MULTI** | ❌ **无法拆分保持原子性 → 报错** | ❌ CROSSSLOT 轰炸 |
| **CROSSSLOT 错误量** | 0（除 Lua/MULTI） | 100% |
| **吞吐** | 正常（17-43 万 cmd/s） | 全部失败 |
| **需要 hash tag？** | 仅 Lua/MULTI 必须；其他可选 | 是（唯一能避免的方式） |

> **注意**：HASHTAG 方案在集群客户端下仍有益处——避免 MSET 拆分开销，MSET_HASHTAG 性能（356-431k cmd/s）是 MSET_RANDOM（202-227k cmd/s）的 ~1.7-1.9 倍。

### 7.10 核心结论

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    场景 7 核心发现                                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ★ CROSSSLOT 只存在于"命令最终落到不拥有全部 key 的单个节点"时           │
│                                                                         │
│  1. 集群客户端（Lettuce/Jedis ClusterClient）自动处理跨槽路由            │
│     - Pipeline 单 key 命令: 按 slot 分组，自动发往不同节点              │
│     - MSET/DEL/MGET: 自动拆分为单 key 命令（无状态命令可安全拆分）       │
│     - 结论: 单 key 命令和可拆分多 key 命令 CROSSSLOT = 0                │
│                                                                         │
│  2. Lua / MULTI 是例外——必须同槽！                                       │
│     - 客户端无法把单个脚本/事务拆到多个节点并保持原子性                   │
│     - 跨槽的 Lua/MULTI 在所有客户端下都会失败（CROSSSLOT 或客户端报错）  │
│     - 解决方案: 必须使用 hash tag {分片键} 确保所有 key 同槽             │
│                                                                         │
│  3. CROSSSLOT 触发场景（包括非显式绕过客户端的情况）:                     │
│     - 直接 redis-cli -h 单节点 -p 6379 跑多 key 命令                    │
│     - 用普通 RedisClient（非 RedisClusterClient）连集群单节点            │
│     - 不支持 Cluster 的代理（如旧版 Twemproxy）、错误的 LB/DNS 配置      │
│     - 集群客户端内拿到单节点连接（非 cluster-aware 连接）做 pipeline      │
│     → 本质: 任何时刻命令发往"不拥有所有 key 的单个节点" → CROSSSLOT     │
│                                                                         │
│  4. Hash Tag 的价值:                                                    │
│     - 对 Lua/MULTI: 必须（否则无法执行）                                 │
│     - 对 MSET/DEL: 性能优化（避免拆分开销，吞吐 ~2x）                    │
│     - 对 Pipeline SETEX/HGET: 不需要（客户端已自动分组）                 │
│                                                                         │
│  5. 客户端重定向（MOVED / ASK）内置处理:                                 │
│     - MOVED: 节点告知"key 归另一个节点管"，客户端更新 slot→node 映射并重试│
│     - ASK: 节点告知"key 正在迁移中"，客户端临时重定向（不更新映射表）     │
│     - 优秀客户端库（Lettuce/Jedis）自动处理两者，业务代码无感知           │
│                                                                         │
│  6. 单机 → 集群迁移 checklist:                                           │
│     ☐ 确保使用集群客户端（RedisClusterClient / JedisCluster）            │
│     ☐ **Lua/MULTI 场景必须用 hash tag 保证同槽**                         │
│     ☐ 不要用普通客户端直连集群单节点，避免代理/LB 错误配置                │
│     ☐ 性能敏感: 多 key 写用 hash tag 避免拆分开销 ~2x                   │
│     ☐ 必须使用 cluster-aware pipeline API                                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 7.11 CRC16 由客户端自动计算 — 应用层无需关心

**集群客户端自动完成 CRC16 计算和节点路由，业务代码不需要写任何 CRC16 逻辑。**

```
你的 Java 代码:
  clusterClient.sync().set("user:1001", "value");

Lettuce 内部自动做的事:
  ① CRC16("user:1001") % 16384 → slot 12182
  ② 查 slot→node 映射表 → master-2 (172.20.0.3:6379)
  ③ 发 SET 命令到 master-2
  ④ 拿到响应返回给你

零行 CRC16 代码，零次手动路由。
```

压测已证明：LETTUCE_RANDOM、LETTUCE_MSET_RANDOM、LETTUCE_HGET 全部 **CROSSSLOT = 0**，CRC16 计算和路由全是客户端库内部的事。

**唯一需要手动算 CRC16 的场景**：设计 hashtag 策略时，验证不同 tag 值是否均匀分布。

```java
// 仅用于验证分片均匀性，生产代码不需要
for (int i = 1; i <= 100; i++) {
    int slot = CRC16.crc16(("user:" + i).getBytes()) % 16384;
    System.out.println("user:" + i + " → slot " + slot);
}
```

#### 7.11.1 客户端重定向：MOVED 与 ASK

集群客户端内置处理以下两种重定向，业务代码无感知：

| 重定向类型 | 触发条件 | 客户端行为 |
|:--|:--|:--|
| **MOVED** | key 归属的 slot 不在当前节点 | 更新 slot→node 映射表，自动重试到正确节点 |
| **ASK** | key 正在 slot migration 中，暂时在旧节点 | 临时重定向到目标节点（不更新映射表），仅该次请求 |

```
MOVED 流程:
  客户端发送 SET k1 v1 → 节点 A
  节点 A: "k1 归 slot 6374，MOVED to 节点 B"
  客户端: 更新 slot 6374→节点 B，自动重发 SET k1 v1 → 节点 B ✅

ASK 流程（slot 迁移窗口）:
  客户端发送 GET k1 → 节点 A
  节点 A: "k1 正在迁移，ASK to 节点 B"
  客户端: 仅本次请求发往节点 B，下一条请求仍然查原映射表
```

> Lettuce 和 Jedis 的集群客户端都内置了 MOVED/ASK 处理。
> 使用 `RedisClusterClient` 时无需额外配置，连接池和拓扑刷新也由库管理。

### 7.12 Hashtag 打散策略与最佳实践

**核心原则：打散但要可控，不能随机打。**

```
❌ 错误: 所有人用同一个 hashtag

  key = "{user}:order:1001"    → 都在 slot 1234 → 都在 master-1
  key = "{user}:order:1002"    → 都在 slot 1234
  key = "{user}:cart:5001"     → 都在 slot 1234

  → master-1 CPU 100%，master-2/3 闲着 → 热点！

❌ 错误: hashtag 完全是随机值

  key = "{rnd583}:order:1001"  → slot A
  key = "{rnd291}:cart:1001"   → slot B
  → 两个 key 永远无法合并 MSET → 失去 hashtag 的价值

✅ 正确: 按业务实体稳定分片

  key = "{1001}:order"         → slot X  ← 用户 1001 所有数据同一个 slot
  key = "{1001}:cart"          → slot X  ← 可 MSET/DEL/Lua 高效操作
  key = "{1002}:order"         → slot Y  ← 用户 1002 不同 slot
  key = "{1002}:cart"          → slot Y

  → 同用户数据高效聚合 + 不同用户负载自然分散
```

**一条规则：hashtag 的业务实体数量 >> Redis 主节点数量 >> 1**

| 场景 | hashtag 策略 | 理由 |
|:--|:--|:--|
| **用户维度数据** | `{userId}` | 一个用户的所有 key 在同一 slot，可用 MSET 批量操作 |
| **订单维度** | `{orderId}` | 订单相关的 order + items + log 同槽 |
| **消息/时间线** | `{bucket}` 如 `{hour-12}` | 按时间窗口分桶 |
| **全局缓存** | **不加 hashtag** | 单 key 操作无需同槽，集群自然分布即可 |

**当前代码示例：`{tag1}` ~ `{tag8}`，8 个固定 tag 均匀落到 3 个 master。**

#### 7.12.1 提前设计 Hashtag：单机 → 集群平滑预备

**强烈建议：即使在单体 Redis 阶段，就按集群规范设计 key 格式（预埋 `{}`）。**

```
单体阶段就写:

  user:{1001}:profile     ← 即使现在只有单机，{1001} 也无副作用
  user:{1001}:orders      ← 单机下 CRC16 不参与路由，但格式已就绪
  user:{1001}:cart

迁移集群时:

  数据原样导入 → {1001} 自动将同一用户的 key 锚定到同一 slot
  → MSET / Lua / MULTI 全都可以直接使用，零 key 改动 ✅
```

```
反例：单体阶段没加 {}：

  user:1001:profile       ← 单体下无所谓
  user:1001:orders
  user:1001:cart

迁移集群时:

  三个 key 分到三个不同 slot → MSET 被拆分，性能打对折
  如果要加 {} → 需要全量改 key 格式 + 重新写入数据 → 巨大成本
```

| 方案 | 单体阶段 | 迁移集群 | 总成本 |
|:--|:--|:--|:--|
| **提前加 `{}`** | 零额外成本，`{}` 在单机下无任何影响 | 零改动，直接迁移 | **最低** |
| **不加 `{}`** | 零成本 | 单 key 命令 OK；多 key 性能打折扣；想优化必须改 key | **高** |

> **提前加 `{}` 就像建楼时预留电梯井——用不上时没感觉，将来需要时就是零成本升级。**

### 7.13 单机升级集群迁移指南

**推荐停服迁移以获得最简单可靠的迁移路径。**

```
为什么推荐停服（风险最低）:
  集群建立后 slot 立刻有了归属。
  在线迁移需要协调 slot 逐批搬迁、客户端 MOVED/ASK 重定向、迁移期间的幂等性。
  对大多数场景，几分钟的停服窗口远小于在线迁移出问题的排查时间。

  单机 → 集群 = 数据物理重分布（2/3 的数据要搬到新节点）
```

> **如果需要零停机**，可使用 `redis-cli --cluster reshard` 在线逐批搬 slot。前提：
> - 客户端正确实现了 MOVED 重定向（更新 slot→node 映射后自动重试）
> - 客户端正确处理 ASK 临时重定向（迁移中的 key 暂时在源节点）
> - 业务层有幂等性保证或可容忍搬迁期间的短暂不可用
> - **必须在测试环境充分验证**

#### 7.13.1 原有数据是否需要加 hashtag？

**不需要。** 原有 key 原样迁入集群，单条 GET/SET 命令完全正常。

```
迁移前 key:
  user:1001:profile    → 独立 key，CRC16 自动算 → 落在某个 slot ✅
  order:5001:items     → 独立 key，CRC16 自动算 → 落在某个 slot ✅

迁移后:
  GET user:1001:profile   → 客户端自动算 slot → 路由到正确节点 ✅

  MSET user:1001:profile v1 user:1001:scores v2 ...
  → Lettuce 自动拆分为多条 SET → 工作正常，只是比单条 MSET 慢 ~2x
```

**只有想用多 key 命令做性能优化时，才需要加 hashtag 并改写 key 格式。这是可选的后续优化。**

#### 7.13.2 迁移流程

```bash
# === 1. 停服 ===
# 关闭业务流量入口

# === 2. 从单机 dump 数据 ===
redis-cli -h 原节点 BGSAVE           # 生成 dump.rdb

# === 3. 启动 3 主 3 从集群（6 个空节点）===
docker-compose --profile cluster up -d

# === 4. 建立集群关系 ===
redis-cli --cluster create \
  172.20.0.2:6379 172.20.0.3:6379 172.20.0.4:6379 \
  172.20.0.5:6379 172.20.0.6:6379 172.20.0.7:6379 \
  --cluster-replicas 1

# === 5. 把旧数据导入集群 ===
# 方式 A: 用 redis-cli --cluster import 从旧节点 pipe 导入
# 方式 B: 覆盖一个 master 的 RDB，再 reshard 到其他节点

# === 6. 验证 slot 分布和 key 数量 ===
redis-cli --cluster check 172.20.0.2:6379

# === 7. 恢复业务 ===
```

> **几十万 key 迁移通常只需几分钟。停服是最安全、最简单的方案。**

#### 7.13.3 迁移后 checklist

```
☐ 确认所有客户端使用 RedisClusterClient / JedisCluster（不是单节点客户端）
☐ 新写入的 key 不需要特殊处理（单条命令自动路由）
☐ 性能敏感链路: 评估是否需要 hash tag 优化多 key 命令
☐ Lua 脚本传 key 用 KEYS[] 而非硬编码
☐ 压测验证吞吐和延迟符合预期
```

### 7.14 停止

```bash
# 停止 cluster profile
docker-compose --profile cluster down

# 清理数据卷（可选）
docker volume rm redis-project_cluster-data-1 redis-project_cluster-data-2 \
  redis-project_cluster-data-3 redis-project_cluster-data-4 \
  redis-project_cluster-data-5 redis-project_cluster-data-6
```
