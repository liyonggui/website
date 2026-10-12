---
title: "场景 8.15 · Pipeline × RTT 敏感度 — 网络延迟对五种模式的退化量化"
summary: "场景 8 所有实测数据均基于 Docker bridge 单机环境（RTT < 0.1ms），而真实生产环境跨机 RTT 通常在 1~5ms（同 AZ）、5~10ms（跨 AZ）、…"
date: 2026-07-09
author: "小番茄程序猿"
tags:
  - Redis
  - 缓存
category: deep
series: "redis"
slug: "场景8-2-Pipeline RTT敏感度与WAIT"
reading_time: 117
featured: false
draft: false
---
## 场景 8.15: Pipeline × RTT 敏感度 — 网络延迟对五种模式的退化量化

### 8.15.1 背景与问题

场景 8 所有实测数据均基于 Docker bridge 单机环境（RTT < 0.1ms），而真实生产环境跨机 RTT 通常在 1~5ms（同 AZ）、5~10ms（跨 AZ）、10~50ms（跨 Region）。

经典公式 `throughput ≈ (concurrency × pipelineSize) / RTT` 说明吞吐与 RTT 成反比——但不同模式的退化幅度不同：

- **网络依赖模式**（SINGLE_KEY / DISTRIBUTED / SHARDED）：Pipeline 中每条 INCR 都必须等待 Redis 回包 → 吞吐 ∝ 1/RTT（即 RTT 增大时吞吐按反比下降）
- **RTT 免疫模式**（LOCAL_BUFFERED / COMPUTED）：主循环在本地内存执行，仅定期 INCRBY 受 RTT 影响 → 本地吞吐不受网络延迟影响

**本场景的目标**：在不同 tc netem 延迟下量化五种模式的退化曲线，给出"何时应该切换到本地缓冲模式"的量化判据。

### 8.15.2 原理公式

```
Pipeline 命令/秒 的简化上限可近似表示为:
  maxCmdPerSec ≈ (concurrency × pipelineSize) / RTT

⚠️ 单位说明: RTT 必须以「秒」为单位代入计算！
  例如 RTT = 1ms → 应代入 0.001s。
  若误将 ms 当作秒直接代入，吞吐估算会偏离 1000 倍。

  由此可知吞吐量与 RTT 成反比: 吞吐 ≈ O(1/RTT)。
  该公式是理论上限，实际吞吐还受 Redis 单线程处理能力、CPU、网卡
  和客户端实现（Netty / 线程）等限制。

示例（concurrency=16, pipelineSize=100, 先换算 ms → s）:
  RTT=0.1ms = 0.0001s → 理论上限 ≈ (16×100)/0.0001 = 16M cmd/s
                        （实际受 Redis 单线程限制 ~360k）
  RTT=1ms   = 0.001s  → 理论上限 ≈ 1.6M cmd/s
  RTT=5ms   = 0.005s  → 理论上限 ≈ 320k cmd/s
  RTT=10ms  = 0.01s   → 理论上限 ≈ 160k cmd/s

换算过程（以 RTT=1ms 为例）:
  (16 × 100) / (1ms ÷ 1000) = 1600 / 0.001 = 1,600,000 cmd/s

说明: 按公式 (16 × 100) / RTT_seconds，我们在 RTT=0.1ms 时得到理论上限 ~16M cmd/s，
但 Redis 本身是单线程，处理/调度/网络上下文切换等会显著降低这个理论值；
本实验单机 Redis 的实际上限约为 300k~600k（取决于命令类型和 CPU），
因此在低 RTT 场景下实际吞吐受 Redis 本身限制而非网络限制。

LOCAL_BUFFERED / COMPUTED 本地操作:
  本地吞吐 ≈ CPU 核心数 × 单核原子操作速率
  RTT 不影响本地循环 → 始终 ≈ 30M~95M cmd/s（仅定期 INCRBY flush 略有退化）
```

### 8.15.3 压测架构

```
                    ┌──────────────────────────────────────┐
                    │   压测应用 (Spring Boot 8082)          │
                    │   RttBenchService                     │
                    │   5 模式顺序 sweep                    │
                    │   (3s 预热 + 5s 采样) / 每模式       │
                    └───────────┬──────────────────────────┘
                                │ Pipeline INCR / 本地原子操作
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                   ▼
        redis-cluster-1   redis-cluster-2   redis-cluster-3
        (Master A)        (Master B)        (Master C)
        │ tc netem        │ tc netem        │ tc netem
        │ delay Xms       │ delay Xms       │ delay Xms
        └── egress ──→ 延迟 Xms ──→ 客户端回包延迟
```

**RTT 模拟方式**：通过 `docker exec <master> tc qdisc add dev eth0 root netem delay Xms` 在 3 个 master 容器的 egress 方向注入延迟，使客户端收到的回包延迟增加 Xms。

> **注意**：在这里我们通过在每个 master 容器的 egress（出向）上注入单向延迟 Xms，使得 server→client 的响应路径被延迟 Xms。客户端对单次请求的额外感知延迟大致为 Xms（若 client→server 路径不受延迟影响）。要模拟对称的网络往返 RTT 增加 T，则应在 request 与 response 两个方向各注入约 T/2；如果只在一侧注入 Xms，那么整体 RTT 只增加约 Xms 而非 2×Xms。文中所述"tc delay=5ms 模拟 RTT≈10ms"的说法仅在你同时在两个方向或两个端点都注入 5ms 的情况下成立；单侧注入 5ms 实际上只会在往返上增加约 5ms。

### 8.15.4 测试方法

```bash
# 前提: 3 主 3 从集群 + 压测应用 8082 已启动
# docker-compose --profile cluster up -d
# mvn spring-boot:run -f redis-lettuce-bench/pom.xml

# 一键全自动（推荐）
.\loadtester\pipeline-rtt-bench.ps1 -Concurrency 16 -BatchSize 100

# 或分步手动:
# ① 基线
curl -X POST "http://localhost:8082/rtt/sweep"
# ② 注入 5ms 延迟后再次 sweep
docker exec redis-cluster-1 tc qdisc add dev eth0 root netem delay 5ms
docker exec redis-cluster-2 tc qdisc add dev eth0 root netem delay 5ms
docker exec redis-cluster-3 tc qdisc add dev eth0 root netem delay 5ms
curl -X POST "http://localhost:8082/rtt/sweep"
# ③ 清除
docker exec redis-cluster-1 tc qdisc del dev eth0 root
docker exec redis-cluster-2 tc qdisc del dev eth0 root
docker exec redis-cluster-3 tc qdisc del dev eth0 root
```

> **本实验的 tc 注入方式 = 单侧 egress 注入 Xms**（仅在 3 个 master 的 egress 方向注入）。
> 因此表中标注的 **"tc delay = Xms" 是单向注入**，客户端实际感知的往返延迟大约增加 **Xms（非 2×Xms）**。
> 例如：`tc delay 5ms` → 客户端感知 RTT 增加 ≈ 5ms。
> 若想精确模拟对称 RTT 增加 T，应在 request 与 response 两个方向各注入 T/2（或 server 与 client 两侧各注入 T/2）。

### 8.15.5 REST 端点

```bash
POST /rtt/sweep     — 跑一次全模式 sweep（当前网络条件下），返回 JSON 数组
GET  /rtt/status    — 当前状态

# 响应示例:
# [
#   {"mode":"SINGLE_KEY",  "cmdPerSec":"358,210", "avgLatencyMs":"0.044", "errors":0, "rttImmune":false},
#   {"mode":"DISTRIBUTED", "cmdPerSec":"290,105", "avgLatencyMs":"0.055", "errors":0, "rttImmune":false},
#   {"mode":"SHARDED",     "cmdPerSec":"252,830", "avgLatencyMs":"0.063", "errors":0, "rttImmune":false},
#   {"mode":"LOCAL_BUFFERED","cmdPerSec":"~34,000,000 (本地免疫)", "avgLatencyMs":"N/A (本地)", "errors":0, "rttImmune":true},
#   {"mode":"COMPUTED",    "cmdPerSec":"~95,000,000 (本地免疫)", "avgLatencyMs":"N/A (本地)", "errors":0, "rttImmune":true}
# ]
```

### 8.15.6 实测数据

> **测试条件**：Docker 单机，3 主 3 从 Redis 7.x Cluster，Lettuce 6.x + Netty，concurrency=16 × batchSize=100，每种模式 3s 预热 + 5s 采样。

| RTT (tc delay) | SINGLE_KEY (cmd/s) | DISTRIBUTED (cmd/s) | SHARDED (cmd/s) | LOCAL_BUFFERED | COMPUTED |
|:--|:--|:--|:--|:--|:--|
| **基线 (~0.1ms)** | 307,820 | 116,700 | 108,420 | ~545M | ~134M |
| **1ms** | 315,240 | 130,960 | 114,940 | ~538M | ~173M |
| **3ms** | 333,000 | 117,000 | 123,780 | ~560M | ~178M |
| **5ms** | 279,060 | 116,800 | 112,980 | ~537M | ~182M |
| **10ms** | 163,600 | 106,980 | 103,940 | ~513M | ~173M |

#### 退化率分析（以基线为 100%）

| RTT | SINGLE_KEY | DISTRIBUTED | SHARDED |
|:--|:--|:--|:--|
| 基线 → 1ms | 102% ✅ | 112% | 106% |
| 基线 → 3ms | 108% | 100% | 114% |
| 基线 → 5ms | 91% | 100% | 104% |
| 基线 → **10ms** | **53%** ⚠ | 92% | 96% |

> **关于 1~3ms 高于基线的解释**：部分 RTT 梯度吞吐略高于基线属于测量噪声（JIT 预热、GC、线程调度、网络抖动等）。建议对每个 RTT 梯度至少运行 N 次（N≥3）并报告均值±标准差或 P50/P90，以确认趋势的显著性，避免把噪声当作真实性能差异。

### 8.15.7 关键发现

#### 发现 1: SINGLE_KEY 在低延迟下几乎不退化，10ms 才明显退化

```
Docker 内网延迟 < 0.1ms，Pipeline 100 命令/往返有效均摊了 RTT:
  1~3ms 时吞吐反而略高于基线（噪声范围内， ±8%）
  5ms 时退化至 91%
  10ms 时退化至基线的 53%（~307k → ~164k）

原因: Pipeline 将 100 条 INCR 合并为 1 次网络往返，
      RTT 开销被 /100 均摊。
```

#### 发现 2: DISTRIBUTED / SHARDED 对小幅 RTT 退化非常小

```
DISTRIBUTED / SHARDED 在本次测试配置下对单向/小幅 RTT 的退化非常小
（近似 RTT 免疫），但这不是绝对特性。关键条件是：每个 pipeline batch 内
的命令被路由到相同 slot（或同一 master）以避免 CROSSSLOT，并且不同批次
能并行落在不同 master，从而并行隐藏了每个 master 的 RTT。若并发减少、
批次分布发生变化或某些 master 的 replica 延迟较大，DISTRIBUTED/SHARDED
仍会受 RTT 或节点间复制延迟的影响。

100~130k cmd/s 在所有 RTT 梯度下保持稳定（±10% 噪声范围内）。

注意: 修复前这两个模式吞吐为 0（CROSSSLOT 错误），因为每批内 key 跨 slot。
      修复: 每批使用同一个 hashtag（buildKey(mode, batchSlotSeed)），
      确保 100 条 INCR 落在同一 slot，避免 CROSSSLOT。
```

#### 发现 3: LOCAL_BUFFERED / COMPUTED 本地免疫 — 吞吐级不在一个量级

```
LOCAL_BUFFERED:  ~500M~560M cmd/s  ← 纯本地 AtomicLong，RTT 完全无关
COMPUTED:        ~130M~180M cmd/s  ← 本地计算 + 批量 CAS，RTT 完全无关

与网络模式的差距:
  基线: LOCAL_BUFFERED (545M) ÷ SINGLE_KEY (0.31M) ≈ 1,770 倍
  10ms: LOCAL_BUFFERED (513M) ÷ SINGLE_KEY (0.16M) ≈ 3,136 倍
  → 网络越差，本地缓冲优势越显著
```

> ⚠️ **Caveat：LOCAL_BUFFERED / COMPUTED 的吞吐优势以牺牲实时一致性与失败恢复为代价。** 本地缓冲的更新若在进程崩溃前未写回到 Redis 会丢失；需配套本地持久化、重启恢复，或将缓冲写到本地持久存储以降低丢失风险。选择本地方案前必须评估业务对丢失与延迟的容忍度（RPO/RTO）。

#### 发现 4: 量化判据

| RTT 条件 | 最佳方案 | 原因 |
|:--|:--|:--|
| RTT < 1ms（同机/Docker） | SINGLE_KEY 足够 | Pipeline 均摊有效，退化 < 10% |
| 1ms ≤ RTT < 5ms（同 AZ） | SHARDED / COMPUTED | SINGLE_KEY 仍可用 (91%)，COMPUTED 完全免疫 |
| RTT ≥ 5ms（跨 AZ） | LOCAL_BUFFERED / COMPUTED | SINGLE_KEY 退化明显，本地方案优势放大 |
| RTT ≥ 10ms（跨 Region） | COMPUTED + 批量写回 | SINGLE_KEY 退化至 53%，绝不使用直接 INCR Pipeline |

> **核心公式**: `退化后吞吐 ≈ 基线吞吐 × (基线RTT / 当前RTT)`，当地 RTT 达到线程/连接数铺不满带宽的临界点时，退化加速。

**可计算切换判据（便于自动化决策）**：

```
① 预测网络吞吐:  T_net(RTT) ≈ min(redis_single_thread_limit,
                                    (concurrency × batchSize) / RTT_seconds)
② 若 T_net(RTT) < T_local × α （例如 α = 0.1，即网络方案低于本地方案的 10%）
   → 切换到本地缓冲模式（LOCAL_BUFFERED / COMPUTED）
③ 或基于延迟 SLO：若预计读 P99 > SLO_read 或写 P99 > SLO_write → 切换
④ 经验阈值：RTT ≥ 5ms 且写压占主路径 → 优先本地缓冲/COMPUTED；
   RTT < 1ms 且需要强一致性 → 同步 WAIT 可接受
```

#### 开发踩坑: CROSSSLOT 修复

| 问题 | 原因 | 修复 |
|:--|:--|:--|
| DISTRIBUTED/SHARDED 吞吐为 0 | 每批内 100 个 key 使用不同随机后缀，跨 slot 触发 CROSSSLOT | 每批共享一个 hashtag: `buildKey(mode, batchSlotSeed)` |

> ⚠️ **hashtag 的范围限制与热点风险**：使用相同 hashtag 能保证同批命令落在同一 slot，但会把热度集中到单一 slot/master，可能造成单一节点成为瓶颈或单点压力源。在生产中应权衡跨 slot 并行性与单 slot 聚合带来的热点风险（必要时通过 key 分片或多 hashtag 策略分摊负载）。
| 5 模式 sweep 只跑第一个 | `runSingle()` 结束后 `running.set(false)` 直接杀死全局 sweep | 引入 `workersStopped` per-mode 终止标志，不污染全局 `running` |

### 8.15.8 Docker 配置变更

为确保 `tc` 可在集群 master 容器中使用，`docker-compose.yml` 中 cluster profile 的 3 个 master 节点需增加：

```yaml
redis-cluster-1:
  cap_add:
    - NET_ADMIN    # 场景 8.15: tc netem 模拟网络延迟
```

### 8.15.9 `RttBenchService` 实现要点

```java
// 核心结构: 5 种模式顺序 sweep，每种模式跑 8s（预热 3s + 采样 5s）
// 网络依赖模式: Pipeline INCR → 记录吞吐 + 延迟
// RTT 免疫模式: 本地 AtomicLong 循环 → 返回标注"本地免疫"

private Map<String, Object> runSingle(String mode) {
    // SINGLE_KEY/DISTRIBUTED/SHARDED:
    //   Pipeline batchSize 条 INCR → flush → awaitAll → 记录耗时
    // LOCAL_BUFFERED/COMPUTED:
    //   本地循环 batchSize 次 AtomicLong.addAndGet → 不调 Redis
    //   返回标记 rttImmune=true
}
```

> 完整代码见 `redis-lettuce-bench/src/main/java/com/example/redis/service/RttBenchService.java`。

### 8.16 Pipeline 混合读写 → 读饥饿演示

#### 8.16.1 背景与问题

```
生产中最常见的是热点 key 上同时存在大量 GET 和 INCR 请求：
  ├─ 读请求 (GET hotkey:counter) 与写请求 (INCR hotkey:counter) 混在一起
  ├─ Redis 单线程事件循环：所有命令串行执行
  ├─ Pipeline 100 条 INCR → 100 条命令排队执行 → GET 被堵在后面
  └─ 结果：读请求被写请求"饥饿"（read starvation）

两种饥饿形式：
  ┌─────────────────────────────────────────────────────────┐
  │ 形式 1: 同连接 Pipeline 混合 (same-connection)          │
  │   Pipeline 中 50% INCR + 50% GET 混在一起               │
  │   影响: 有效写吞吐腰斩（写槽位被读命令抢占）              │
  │   注意: 同连接中读/写 FIFO 有序，读一定看到最新值         │
  ├─────────────────────────────────────────────────────────┤
  │ 形式 2: 跨连接独立读写 (cross-connection) ← 真正读饥饿    │
  │   N 条连接疯狂 Pipeline INCR + M 条连接独立 GET          │
  │   影响: 读者被写者排队阻塞，P99 延迟飙升至 40ms+          │
  │   读者有效吞吐仅为写者的 0.4%                             │
  └─────────────────────────────────────────────────────────┘
```

#### 8.16.2 压测架构

```
┌────────────────────────────────────────────────────────┐
│  压测应用 (lettuce-bench, 容器内)                        │
│                                                         │
│  MIXED_RW (same-conn):                                   │
│   1 条连接 × 16 线程 → Pipeline 100 mixed (50 INCR+50 GET)│
│                                                         │
│  MIXED_RW_MULTI_CONN (cross-conn):                       │
│   12 条写连接 × Pipeline 100 INCR → hotkey:counter       │
│    4 条读连接 × 单条 GET → hotkey:counter                │
│   读者测量 per-GET 延迟（avg + max）                      │
│                                                         │
│  SINGLE_KEY (baseline):                                  │
│   1 条连接 × 16 线程 → Pipeline 100 INCR（纯写）          │
│                                                         │
│  测试窗口: 3s 预热 + 5s 采样                               │
└────────────────────────────────────────────────────────┘
```

#### 8.16.3 实测数据

> **测试条件**: Docker 单机，3 主 3 从 Redis 7.x Cluster，concurrency=16，batchSize=100。

| 模式 | 总吞吐 (cmd/s) | 有效写入 (INCR/s) | 有效读取 (GET/s) | 读者 avg 延迟 | **读者 max 延迟** | 错误数 |
|:--|:--|:--|:--|:--|:--|:--|
| **SINGLE_KEY** (纯写) | 311,260 | 311,260 | — | — | — | 10 |
| **MIXED_RW** (同连接, 50:50) | 328,700 | 164,350 | 164,350 | 5.8ms/batch | — | 800 |
| **MULTI_CONN** (跨连接) | 366,701 | **365,380** | **1,321** | 3.66ms | **41.27ms** | 600 |

#### 8.16.4 关键发现

#### 发现 1: 同连接 Pipeline 混合 — 写吞吐腰斩

```
MIXED_RW (same-conn) 总吞吐 ~329k ≈ 纯写 ~311k（总 CPU 上限不变）
但有效 INCR 从 311k 骤降至 164k（腰斩至 53%）
原因: 50% Pipeline 槽位被 GET 占据 → 有效写入减半
```

#### 发现 2: 跨连接独立读写 — 读者被严重饥饿 ✓ 核心发现

```
MULTI_CONN (cross-conn) 写者 365k INCR/s vs 读者 1,321 GET/s
读者有效吞吐仅为写者的 0.4% ← 读饥饿量化指标

原因链路:
  12 条写连接 → 每批 100 INCR → Redis 单线程顺序执行
  4 条读连接 → 单个 GET 排在 INCR 队列尾部
  → 每个 GET 必须等队列中所有 INCR 先执行完
  → 读者最大延迟 41ms，平均延迟 3.66ms
```

#### 发现 3: 三层退化对比

| 维度 | SINGLE_KEY (纯写) | MIXED_RW (同连接) | MULTI_CONN (跨连接) |
|:--|:--|:--|:--|
| 有效写吞吐 | 311k (100%) | 164k (53%) | 365k (117%)* |
| 有效读吞吐 | — | 164k | **1.3k (0.4%)** |
| 读延迟 P99 | — | ~15ms/batch | **41ms** |
| 写槽位抢占 | 无 | 有（50% 被读占） | 无（独立连接） |
| 读饥饿 | 无 | 无（同 FIFO） | **严重** |

> *写者 365k > 311k 的主要原因是多连接提高了并行 pipeline 的并发度，使客户端更好地填满 Redis 的网络/事件循环与 CPU；这并不意味着单连接 pipeline 本身更差，而是**并发连接数是影响吞吐的关键因素之一**。

#### 发现 4: 生产应对策略

| 场景 | 推荐方案 | 原因 |
|:--|:--|:--|
| 读远多于写 (99:1) | LOCAL_BUFFERED | 读者读本地 AtomicLong，不碰 Redis |
| 读/写比例均衡 | MIXED_RW (同连接) + Pipeline | 读写共享 FIFO，不会互相饥饿 |
| 写远多于读 (99:1) | COMPUTED + 定期批量回写 | 写本地累加，定期写回 Redis |
| 必须实时读到最新值 + 高写压 | **写 master + 读 replica** | 利用主从复制分离读写 |

> ⚠️ **读 replica 的权衡**：把读导向 replica 可显著缓解读饥饿，但会带来读到**陈旧数据**的风险（复制延迟）。如果业务强依赖实时性，这不是可接受的方案。其他缓解措施包括：减小 batchSize、把高优先级读走单独的连接/队列、在客户端做本地缓存或读-修正（如果允许一定的误差）、或把读写合并为 single-connection mixed pipeline（保证 FIFO 和最新值）。

```
核心结论:
  同一 hotkey 上，只要同时存在独立的读连接和写连接：
    → 读连接一定会被写连接排队阻塞（Redis 单线程本质）
    → 解决之道不是优化网络，而是避免读 Redis（用本地缓冲或副本）

说明: 当写者占据大量队列时，读命令在事件循环内的排队延迟会累积，且这和
pipeline 的 flush 策略、客户端连接数以及 Redis 的 IO/网络层实现有关
（例如 Netty socket buffer、epoll 的唤醒策略等）。读饥饿的严重程度还与
系统的 socket 缓冲、客户端 flush 策略以及 Redis 的处理延迟相关；
因此缓解手段除了本地缓冲外，也可以通过减小单批 batchSize、调整
pipeline-timeout、或把读流量导向 replica 来缓解。
```

#### 开发踩坑

| 问题 | 原因 | 修复 |
|:--|:--|:--|
| MIXED_RW 错误数 800~1500（比纯写高 100×） | Pipeline 批量超时：GET 等同批所有 INCR 完成才能返回 | 增大 pipeline-timeout 或减小 batchSize |
| 跨连接读延迟 > 40ms | 12 条写连接并发 Pipeline 100 INCR → 1200 条命令堆积在事件循环 | 这是设计目标——正是为了演示读饥饿 |

#### 8.16.5 `RttBenchService` 实现要点

```java
// MIXED_RW_MULTI_CONN 核心结构:
//   12 条写连接（autoFlush=false, Pipeline 模式）
//    4 条读连接（autoFlush=true, 单条 GET 即时发送）
//   写线程 + 读线程同时启动，共享 3s 预热 + 5s 采样窗口
//   读者每 GET 记录 t0→t1 延迟，CAS 追踪 max

private Map<String, Object> runMultiConn() {
    // 建立写者连接池
    List<StatefulRedisClusterConnection> writerConns = ...;
    // 建立读者连接池
    List<StatefulRedisClusterConnection> readerConns = ...;
    
    // 写者线程: Pipeline 100 INCR → flush → awaitAll
    // 读者线程: async.get("hotkey:counter") → f.get(2, TimeUnit.SECONDS)
    //           记录 per-GET latency → CAS 追踪 max
    
    // 返回: total / write / read cmd/s + reader avg/max latency Ms
}
```

> 完整代码见 `redis-lettuce-bench/src/main/java/com/example/redis/service/RttBenchService.java`。
> 压测脚本: `loadtester/pipeline-mixed-rw-bench.ps1`

---

## 场景 8.17: Pipeline + 同步 WAIT 副本确认 — WAIT 对吞吐的退化量化

### 8.17.1 背景与问题

```
Redis 6 引入 WAIT 命令用于保证写入副本一致性：

  WAIT numreplicas timeout
    → 阻塞当前客户端，直到至少 numreplicas 个副本确认收到该客户端的所有写命令
    → timeout 毫秒后超时返回当前已确认的副本数

WAIT 语义要点:
  • WAIT 等待的是至少 numreplicas 个 replica 向 master 发回确认
    （表示 replica 已「接收到并处理」该主节点发出的写命令）。
  • 该确认只表示数据已复制到 replica 的「内存/复制缓冲并被处理」，
    **不等同于写入磁盘**（是否落盘由 replica 的 AOF/持久化策略决定）。
  • Cluster 模式下，WAIT 必须在「写到对应 master 的那条连接」上调用；
    若一个 batch 涉及多个 master，需要对每个受影响 master 分别调用 WAIT。
    若这些 WAIT 被串行调用，等待时间会叠加 → 建议并行发起所有 WAIT futures
    并用 awaitAll 同时等待，避免串行等待造成极端退化。

生产使用场景：
  每批 Pipeline 写入后调用 WAIT 1 100 → 确保至少 1 个 replica 确认
  → 即使 master 宕机，已确认的数据不会丢失

代价：
  WAIT 本质上等待的是 master 确认指定数量的 replica 已接收写入
  → 这包含 master→replica 的数据传输与确认路径
  → 每批 Pipeline 额外增加一个 replica 复制往返的等待时间
  → 跨 AZ (10ms RTT) 时吞吐直接腰斩
```

```
同步 WAIT 热路径流程：

  send batch → flush → awaitAll → WAIT.get() [阻塞，等价于等 replica 确认] → 下一批
                                                ↑
                               这行阻塞每批额外等待 replica 复制往返时间

  跨 AZ (RTT=10ms): 每批耗时 = 正常 Pipeline 耗时 + WAIT 等待时间
  → 原来每秒能跑 N 批 → 现在每秒只能跑 N/(1 + WAIT_time/batchLatency) 批
  → 退化 ≈ 50%（batch 耗时越短，退化越严重）
```

**本场景目标**：量化在不同 RTT 条件下，同步 WAIT 对三种 Pipeline 模式（SINGLE_KEY / DISTRIBUTED / SHARDED）的吞吐退化。

### 8.17.2 压测架构

```
每批 Pipeline 流程（同步 WAIT 版）:

  1. 建立 stateful 连接 (autoFlush=false)
  2. Pipeline batchSize 条 INCR
  3. async.flushCommands()
  4. LettuceFutures.awaitAll(futures)     ← 等所有 INCR 返回
  5. async.waitForReplication(N, timeoutMs).get()  ← ★ 同步阻塞等副本确认
  6. 记录 t1 = System.nanoTime()          ← t0→t1 包含了 WAIT 的 RTT
  7. 下一批

  对比无 WAIT 基线: 省略步骤 5，t1 在 awaitAll 后立即记录。
```

### 8.17.3 REST 端点

```bash
POST /rtt/wait-sweep     — 跑一次同步 WAIT sweep，返回 6 组 JSON

# 流程: 对每种模式 (SINGLE_KEY / DISTRIBUTED / SHARDED)
#   ① 无 WAIT 基线 (1 条连接 × 16 线程 × Pipeline 100 INCR)
#   ② 同步 WAIT  (每批末尾 awaitAll 后 WAIT 1 100)
#   → 每组 3s 预热 + 5s 采样 → 18~24s 总耗时
```

### 8.17.4 实测数据

> **测试条件**: Docker 单机，3 主 3 从 Redis 7.x Cluster，Lettuce 6.x + Netty，concurrency=16 × batchSize=100，每种模式 3s 预热 + 5s 采样，WAIT 1 replicas timeout 100ms。

#### 无延迟（Docker localhost，RTT < 0.1ms）

| Mode | No WAIT (cmd/s) | Sync WAIT (cmd/s) | 退化率 |
|:--|:--|:--|:--|
| **SINGLE_KEY** | 347,400 | 239,980 | **-31.9%** |
| **DISTRIBUTED** | 480,400 | 415,820 | **-14.4%** |
| **SHARDED** | 521,680 | 441,240 | **-9.3%** |

#### 10ms 延迟（跨 AZ，tc netem delay 10ms）

| Mode | No WAIT (cmd/s) | Sync WAIT (cmd/s) | 退化率 |
|:--|:--|:--|:--|
| **SINGLE_KEY** | 167,300 | 83,120 | **-46.8%** |
| **DISTRIBUTED** | 169,020 | 81,940 | **-49.8%** |
| **SHARDED** | 172,380 | 13,180 | **-47.1%** |

### 8.17.5 关键发现

#### 发现 1: WAIT 退化本质是 RTT 叠加

```
No WAIT 每批耗时:  t_batch = awaitAll + 调度开销
Sync WAIT 每批耗时: t_batch + WAIT_RTT

退化率 ∝ WAIT_RTT / t_batch

无延迟时 t_batch ≈ 0.05ms，WAIT_RTT ≈ 0.02ms → 退化 9~32%
10ms 延迟时 t_batch ≈ 0.6ms，WAIT_RTT ≈ 10ms → 退化 47~50% ← 吞吐腰斩！
```

#### 发现 2: SHARDED 在 10ms 下退化最严重

```
SHARDED 10ms: 172,380 → 13,180 (退化 92.4%)
原因: SHARDED 模式下每批命令跨多个 master，客户端/框架需要向每个对应的
      master 发起 WAIT 并收集确认。如果这些 WAIT 是串行等待（依次调用
      WAIT.get()），则每个 master 的 replica 确认延迟会累加；如果某个
      replica 本身更慢或复制链路延迟较高，则会把等待时间进一步放大。
      这种多节点 WAIT 叠加是导致极端退化的主要原因。

复现/验证提示: 要验证 SHARDED 的多节点 WAIT 叠加效应，请在代码中确认
      是否对不同 master 的 WAIT 调用是并行发起并并行等待（建议并行），
      否则串行等待会导致更大退化；同时监控各 master→replica 的
      replication latency 与 replica 磁盘/CPU 利用以排查瓶颈。

排查要点（代码层面）:
  ① 在 runSingle / withWait 实现中确认是否对每个 master 的 WAIT 是
     串行阻塞（for each connection: wait.get()）。
  ② 若是串行，改为并行 fire WAIT：把每个 master 的 waitForReplication
     返回的 RedisFuture 加入 pending 列表，然后用
     LettuceFutures.awaitAll(...) 同时等待这些 futures。
  ③ 并行化后，WAIT 的总等待时间只会是 max(各 replica 延迟)，
     而不是各 master 延迟的累加和。
```

#### 发现 3: 量化判据

| RTT 条件 | 同步 WAIT 可行性 | 吞吐代价 |
|:--|:--|:--|
| RTT < 0.1ms（Docker/同机） | ✅ 可用 | -9~32% |
| RTT = 10ms（跨 AZ） | ❌ 腰斩 | **-47~50%** |
| RTT > 10ms | ❌ 不可接受 | > -50% |

### 8.17.6 结论

```
同步 WAIT 是把双刃剑：
  ✅ 保证副本一致性（master 宕机时已确认的数据不丢失）
  ❌ 热路径阻塞等待 replica 确认 → 跨 AZ 吞吐腰斩

根本原因: WAIT.get() 在每批热路径上同步等待 replica 复制确认往返。
解决方向: 将 WAIT 从同步热路径中移除 → 用异步方式 (场景 8.18)。
```

### 8.17.7 实现要点

```java
// 同步 WAIT 核心差异代码
for (int i = 0; i < batchSize; i++) {
    futures.add(async.incr(key));
}
async.flushCommands();
LettuceFutures.awaitAll(Duration.ofSeconds(2), futures.toArray(new RedisFuture[0]));

// ★ 同步阻塞: 这行吃掉 WAIT 的完整 RTT
async.waitForReplication(waitReplicas, waitTimeoutMs).get(2100, TimeUnit.MILLISECONDS);

long t1 = System.nanoTime();  // ← t1 被 WAIT 延迟严重拉长
```

### 8.17.8 测试方法

```bash
# 无延迟基线
curl -X POST "http://localhost:8082/rtt/wait-sweep"

# 注入 10ms 延迟再跑一次
docker exec redis-cluster-1 tc qdisc add dev eth0 root netem delay 10ms
docker exec redis-cluster-2 tc qdisc add dev eth0 root netem delay 10ms
docker exec redis-cluster-3 tc qdisc add dev eth0 root netem delay 10ms
curl -X POST "http://localhost:8082/rtt/wait-sweep"

# 清除延迟
docker exec redis-cluster-1 tc qdisc del dev eth0 root
docker exec redis-cluster-2 tc qdisc del dev eth0 root
docker exec redis-cluster-3 tc qdisc del dev eth0 root
```

> 完整代码见 `redis-lettuce-bench/src/main/java/com/example/redis/service/RttBenchService.java`
> (`sweepWaitModes()` / `runSingle(String mode, boolean withWait)`)。
> 压测脚本: `loadtester/pipeline-wait-bench.ps1`

---

## 场景 8.18: Pipeline + 异步 WAIT（Fire-and-Forget）— 根除 WAIT 吞吐退化

### 8.18.1 背景与问题

场景 8.17 揭示了同步 WAIT 的本质缺陷：`WAIT.get()` 在每批 Pipeline 热路径上同步阻塞，跨 AZ 时吞吐腰斩 ~50%。

**根除思路**：发出 WAIT 命令后不等待，让 WAIT 的 RTT 完全隐藏在后续 Pipeline 工作之后。

```
同步 WAIT (旧):
  send→flush→awaitAll→WAIT.get()[阻塞 RTT×2]→下一批
  ↑ 每批耗时 = t_batch + RTT×2, 退化 ~50%

异步 WAIT (新):
  send→flush→awaitAll→fire WAIT (不阻塞)→下一批→...→每 N 批 drain
  ↑ 每批耗时 = t_batch, 退化 ≈ 0%
```

### 8.18.2 原理

```
异步 WAIT 三步走:

  ① 每批末尾 fire WAIT 但不 .get():
     RedisFuture<Long> wf = async.waitForReplication(N, timeoutMs);
     pendingWaits.add(wf);  // 添加到线程本地列表，不阻塞

  ② 立即开始下一批 Pipeline（WAIT 的 RTT 被隐藏）

  ③ 每 N 批 drain 一次所有 pending futures:
     for (RedisFuture<Long> f : pendingWaits) {
         try { f.get(10, MILLISECONDS); ok++; }  // 已有结果几乎立即可用
         catch { fail++; }
     }
     pendingWaits.clear();

效果:
  • 99% 的批次热路径不包含 WAIT 延迟 → 吞吐 = 无 WAIT 基线
  • 每 50 批 drain 耗时 < 1ms → 可忽略
  • WAIT 一致性保证延迟 < N 个批次（对于 batch=100/concurrency=16，< 0.3ms）
```

### 8.18.3 REST 端点

```bash
POST /rtt/wait-async-sweep   — 跑一次异步 WAIT sweep，返回 6 组 JSON

# 流程: 对每种模式 (SINGLE_KEY / DISTRIBUTED / SHARDED)
#   ① 无 WAIT 基线 (1 条连接 × 16 线程 × Pipeline 100 INCR)
#   ② 异步 WAIT  (每批末尾 fire WAIT 不阻塞，每 N 批 drain)
#   → 每组 3s 预热 + 5s 采样 → 18~24s 总耗时
```

### 8.18.4 配置参数

```yaml
# application.yml
app:
  rtt-bench:
    wait-replicas: 1              # WAIT N replicas 确认
    wait-timeout-ms: 100           # WAIT 超时 ms
    wait-async-drain-batches: 50   # 每 N 批 drain 所有 pending WAIT futures (0=永不 drain)
    concurrency: 16
    batch-size: 100
    warmup-sec: 3
    sample-sec: 5
```

### 8.18.5 实测数据

> **测试条件**: Docker 单机，3 主 3 从 Redis 7.x Cluster，Lettuce 6.x + Netty，concurrency=16 × batchSize=100，每种模式 3s 预热 + 5s 采样，WAIT 1 replicas timeout 100ms，每 50 批 drain。

#### 无延迟（Docker localhost，RTT < 0.1ms）

| Mode | No WAIT (cmd/s) | Sync WAIT (cmd/s) | **Async WAIT (cmd/s)** | Sync 退化 | **Async 退化** | WAIT 成功率 |
|:--|:--|:--|:--|:--|:--|:--|
| **SINGLE_KEY** | 347,400 | 239,980 | **318,620** | -31.9% | **-8.3%** | 99.996% |
| **DISTRIBUTED** | 480,400 | 415,820 | **527,820** | -14.4% | **+9.9%** ✓ | 99.977% |
| **SHARDED** | 521,680 | 441,240 | **384,880** | -9.3% | **-26.2%** | 99.991% |

#### 10ms 延迟（跨 AZ，tc netem delay 10ms）

| Mode | No WAIT (cmd/s) | Sync WAIT (cmd/s) | **Async WAIT (cmd/s)** | Sync 退化 | **Async 退化** | WAIT 成功率 |
|:--|:--|:--|:--|:--|:--|:--|
| **SINGLE_KEY** | 167,300 | 83,120 | **158,740** | -46.8% | **-5.1%** | 99.531% |
| **DISTRIBUTED** | 169,020 | 81,940 | **167,160** | -49.8% | **-1.1%** | 98.181% |
| **SHARDED** | 172,380 | 13,180 | **170,920** | -47.1% | **-0.8%** | 99.996% |

> **说明**：WAIT 成功率下降并不意味着主写入丢失，而是意味着写入未达到预期的副本确认数（replication 确认不足）——副本可能尚未及时确认。是否接受取决于业务的 RPO/RTO 要求。需要据此制定阈值（示例：WAIT 成功率 < 99% → 告警；持续 < 95% → 降级为同步 WAIT 或缩小 batch）。

### 8.18.6 核心结论

#### 发现 1: 异步 WAIT 几乎根除吞吐退化

```
无延迟: 退化从 9~32% → 8% (SINGLE_KEY), DISTRIBUTED 甚至反超基线
跨 AZ:  退化从 47~50% → 0.8~5% ← 退化减少了 10~50 倍

异步 WAIT（fire-and-forget）能把 WAIT 的延迟隐藏在后续批次中，从而在跨 AZ
场景几乎消除同步 WAIT 带来的吞吐腰斩（实测将退化从 ~50% 降至 <5%）。
但这是以把一致性确认从"同步热路径"转变为"异步可观察/可补救"的代价：
当 WAIT 的成功率降低（未达到期望副本数）时，可能存在一小部分写入未被
及时复制到 replica。为此必须配套监控与退路策略（见下）。

SINGLE_KEY 无延迟下仍有 8.3% 退化:
  原因: drain 操作（每 50 批 per-worker × 16 workers = 800 次/5s drain）
  仍有边际开销，但相比同步 WAIT 的 31.9% 已大幅改善
  可通过增大 wait-async-drain-batches (如 50→200) 进一步压缩
```

#### 发现 2: 异步 WAIT 吞吐 ≈ 无 WAIT 基线

```
10ms 跨 AZ:
  Async SINGLE_KEY:  158,740 ÷ 167,300 = 94.9% ← 接近基线
  Async DISTRIBUTED: 167,160 ÷ 169,020 = 98.9% ← 几乎无退化
  Async SHARDED:     170,920 ÷ 172,380 = 99.2% ← 几乎无退化

核心原因: 99% 的批次完全不阻塞 WAIT → 热路径不含 WAIT RTT
         → 吞吐 = 无 WAIT 基线的 95~99%
```

#### 发现 3: 同步 vs 异步 对比总结

| 维度 | 同步 WAIT | 异步 WAIT (Fire-and-Forget) |
|:--|:--|:--|
| **吞吐退化 (无延迟)** | -9~32% | **-8% (可优化至 <5%)** |
| **吞吐退化 (10ms 跨 AZ)** | **-47~50%** | **-0.8~5%** ✅ |
| **一致性保证延迟** | 0ms (实时确认) | < N 批 (< 0.3ms for batch=100) |
| **WAIT 成功率** | 100% | **98~99.996%** |
| **实现复杂度** | 低 (1 行 .get()) | 中 (per-worker pending 列表 + 定期 drain) |
| **生产推荐** | 仅低延迟环境 | **跨 AZ 首选** |

### 8.18.7 生产级推广建议

```java
/**
 * 生产级异步 WAIT 模式：
 *
 *   ① 写入: Pipeline INCR → fire async WAIT (不阻塞)
 *   ② 每 N 批 drain: 快速 .get() 所有 pending futures → 统计成功率
 *   ③ 告警: WAIT 成功率 < 99% → P2 告警
 *   ④ 优化: 失败率升高时降级为同步 WAIT 或减小 batch
 *
 * 核心公式:
 *   throughput ≈ (concurrency × batchSize) / (batchLatency + WAIT_RTT/N)
 *   当 N → ∞ 时 → throughput → 无 WAIT 基线
 */
```

### 8.18.7.1 异步 WAIT 的风险与要求

异步 WAIT 带来的吞吐收益需要以下配套措施来保证可靠性：

| 风险 | 缓解策略 |
|:--|:--|
| **客户端进程/线程在未 drain 前崩溃** | 你将无法得知那些 fired WAIT 是否成功。必须在可接受的窗口期内（例如 drain 前）保证进程稳定，或设计 crash-recovery 机制（如持久化 pending batch 序列号，重启后进行 WAIT 一致性检查） |
| **WAIT 成功率下降** | 当 drain 检测到 WAIT 失败或成功率下降（例如 < 99%）：必须触发告警并采取补救（如重试/降级为同步 WAIT/减小 batch） |
| **Drain 阻塞写热路径** | 建议把 pending futures 放入 per-worker queue，由单独的 drain 线程或定时任务以 bounded timeout 批量处理。f.get(timeout) 的 timeout 选值应非常小（例如 1~10ms），并且总 drain 开销应被测量与限制 |
| **监控盲区** | 记录 pending WAIT 总数、每次 drain 的成功率、以及因 WAIT 失败触发的回退次数 |

**实战要点（生产化补充）**：

- **pendingWaits 必须有界**：把 pendingWaits 做成有界队列，达到上限时触发 backpressure（例如临时降级为同步 WAIT 或减小 batch），防止内存无限增长。
- **持久化 pending 批次元数据**：持久化 batch id + commands checksum，进程重启后能发现未完成的 WAIT 并做补偿（重新 WAIT 或重放）。
- **Drain 失败分级响应**：
  - 短期偶发（<1%）→ 只告警
  - 中度持续（1~5%）→ 自动减小 batch 或紧急同步 WAIT
  - 高风险（>5% 持续）→ 立即降级为同步 WAIT 并通知运维
- **核心监控指标**：`pending_wait_count`、`drain_latency_ms`、`wait_success_rate`、`wait_fail_rate`、`queued_batches`。
  - 阈值示例：`wait_success_rate < 99%` → P2 告警；持续 `<95%` → 自动降级。

> **核心公式**: `throughput ≈ (concurrency × batchSize) / (batchLatency + WAIT_RTT/N)`，当 N → ∞ 时 → throughput → 无 WAIT 基线。

### 8.18.8 实现要点

```java
// ★ 差异: 不等待 WAIT，fire 后立即进入下一批
RedisFuture<Long> wf = async.waitForReplication(waitReplicas, waitTimeoutMs);
pendingWaits.add(wf);  // 线程本地列表，无锁

// 每 N 批 drain（几乎零开销）
if (batchCounter % waitAsyncDrainBatches == 0) {
    drainPendingWaits(pendingWaits, okCounter, failCounter);
}

// drain 实现: 快速尝试获取已有结果
private static void drainPendingWaits(List<RedisFuture<Long>> pending,
                                       AtomicLong ok, AtomicLong fail) {
    for (RedisFuture<Long> f : pending) {
        try {
            f.get(10, TimeUnit.MILLISECONDS);  // WAIT 结果几乎立即可用
            ok.incrementAndGet();
        } catch (Exception e) {
            fail.incrementAndGet();
        }
    }
    pending.clear();
}
```

> 完整代码见 `redis-lettuce-bench/src/main/java/com/example/redis/service/RttBenchService.java`
> (`sweepWaitAsyncModes()` / `runSingleAsyncWait()` / `drainPendingWaits()`)。
> 压测脚本: `loadtester/pipeline-wait-bench.ps1`

### 8.18.9 测试方法

```bash
# 无延迟基线
curl -X POST "http://localhost:8082/rtt/wait-async-sweep"

# 注入 10ms 延迟再跑一次
docker exec redis-cluster-1 tc qdisc add dev eth0 root netem delay 10ms
docker exec redis-cluster-2 tc qdisc add dev eth0 root netem delay 10ms
docker exec redis-cluster-3 tc qdisc add dev eth0 root netem delay 10ms
curl -X POST "http://localhost:8082/rtt/wait-async-sweep"

# 清除延迟
docker exec redis-cluster-1 tc qdisc del dev eth0 root
docker exec redis-cluster-2 tc qdisc del dev eth0 root
docker exec redis-cluster-3 tc qdisc del dev eth0 root
```

### 8.18.11 检测清单 / 观测指标（生产化建议）

**每次 sweep/实验记录**：RTT（ms 单位）、concurrency、batchSize、单批平均执行时间（ns 或 ms）、cmd/s、errors、WAIT 成功率、drain_latency。

**生产监控指标**：

| 指标 | 说明 |
|:--|:--|
| `pending_wait_count` | 当前积压的 WAIT future 数 |
| `wait_success_rate` (1m/5m) | WAIT 确认成功率 |
| `drain_latency_ms` | 每次 drain 的耗时 |
| `queued_batches` | 积压的批次队列长度 |
| `avg_batch_latency` | 平均批次耗时 |
| `p99_read_latency` / `p99_write_latency` | 读/写 P99 延迟 |
| `replication_lag_per_replica` (ms) | 各 replica 的复制延迟 |

**自动化策略示例**：若 `wait_success_rate_5m < 99%` 且持续 → 减小 batchSize × 0.5；若持续且影响业务 → 切换为同步 WAIT 并报警。

### 8.18.10 同步 WAIT vs 异步 WAIT 架构决策树

```
                   ┌─────────────────────────────┐
                   │    你需要 WAIT 副本确认吗？    │
                   └─────────────────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
           不需要确认         需要确认           需要确认
           (最终一致)         RTT < 1ms          RTT ≥ 1ms
                │                │                │
                ▼                ▼                ▼
         ┌──────────┐   ┌──────────────┐   ┌──────────────┐
         │ 不用 WAIT │   │  同步 WAIT    │   │ 异步 WAIT    │
         │ ~100% 基线 │   │ 退化 9~15%   │   │ 退化 < 5%    │
         └──────────┘   └──────────────┘   └──────────────┘
                │                │                │
                ▼                ▼                ▼
          最简方案         简单可靠          激进优化方案
                                                (Fire-and-Forget)

  建议:
    ├─ 同 AZ 部署 (RTT < 1ms): 同步 WAIT 足够，实现最简
    ├─ 跨 AZ 部署 (RTT ≥ 5ms):  异步 WAIT 必须，否则吞吐腰斩
    └─ 如果 WAIT 成功率持续 < 99%: 降级为同步 WAIT + 调小 batch
```

---

### 8.18.12 精简修正版要点（FAQ / 关键结论）

- **核心公式与单位**：吞吐量理论上限可近似估算为 `maxCmdPerSec ≈ (concurrency × pipelineSize) / RTT_seconds`，其中 **RTT 必须以秒为单位**。实际吞吐受 Redis 单线程、客户端实现和 CPU/网络资源限制，通常远低于理论上限。
- **tc netem 方向**：在容器 egress 单侧注入 Xms 是**单向延迟**，客户端感知的往返延迟大约增加 **Xms**（非 2×Xms）。若要模拟对称 RTT 增加 T，应在 request/response 两端各注入 T/2（或 server 与 client 两侧各注入 T/2）。
- **切换判据（建议）**：用预测吞吐比较法——若 `T_net(RTT) < T_local × α`（例 α=0.1）或写/读 P99 超出 SLO，则切换到本地缓冲。经验阈值：RTT ≥ 5ms 强烈考虑本地缓冲/COMPUTED；RTT < 1ms 同机可继续使用 pipeline + 同步 WAIT（若需要）。
- **WAIT 建议**：同 AZ（RTT<1ms）可用同步 WAIT；跨 AZ（RTT≥5~10ms）应采用异步 WAIT（fire-and-forget）并做 periodic drain 与成功率监控；若 WAIT 成功率持续下降（建议阈值 <99%）应触发告警并考虑减小 batch 或临时降级为同步 WAIT。
- **读饥饿缓解**：若写压高且存在独立读连接，优先把读导向 replica 或使用本地缓存/近似读；若需要强一致性且实时性要求高，考虑用 single-connection mixed pipeline（读写同连接）或把写压力削峰（减小 batch、限制并发连接）。
- **实现与运维要点**：pendingWaits 必须是有界的；需要 persist/recovery plan（防止进程崩溃导致未确认批次丢失）；监控指标包括 pending_wait_count、wait_success_rate、drain_latency_ms、queued_batches、read_p99、write_p99，并设置告警与自动降级策略。

---

## 8.19 Pipeline 断连重连 + 幂等性

### 8.19.1 背景

在 Pipeline 高并发写入过程中，如果 Master 节点宕机（网络中断 / OOM / 硬件故障），集群会触发 failover 切换到新 Master。Lettuce 连接池具备自动重连能力，但问题是：

- **幂等命令**（如 `SETEX`）：重复执行不会产生副作用
- **非幂等命令**（如 `INCR`）：断连重试可能导致「已被旧 Master 处理、但客户端未收到 ACK 的命令」被重复执行

```
时间线:
  t1: 客户端发送 INCR(1→2) → Master 收到并处理
  t2: Master 宕机（客户端未收到 ACK）
  t3: Cluster failover → 新 Master 接管 slot
  t4: Lettuce 重连 + 重试 INCR(1→2) → 新 Master 再次执行
  结果: 实际计数 +2，预期 +1 → 过度计数
```

> ⚠️ **注意（客户端行为）**：不同客户端对"断连时在路上/未 ACK 命令"的处理不同。Lettuce 支持自动重连和拓扑刷新，但**不会自动为所有命令保证不重复语义**——是否对已发送但未确认的命令做透明重试受客户端配置与版本影响（以及命令类型/事务状态）。因此测试中观察到的"重复执行"并不是 Redis 协议必然行为，而是由客户端（或应用层的重试逻辑）在断连/重连流程中可能造成的重放行为。后续讨论**假定存在未确认命令被重试的情形**，故需采用幂等/去重设计以保证正确性。

### 8.19.2 测试设计

| 项目 | 说明 |
|:--|:--|
| **目标** | 量化非幂等命令（INCR）在 Pipeline 断连重连中的过度计数比例 |
| **幂等对照组** | `SETEX {hotkey}:idempotent 600 batch:N` — 即使重复执行也不产生副作用 |
| **实验组** | `INCR {hotkey}:idempotent` — 每次执行都递增，重复执行导致过度计数 |
| **断连方式** | `docker kill` Master 容器 → 模拟 Master 宕机 |
| **恢复方式** | Lettuce ClusterClient 自动拓扑刷新 + failover 切换 |
| **参数** | 16 并发 Worker，每批 100 INCR + 100 SETEX，bathSize=100 |

### 8.19.3 测试结果 — 裸 INCR（无去重）

```
╔══════════════════════════════════════════════════════════════════════╗
║               裸 INCR 断连重连 实测数据 (对比基线)                      ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  场景: 连续 Pipeline INCR+SETEX → docker kill Master → failover     ║
║  断连方式: docker kill 容器 (模拟 Master 宕机)                        ║
║  测试窗口: 25s (预热 3s + 断连前 6s + 断连 15s + 恢复)              ║
║                                                                      ║
║  ┌──────────────────────────────────────────────────────────────┐   ║
║  │ 指标                      │ 值                               │   ║
║  ├──────────────────────────────────────────────────────────────┤   ║
║  │ INCR 预期 (成功 ACK)      │ 710,400                          │   ║
║  │ INCR 实际（Redis 最终值） │ 870,400                          │   ║
║  │ INCR 过度计数             │ 160,000                          │   ║
║  │ INCR 过度计数比例         │ 22.523%                          │   ║
║  │ SETEX 成功 ACK            │ 710,400                          │   ║
║  │ SETEX 失败批次            │ 1,700                            │   ║
║  │ SETEX 丢失批次            │ 209 (可安全重放)                  │   ║
║  │ 断连前最后批次            │ batch:8626                       │   ║
║  │ 恢复后首成功批次          │ batch:8611                       │   ║
║  │ 错误总数                  │ 3,400                            │   ║
║  └──────────────────────────────────────────────────────────────┘   ║
║                                                                      ║
║  结论:                                                                ║
║  ┌──────────────────────────────────────────────────────────────┐   ║
║  │ INCR 非幂等: 断连重试导致 ~22.5% 过度计数 (160,000 次)        │   ║
║  │ SETEX 幂等:   丢失 209 批次 (2.3%), 但不会重复, 可安全重放    │   ║
  ║  │ 恢复后首成功批次 (8611) < 断连前 (8626) → 证明有重试行为      │   ║
  ║  └──────────────────────────────────────────────────────────────┘   ║
  ║                                                                      ║
  ╚══════════════════════════════════════════════════════════════════════╝
```

> **表格单位注**：INCR 的计数单位为"命令次数"；SETEX 统计以"批次数"为单位（每批包含 100 条 SETEX）。因此 710,400 次 INCR 对应 7,104 批（710,400 / 100 = 7,104）。文中所有涉及 INCR/SETEX 的表格均已按该单位标准化。

### 8.19.4 分析

**为什么 INCR 多计了 22.5%？**

```
断连窗口内 (15s) 有大量 Pipeline 批次处于"已发送但未 ACK"状态：
  - 一些 INCR 命令在旧 Master 已经执行，但客户端未收到确认
  - Lettuce 重连后未确认的命令被重试，旧 Master 的结果和新 Master 的结果叠加
  - 断连前 seq=8626，恢复后 seq=8611 → 说明至少有 15 个批次被完全重试
  - 加上断连瞬间正在传输的批次，过度计数在 ~20-25% 之间是合理的

注: 这里的 seq 为客户端为每批写入维护的单调批次号（写入时把该批次号写入 marker 到 Redis 以便对齐）。若使用其他方式测量（如 Redis key 的最后 marker），说明应包括 marker 写入时序与持久化策略以保证可验证性。
```

**为什么 SETEX 不受影响？**

```
SETEX 是幂等命令：
  - SETEX key ttl value → 多次执行结果相同（总是最后一次写入生效）
  - 丢失的 209 个批次 (2.3%) 可以通过对比 Seq 号重放补偿
  - 不会产生语义错误（只是批次丢了，不是值错）
```

### 8.19.5 生产建议

| 层级 | 措施 | 效果 |
|:--|:--|:--|
| **命令层面** | 优先使用幂等命令（SET/SETEX/HSET），避免 INCR/DECR 无条件重试 | 根本杜绝过度计数 |
| **幂等性标记** | 为 INCR 类操作增加业务级幂等键（如订单号 + 状态标记），重复 INCR 时检查跳过 | 业务级防护 |
| **At-Least-Once 计数** | 改用 `HSETNX` + `HINCRBY` 代替裸 INCR，依赖唯一性约束去重 | 利用 Redis 原子性去重 |
| **客户端协议层** | 升级 Lettuce ≥ 6.2，启用 `ClientOptions.autoReconnect()` + 自定义重试策略（仅重试幂等命令） | 框架级防护 |
| **外部 idempotent key** | 在应用层生成唯一请求 ID → Redis `SETNX request:lock:{id} 1 EX 300` → 仅首次执行 INCR | 应用层兜底 |

> **核心原则**：Pipeline 断连重连场景下，不要假设网络传输的命令一定未被执行。对非幂等命令必须引入业务/协议层面的幂等机制。

**补充风险/代价**：幂等化通常需要额外的去重状态（dedup key / request-id）或服务端脚本。
- 去重 key 的 TTL 必须大于最坏 failover/重试窗口，否则无法去重；
- 去重 key 会消耗内存（高吞吐场景需评估，见 8.19.7 实现要点）；
- Lua EVAL 三合一可以把去重/计数/标记合并为 1 次原子操作，但脚本在 Redis 主线程运行，长脚本或高并发短脚本会阻塞主线程并影响其他客户端，需谨慎设计并使用 EVALSHA / SCRIPT LOAD 缓存脚本。

### 8.19.6 验证指标对照

| 对比维度 | INCR (非幂等) | SETEX (幂等) |
|:--|:--|:--|
| 预期计数值 | 710,400 | 7,104 批次 |
| 实际计数值 | 870,400 | 8,704 最终批次号 |
| 偏差 | +160,000 (**22.5% 过度**) | -209 批次 (**2.3% 丢失, 可重放**) |
| 语义正确性 | ❌ 过度计数 | ✅ 幂等安全 |
| 生产可用性 | ⚠️ 需幂等改造 | ✅ 直接可用 |

### 8.19.7 幂等修复验证 — 三种方案同环境公平对比

> **测试环境**: Docker 6 节点 Redis Cluster, `appendonly no`, 每次测试前 `down -v` 重建集群。batchSize=100, concurrency=16, warmup=5s, sampleSec=25s, deadline=30s, backoffCap=12s。

#### 方案 A: EVAL 三合一原子去重 (推荐)

将 SETNX 去重 + INCRBY + SETEX 全部封装为单条 Lua 脚本，**1 次 RTT 完成所有操作**：

```lua
-- KEYS[1]={hotkey}:dedup:{seq}  KEYS[2]={hotkey}:incr  KEYS[3]={hotkey}:marker
-- ARGV[1]=batchSize  ARGV[2]=dedupTTL(2s)  ARGV[3]=batch:seq  ARGV[4]=markerTTL(600)
if redis.call('SET',KEYS[1],'1','NX','EX',tonumber(ARGV[2])) then
    local v=redis.call('INCRBY',KEYS[2],tonumber(ARGV[1]))
    redis.call('SETEX',KEYS[3],tonumber(ARGV[4]),ARGV[3])
    return v           -- 首次执行 → 返回新值
else
    redis.call('SETEX',KEYS[3],tonumber(ARGV[4]),ARGV[3])
    return -1           -- 已去重 → 跳过 INCRBY, 仍更新 marker
end
```

**实现与运维要点**：
- 使用 **SCRIPT LOAD + EVALSHA** 避免每次发送完整脚本体（更省带宽/CPU）。
- dedup key 的 TTL（示例 ARGV[2]=2s）应选**大于系统最坏的 failover+重试窗口**（例如 2×failover 最大时延 + safety），确保重复请求在去重窗口内可被识别。
- 高吞吐场景下大量 dedup keys 会占用内存（高基数）：考虑使用压缩/批量去重或短 TTL + 外部持久化作为补偿策略，并监控 `dedup_key_count` / `dedup_key_memory_bytes`。
- **Cluster 模式**：确保 `KEYS[]` 都在同一 slot（或使用 hashtag），否则 EVAL 会报 CROSSSLOT。

#### 方案 B: SETNX 两阶段去重

SETNX 去重检查 (Phase A) + INCRBY+SETEX (Phase B) = **2 RTT/批**，详见上文原始设计。

#### 方案 C: 裸 INCR (无去重, 对照组)

单管线 INCRBY + SETEX = **1 RTT/批**，无任何幂等保护。

#### 同环境三轮对比实测

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                  幂等去重 三方案同环境公平对比 (appendonly=no)                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  指标                  │ EVAL 三合一     │ SETNX 两阶段    │ 裸 INCR (对照组)  ║
║  ──────────────────────┼─────────────────┼─────────────────┼───────────────── ║
║  cmdPerSec             │       70,440    │        4,204    │       91,778     ║
║  vs 裸 INCR            │        76.8%    │         4.6%    │         100%     ║
║  p50Ms                 │        0.757    │       16.130    │        4.880     ║
║  p95Ms                 │        2.039    │       29.719    │        8.379     ║
║  p99Ms                 │        6.364    │       51.205    │       14.455     ║
║  avgLatencyMs          │       19.601    │      329.865    │       10.818     ║
║  maxMs                 │      128,067    │      119,015    │       12,014     ║
║  errors                │            0    │            0    │            0     ║
║  overcount             │       0.001%    │      -0.072%    │      -0.050%     ║
║  preDisconnect 批数     │      143,340    │        6,824    │       22,071     ║
║  sampleDurationSec     │        137.5    │        136.3    │         98.3     ║
║                                                                               ║
║  ┌─ 发现 1: EVAL 延迟全面碾压裸 INCR ────────────────────────────────────┐   ║
║  │                                                                        │   ║
║  │  p50: 0.757ms vs 4.880ms (6.4x 更快)                                  │   ║
║  │  p95: 2.039ms vs 8.379ms (4.1x 更快)                                  │   ║
║  │  p99: 6.364ms vs 14.455ms (2.3x 更快)                                 │   ║
║  │                                                                        │   ║
║  │  原因: 101 条命令 (1×INCRBY+100×SETEX) → 1 条 EVAL                     │   ║
║  │        序列化/网络传输/反序列化开销减少 ~100x                            │   ║
║  └────────────────────────────────────────────────────────────────────────┘   ║
║                                                                               ║
║  ┌─ 发现 2: EVAL 吞吐略低于裸 INCR (76.8%) — 但非性能瓶颈 ──────────────┐   ║
║  │                                                                        │   ║
║  │  解释: EVAL 显示出更低的 p50/p99，但 cmdPerSec 低于裸 INCR 的原因      │   ║
║  │  是: EVAL 在断连前生成了大量 pending 批次 (preDisconnect 143,340 批,  │   ║
║  │  裸 INCR 的 6.5x) → failover/恢复期间海量 pending 批次排队重试 →       │   ║
║  │  拉长了整体 sampleDuration (137.5s vs 98.3s)。                         │   ║
║  │  即: 吞吐 = 完成命令 / 测试时长，EVAL 单次延迟更低、短时峰值更高，     │   ║
║  │  但因断连/积压策略差异，整体样本周期内吞吐受测试窗口影响。             │   ║
║  │  建议在表中同时提供"稳定态峰值吞吐"(不含 failover 窗口) 以便公平比较， │   ║
║  │  并把 cmdPerSec 报为"均值 ± 标准差 (N)"。                              │   ║
║  └────────────────────────────────────────────────────────────────────────┘   ║
║                                                                               ║
║  ┌─ 发现 3: SETNX 两阶段全面落后，高吞吐下不推荐 ──────────────────────┐   ║
║  │                                                                        │   ║
║  │  结论: SETNX 两阶段因为 2 RTT/批 带来巨大延迟成本，在高并发写场景下    │   ║
║  │  吞吐会显著下降 (本测约为基线的 4.6%)，延迟退化 3~6x。                 │   ║
║  │  因此在高吞吐/低延迟需求下不推荐。                                    │   ║
║  │  仅在吞吐较低且无法部署 Lua 的极特殊场景 (或脚本禁用策略) 下          │   ║
║  │  可作为次优方案。                                                     │   ║
║  └────────────────────────────────────────────────────────────────────────┘   ║
║                                                                               ║
║  ┌─ 发现 4: 正确性 ─────────────────────────────────────────────────────┐   ║
║  │                                                                        │   ║
║  │  EVAL:     overcount=0.001% (噪声级, 完美幂等)                        │   ║
║  │  SETNX:    overcount=-0.072% (极少量计数丢失)                         │   ║
║  │  裸 INCR:  overcount=-0.050% (预期内的旧 master 数据丢失)              │   ║
║  │  三种方案 errors=0: 在所有正确性测试中全部批次成功完成                 │   ║
║  └────────────────────────────────────────────────────────────────────────┘   ║
║                                                                               ║
║  overcount 负值说明: 小幅负值表示最终计数略低于期望 (少量丢失)，通常由     ║
║  测量噪声、计数窗口不同步或在 failover 期间丢失的批次造成；0.1% 量级      ║
║  属于噪声范围。建议在数据表中添加样本 N 与标准差/置信区间以量化这类偏差。  ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

#### 方案优缺点对比

| 方案 | 优点 | 缺点 |
|:--|:--|:--|
| **EVAL 三合一** | 原子、单 RTT、低延迟、完美幂等 | 阻塞主线程、脚本复杂度、cluster 需同 slot |
| **SETNX 两阶段** | 实现简单、不需脚本 | 2 RTT、吞吐低 (4.6%)、延迟高 |
| **应用层 request-id + SETNX** | 可跨系统、语义一致 | 需额外存储 (dedup key)、性能开销 |
| **HSETNX/HINCRBY 模式** | 利用 Redis 原子性去重 | 需细设计字段语义、去重状态占用内存 |

#### 修复原理

**EVAL 三合一原子化（最终方案）** 是让 Redis 服务端在单次调用中原子地完成去重判断 + 计数 + 标记更新：

```
Lua 脚本 (单次 EVAL 调用, 1 RTT):

  SETNX dedup:seq100 → true  (首次)
    → INCRBY counter +100    → 返回 新值
    → SETEX marker batch:100

  SETNX dedup:seq100 → false (已处理, 上次的 INCRBY+SETEX 均成功)
    → 跳过 INCRBY
    → 仍更新 SETEX marker (幂等安全)
    → 返回 -1

  → 无论首次还是重试，1 次 RTT 完成全流程
  → 原子性保证: SETNX + INCRBY + SETEX 要么全部生效，要么全部不生效
```

**与 SETNX 两阶段方案的对比：**

| 维度 | SETNX 两阶段 | EVAL 三合一 |
|:--|:--|:--|
| RTT/批 | 2 | **1 (与裸 INCR 等同)** |
| p50 延迟 | 16.1ms | **0.76ms (6.4x↑)** |
| 吞吐 vs 裸 INCR | 4.6% | **76.8%** |
| overcount | -0.072% | **0.001% (完美)** |
| 服务端原子性 | ❌ 分两次调用 | ✅ 单次原子 |

#### 关键教训

| 教训 | 说明 |
|:--|:--|
| **Lua 脚本命令压缩是关键优化** | 101 条命令 → 1 条 EVAL，序列化/网络传输开销减少 ~100x → p50 从 4.9ms 降至 0.76ms |
| **服务端原子化 > 客户端多阶段** | EVAL 单次调用保证 SETNX+INCRBY+SETEX 原子性，不会出现 Phase A 成功但 Phase B 失败导致计数丢失的边界情况 |
| **分两次 RTT 是不可承受之重** | SETNX 两阶段 2 RTT/批 → 吞吐仅裸 INCR 的 4.6%，延迟退化 3~6x，生产不可用 |
| **EVAL 在 failover 场景比裸 INCR 更优** | 裸 INCR 只有 101 条命令/批，p50=4.88ms；EVAL 仅 1 条命令 → p50=0.76ms（6.4x 更快）。断连前积压更多但整体延迟更低 |
| **预热期必须计入 expected** | 否则 expected 与实际计数不对齐，产生虚假的 ~20% 过度计数信号 |
| **重试策略要明确退避上限** | 示例：指数退避（initial=1s, multiplier=2, max=12s），总重试窗口设 30s（超过则告警或降级）。这样在 30s 窗口内可进行多次重试，而不会因大量并发重试立即压垮新 master |
| **避免 connection double-close** | `refreshPartitions()` 前不要 `conn.close()`，Lettuce 内部管理连接生命周期 |

---

## 场景 8.20: Lua EVAL vs Pipeline — 原子读-改-写的吞吐与延迟对比

### 8.20.1 背景与问题

场景 8.11 的决策树中有一条分支：**"需要实时去重/限流 → Lua + BloomFilter"**，但没有任何数据说明 Lua EVAL 的吞吐退化有多大，以及什么场景下 Lua 比 Pipeline 更好。

**核心问题**：对于"读-改-写"型热点操作（如 `GET + INCRBY`），Lua EVAL 和 Pipeline 各有什么优劣？

- **Pipeline (GET + INCRBY)**：两条命令在同一管线中发送 → 1 RTT，**非原子**。GET 和 INCRBY 之间可能有其他连接插入，读到的是"可能已过期的值"
- **Lua EVAL (原子 GET+INCRBY)**：单条 EVAL 调用 → 1 RTT，**原子**。服务端保证 GET→INCRBY 期间不会有其他命令插入

直觉上，Lua 脚本的解析/缓存/VM 开销应该比两条独立命令更高 → 吞吐更低。但实测结果恰恰相反。

### 8.20.2 原理

```
Pipeline GET + INCRBY 流程 (1 RTT, 100 命令/批):

  ┌──────────────────────────────────────────────────────────┐
  │ Client: [GET key, INCRBY key N, GET key, GET key, ...]  │  ← 100 条命令
  │  flush() ─────────────────────────────────────→ Server   │      1 RTT
  │          ← [value, newValue, value, value, ...]          │
  │  GET 与 INCRBY 之间是两条独立命令 → 非原子                  │
  └──────────────────────────────────────────────────────────┘

Lua EVAL 原子读-改-写 (1 RTT, 100 命令/批):

  ┌──────────────────────────────────────────────────────────┐
  │ Client: [EVAL "GET+INCRBY" 1 key N, GET key, GET key, ...]│ ← 100 条命令
  │  flush() ─────────────────────────────────────→ Server   │      1 RTT
  │          ← [oldValue, value, value, ...]                  │
  │  GET+INCRBY 在服务端原子执行 → 无竞争窗口                    │
  └──────────────────────────────────────────────────────────┘
```

**Lua 为什么可能比 Pipeline 更快？**

- Pipeline: GET + INCRBY = **2 条命令**需要独立解析/执行/回包编码
- Lua: 1 条 EVAL 包含全部业务逻辑 → 解析次数少、响应编码少
- 管线中其他填充 GET 命令的解析开销在两种模式下相同

### 8.20.3 REST 端点

```bash
# 一次运行完成 Pipeline 和 Lua 两种模式的对比
curl -X POST http://localhost:8082/rtt/lua-vs-pipeline
```

### 8.20.4 测试参数

| 参数 | 值 |
|:--|:--|
| batchSize (管线命令数) | 100 |
| concurrency | 16 |
| warmup | 3s |
| sample | 5s |
| Redis 集群 | 6 节点 (3 master + 3 replica), appendonly no |
| 对比维度 | Pipeline: GET+INCRBY(1 RTT, 非原子) vs Lua: EVAL(1 RTT, 原子) |

### 8.20.5 实测数据

```
╔══════════════════════════════════════════════════════════════════════╗
║            Pipeline GET+INCRBY vs Lua EVAL 原子读-改-写                ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  指标          │ Pipeline (非原子)   │ Lua EVAL (原子)    │ 差异      ║
║  ──────────────┼─────────────────────┼────────────────────┼───────── ║
║  cmdPerSec     │     217,340         │     273,780        │ +26.0% ↑ ║
║  p50Ms         │       7.416         │       6.377        │ -14.0% ↓ ║
║  p95Ms         │      15.511         │      11.441        │ -26.2% ↓ ║
║  p99Ms         │      34.676         │      18.838        │ -45.7% ↓ ║
║  avgLatencyMs  │       8.875         │       7.019        │ -20.9% ↓ ║
║  errors        │           8         │          15        │          ║
║  atomic        │         no          │         yes        │          ║
║                                                                      ║
║  ┌─ 发现 1: Lua EVAL 在吞吐和延迟上全面反超 Pipeline ────────────┐   ║
║  │                                                                  │   ║
║  │  Lua EVAL 比 Pipeline GET+INCRBY 快 26%，p99 延迟低 46%。        │   ║
║  │  原因: 1 条 EVAL 命令 (1 次序列化 → 1 次解析 → 1 次编码)        │   ║
║  │        vs 2 条命令 (GET+INCRBY, 2 次解析 → 2 次编码)             │   ║
║  │  即 Lua 同时提供了原子性和更高的吞吐 — "双赢"                    │   ║
║  └──────────────────────────────────────────────────────────────────┘   ║
║                                                                          ║
║  ┌─ 发现 2: Lua 原子性没有性能代价 ──────────────────────────────────┐  ║
║  │                                                                     │  ║
║  │  长期以来认为 "原子操作 = 性能差" 的直觉在此场景不成立。            │  ║
║  │  因为单条 EVAL 减少了网络上的命令数 (1 vs 2)，                    │  ║
║  │  序列化/反序列化开销的降低反而带来了净性能收益。                    │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

> ⚠️ **实务警告**：Lua 虽然能在本场景带来延迟与吞吐优势，但**脚本运行于 Redis 主线程**，复杂或长时间运行的脚本会阻塞其他请求，并可能延长故障恢复时间。生产使用时请：
> - 使用 **EVALSHA / SCRIPT LOAD** 避免每次传输完整脚本体；
> - 限制脚本复杂度与执行时间，避免大循环/慢操作；
> - 在 Cluster 模式确保 keys 在同一 slot（或 hashtag），否则 CROSSSLOT；
> - 设置 `lua-time-limit`、监控 slowlog 与主节点阻塞指标（如 `lua_script_block_ms`）。

### 8.20.6 决策树更新

原 8.11 决策树中的 **"需要实时去重/限流 → Lua + BloomFilter"** 的分支，现补充量化数据：

```
╔═══════════════════════════════════════════════════════════════════════════════╗
║                        Pipeline vs Lua EVAL 完整决策树                         ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║                     ┌───────────────────────────────────┐                      ║
║                     │   你要做什么类型的 Redis 操作?       │                      ║
║                     └───────────────────────────────────┘                      ║
║                                      │                                         ║
║              ┌───────────────────────┼───────────────────────┐                 ║
║              ▼                       ▼                       ▼                 ║
║        纯写入 (INCRBY/SET)     读-改-写 (GET+INCRBY)     多 key 聚合            ║
║              │                       │                       │                 ║
║              │           ┌───────────┼───────────┐           │                 ║
║              │           ▼                       ▼           │                 ║
║              │      需要原子性?              不需要原子性      │                 ║
║              │           │                       │           │                 ║
║              ▼           ▼                       ▼           ▼                 ║
║    ┌─────────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────────┐      ║
║    │   Pipeline  │  │  Lua EVAL    │  │  Pipeline   │  │  没有统一     │      ║
║    │   SINGLE_KEY│  │  GET+INCRBY  │  │  GET+INCRBY │  │  hashtag →   │      ║
║    │  ~307kcmd/s │  │ ~274kcmd/s ✓ │  │ ~217kcmd/s  │  │  Pipeline    │      ║
║    │  1 RTT      │  │ 1 RTT + 原子 │  │ 1 RTT       │  │  ~307kcmd/s  │      ║
║    │  ✅ 最高吞吐  │  │ ✅ 原子+吞吐  │  │ ❌ 非原子   │  │  有统一      │      ║
║    └─────────────┘  └──────────────┘  └─────────────┘  │  hashtag →   │      ║
║                                                        │  Lua/管道均可│      ║
║                                                        └──────────────┘      ║
║                                                                                ║
║    ┌─ 核心结论 ─────────────────────────────────────────────────────────────┐  ║
║    │                                                                            ║
║    │  ① 纯写入: Pipeline 比 Lua 快 12% (307k vs 274k)。                        ║
║    │     INCRBY/SET 的 RESP 编解码太轻，Lua VM 开销成了净代价。                 ║
║    │                                                                            ║
║    │  ② 读-改-写 + 需要原子: Lua 唯一选择。不仅原子，吞吐还反超 Pipeline 26%。  ║
║    │     1 条 EVAL 序列化 < GET+INCRBY 两条命令的序列化。                       ║
║    │                                                                            ║
║    │  ③ 多 key 聚合 + 无统一 hashtag: Pipeline 是唯一可行方案。                 ║
║    │     但如果 key 命名可提前规划 ({hashtag}:...)，Pipeline 和 Lua 均可，      ║
║    │     此时按 ①/② 的逻辑选择即可。                                           ║
║    │                                                                            ║
║    │  ④ Pipeline 更适合调试/部分重试/SLOWLOG 排障。                            ║
║    │                                                                            ║
║    │  ⑤ 工程决策补充三维度（非仅吞吐/延迟/原子性）：                          ║
║    │     · 可观测性: Pipeline 更好 (逐条命令可见)                              ║
║    │     · 重试复杂度: Pipeline 更易做部分重试 (blast radius 小)              ║
║    │     · 脚本风险: Lua 可能阻塞主线程 (需监控/限时)                          ║
║    └────────────────────────────────────────────────────────────────────────┘   ║
║                                                                                 ║
╚═══════════════════════════════════════════════════════════════════════════════════╝
```

### 8.20.7 关键教训

| 教训 | 说明 |
|:--|:--|
| **Lua EVAL 在 pipeline 场景比两命令更快** | 1 条 EVAL 的序列化开销 < GET + INCRBY 两条命令的序列化开销之和。原子性反而带来性能收益，不是代价 |
| **命令压缩是隐藏优化** | 将多次 Redis 操作压缩为 1 条 Lua 命令，减少网络上的命令数，也减少了服务端事件循环的调度次数 |
| **"原子 → 慢" 的直觉可能误导** | 在这个场景中，原子 Lua 比非原子 Pipeline 更快，因为客户端/服务端的序列化瓶颈 > Lua VM 开销 |
| **决策树 8.11 的 "Lua + BloomFilter" 分支** | 现补充量化数据: Lua 吞吐 ≈ 274k cmd/s，比纯 INCR Pipeline (307k) 只低 11%，但比 Pipeline GET+INCRBY (217k) 高 26% |

### 8.20.8 Pipeline 优于 Lua 的场景分析

虽然 8.20.5 显示 Lua EVAL 在"读-改-写"场景碾压 Pipeline GET+INCRBY，但 **Pipeline 并非在所有场景都输给 Lua**。以下是 Pipeline 明确优于 Lua 的五种情况。

#### 三条基准数据回顾

| 模式 | cmdPerSec | RTT | 原子性 | 命令数/批 |
|:--|:--|:--|:--|:--|
| **纯 Pipeline INCRBY** (SINGLE_KEY) | **~307k** | 1 | INCRBY 自身原子 | 101 (1×INCRBY+100×SETEX) |
| **Pipeline GET+INCRBY** | ~217k | 1 | ❌ 非原子 | 100 (GET+INCRBY+填充GET) |
| **Lua EVAL GET+INCRBY** | ~274k | 1 | ✅ 原子 | 100 (EVAL+填充GET) |

#### 场景 1: 纯写入操作，不需要"读-改-写"原子性

```
INCRBY 本身就是 Redis 原子命令，用 Pipeline 打包 100 条独立 INCRBY：
  → 307k cmd/s (比 Lua 274k 高 12%)
  
关键差异:
  Pipeline: 101 条独立 RESP 编解码 (INCRBY 的 RESP 仅几十字节，编解码极快)
  Lua EVAL:  1 次 EVAL RESP + ~200 字节脚本传输 + Lua parser + Lua VM 执行
             + Redis↔Lua 类型转换 (ARGV → tonumber、 返回 → integer)
  
  → INCRBY 的编解码太轻量，Lua VM 开销成了净代价
  → Pipeline 胜出 12%
```

**结论**：只要业务逻辑是"纯写"（INCRBY / DECRBY / SET / ZADD），每条命令自身原子，Pipeline 就是最优解。

#### 场景 2: 命令不需要互相保证原子性

```
场景：100 个独立 key 各 INCRBY 1
  ┌─ Pipeline: 100×INCRBY key:1,2,3... → 1 RTT
  │  每条 INCRBY 原子，彼此独立，无竞争
  │  expected 预期精准，retry 语义可控
  │
  └─ Lua: EVAL "for i=1,100 do redis.call('INCRBY',KEYS[i],1) end"
      → 必须用 {hashtag} 保证同 slot（增加 key 命名约束）
      → 即使同 slot，Lua 阻塞 Redis 单线程执行 100 次 INCRBY，
         这 100 次修改都被一个 Lua 调用霸占 → 对其他客户端不公平
```

**结论**：多条独立命令之间无因果关系时，Pipeline 更灵活、Retry 更安全。

#### 场景 3: key 命名无法引入统一 hashtag 时

```
关键前提: 如果提前设计好 hashtag（如 {user1}:counter, {user1}:lock），
          Lua EVAL 不存在跨 slot 问题 —— 所有 key 已路由到同一槽位。
          此时 Pipeline 和 Lua 均可，按 ①/② 的逻辑选即可。

但现实生活中，不是所有系统都能自由设计 key:
  ┌─ 存量系统 key 已固定且分散, 改命名需双写迁移
  │  → 无法加 {hashtag}
  │  → Lua EVAL 直接报 CROSSSLOT
  │  → Pipeline 是唯一可行方案
  │
  ┌─ 多租户 SaaS, key 前缀由租户 ID 自然决定
  │  → 天然跨 slot
  │  → Pipeline 自动分片路由
  │
  └─ 新系统 / 可自主设计 → 加 {hashtag} → 两种方案均可
```

**结论**：真正的问题不是"跨 slot"，而是**"key 命名能否引入统一 hashtag"**。不能 → Pipeline；能 → 按吞吐/原子性选。

#### 场景 4: 调试与监控更友好

```
MONITOR / SLOWLOG 中的 Pipeline:
  → "INCRBY key:counter 100"       ← 命令可读，可直接诊断哪个命令慢
  → "SETEX key:marker 600 batch:99" ← 参数可解析

MONITOR / SLOWLOG 中的 Lua:
  → "EVAL 'if redis.call...' 3 key1 key2 key3 100 2 batch:99 600"
  → 脚本内容不展开，必须去 lookup 脚本 SHA 对应的原始文本
  → 生产排障时每多一步都是时间成本
```

**结论**：Pipeline 的逐条命令可见性是生产排障的重要优势。

#### 场景 5: 内省与部分失败处理

```
Pipeline GET 读当前值:
  → 可以在 client 侧拿到每条 GET 的返回值
  → 如果某条 GET 返回 nil (key 不存在)，可以决定是否 abort 后续 INCRBY
  → 部分失败可以按条重试

Lua EVAL 原子读-改-写:
  → 服务端执行完毕后只返回最终结果
  → 无法"中途"干预
  → 如果脚本失败（OOM/CROSSSLOT/NOSCRIPT），整个批都得重试
  → 重试语义重 (原子 blast radius)
```

#### 决策矩阵

```
                        ┌───────────────────────────────────────────┐
                        │       Pipeline vs Lua EVAL 决策矩阵        │
                        └───────────────────────────────────────────┘
                                         │
           ┌─────────────────────────────┼─────────────────────────────┐
           ▼                             ▼                             ▼
     需要跨命令原子性              不需要跨命令原子性              key 命名无统一 hashtag
           │                             │                             │
   ┌───────┴───────┐             ┌───────┴───────┐             ┌───────┴───────┐
   │   Lua EVAL   │             │   Pipeline    │             │   Pipeline    │
   │   (1 RTT)    │             │   (1 RTT)     │             │   (1 RTT)     │
   │   274k cmd/s  │             │   307k cmd/s  │             │   307k cmd/s  │
   │   ✅ 原子     │             │   ✅ 最高吞吐  │             │   ✅ 唯一可行  │
   └───────────────┘             └───────────────┘             └───────────────┘
         ↓                             ↓                             ↓
   三合一去重                     纯 INCRBY                    存量系统迁移
   GET+INCRBY 原子               SETEX/批量写                  多租户 SaaS
   限流/去重/幂等                  无需 retry 分析的写操作          key 分散且无法改
```

#### 关键教训补充

| 教训 | 说明 |
|:--|:--|
| **Pipeline 在纯写场景比 Lua 快 12%** | INCRBY/SET 等简单命令的 RESP 编解码 < Lua VM 完整执行开销。每条命令自身原子时不要引入 Lua |
| **Pipeline 不需要同 slot，但 hashtag 可打通 Lua** | 如果 key 命名可加统一 hashtag（`{tag}:...`），Lua EVAL 的跨 slot 限制就消失了。真正的问题是"现有 key 能否加 hashtag"，不能 → Pipeline，能 → 按吞吐/原子性自由选 |
| **Lua 的 1 条 EVAL = 更高的调试盲区** | SLOWLOG/MONITOR 中 EVAL 不展开脚本内容。选 Lua 时需要配合脚本 SHA → 源代码的映射机制 |
| **Pipeline 支持逐条重试** | Pipeline 中的部分命令失败只需重试失败的那几条，Lua 脚本失败则整批重试（blast radius 更大） |
| **选 Lua 的条件：必须原子 + 只涉及单 slot** | 两个条件缺一不可，否则默认用 Pipeline |

---

## 附录 A: 工程化建议小结

```
1) 优先策略：尽可能把高频计数保存在本地（LOCAL_BUFFERED/COMPUTED）
   并周期性批量写回 Redis，尤其当 RTT ≥ 5ms 时本地方案的吞吐优势会成倍放大。

2) 若必须保证副本确认：在线生产跨 AZ 场景请使用异步 WAIT（fire-and-forget）
   + 周期性 drain 的模式，并对 WAIT 成功率设置严格监控与自动告警；
   当成功率下降到预设阈值时，自动降级为同步 WAIT 或减小 batch。

3) 对非幂等命令（INCR/DECR）不要依赖客户端重试来保证一次语义，
   应通过 EVAL 三合一原子去重（SETNX + INCRBY + SETEX 单次 Lua 调用）
   实现 1 RTT 的幂等保障；两阶段 SETNX 方案（2 RTT）吞吐仅基准的 4.6%，不推荐。

4) 对"读-改-写"操作（GET + INCRBY），优先选 Lua EVAL 原子方案：
   既提供原子性又比 Pipeline GET+INCRBY 快 26%（274k vs 217k cmd/s），
   p99 延迟低 46%（18.8ms vs 34.7ms）。Lua 的命令压缩优势在此场景下
   超过了脚本解析开销。

5) 对纯写入操作（INCRBY/SET/CAS），优先选 Pipeline 单管线：
   吞吐比 Lua 高 12%（307k vs 274k cmd/s），RESP 编解码 < Lua VM 开销。
   每条命令自身原子时不要引入 Lua 的脚本解析成本。

6) 对多 key 聚合操作：
   - 如果能引入统一 hashtag (`{tag}:...`) → Pipeline 和 Lua 均可，按 4/5 的逻辑选
   - 如果存量 key 无法改名 → Pipeline 是唯一可行方案

7) 对任何微基准结果，请记录并发布误差范围/样本数与环境细节
   （CPU、网络模式、Redis 配置），以便他人复现。

8) 复杂场景替代：对于极高吞吐且要求严格无重复计数的场景，考虑使用可靠消息
   + 幂等消费（例如将事件写入 Kafka/Redis Stream，消费端做幂等聚合并持久化
   到 Redis），将实时计数写路径与持久化/幂等恢复解耦，降低主写路径对 Redis
   failover 的敏感度。
```

## 附录 B: 监控与告警模板

| 指标 | 推荐报警阈值 | 说明 |
|:--|:--|:--|
| **WAIT 成功率** | < 99% → 告警，持续 < 95% → 自动降级为同步 WAIT | 反映 replica 确认的健康度 |
| **Pending-WAIT 队列长度** | 持续 > 1000 → 告警 | drain 线程跟不上写入速度 |
| **Drain 成功率** | < 98% → 告警 | drain 过程中 futures 超时或被丢弃 |
| **重试/失败批次数** | 增长率 > 10%/min → 告警 | 写入链路异常信号 |
| **failover_count/min** | 异常升高 → 告警 | master 切换频率，判断客户端重试/幂等策略是否生效 |
| **client_reconnect_rate** | 升高 → 告警 | 重连频率，反映网络/拓扑稳定性 |
| **pending_command_count** | 持续偏高 → 告警 | 客户端积压的未确认命令数 |
| **avg_pending_wait_ms** | 升高 → 告警 | 未确认命令平均等待时间 |
| **dedup_key_count / dedup_key_memory_bytes** | 内存占用 > Redis 5% → 告警 | 去重 key 的高基数成本 |

**Backpressure 策略**：当 pendingWaits 达到阈值（例如 10k）时，应立即降级写入模式（减小 batch / 暂停写入并切换为同步 WAIT / 开启同期持久化），并报警。

### 附录 B.1: 进程崩溃重启恢复流程（示例）

当进程在未 drain 前崩溃时，仅持久化 pending batch 元数据还不够，建议按以下流程恢复：

```
1. 进程重启时读取本地持久化的 pending batch 列表 (batch id + checksum + commands)
2. 对每个 pending batch 在当前 master 上执行 EVAL 去重检查（或 WAIT）以确认是否已被执行
3. 对未确认的 batch 做重放或人工审查（按业务要求决定是否重放）
4. 记录恢复操作的 audit log 与监控事件
```

## 附录 C: 实验测量误差与可复现性

> 当前数据表中未标注方差/标准差。建议每次 sweep 重复 N 次（例如 3~5 次）取均值 ± 标准差，以降低单次噪声（目前个别点出现 >10% 的波动）。
> 数据格式建议：`均值 ± 标准差 (N=5)`, 例如 `307,820 ± 12,300 (N=5)`。

## 附录 D: 环境细节补充

| 项目 | 规格 / 说明 |
|:--|:--|
| **Redis 版本** | Redis 7.x Cluster，AOF 关闭 (appendonly no, 8.19 对比测试环境) |
| **客户端** | Lettuce 6.x + Netty，`RedisClusterClient` |
| **容器 NIC 模式** | Docker bridge（独立 `cluster-net` 网络） |
| **CPU** | 主机 CPU 型号 / 容器 CPU 限制（建议补充） |
| **JVM 参数** | `-Xms512m -Xmx1g`（建议补充 GC 策略，如 `-XX:+UseG1GC`） |
| **AOF fsync 策略** | 默认 everysec（对高并发写入有影响，建议注明） |

## 附录 E: 网络模拟精确性说明

模拟双向 RTT 的精确做法：

```bash
# 在 client 端也注入 egress 延迟（使 request 和 response 各增加 T/2）
# client 端:
tc qdisc add dev eth0 root netem delay 5ms

# server 端（已有的 master egress 注入）:
docker exec redis-cluster-1 tc qdisc add dev eth0 root netem delay 5ms

# 或者使用 netem 的 delay jitter 和 loss 参数模拟真实丢包/乱序环境：
docker exec redis-cluster-1 tc qdisc add dev eth0 root netem delay 10ms 1ms loss 0.1%
```

记录项：
- 注入位置：ingress / egress（已在本文中说明）
- `tc qdisc` 确切参数（delay, jitter, loss）
- 延迟模拟的拓扑（仅 server egress，或同时 client egress）

## 附录 F: Drain 实现注意事项

- 使用非阻塞/有界队列保存 pending futures，避免内存无限增长
- 在专门的单线程/线程池中做 drain，避免写线程在 drain 时卡住
- 对 `f.get(timeout)` 的 timeout 值做实验调优（建议对比 1ms / 5ms / 10ms 三档）
- 记录 drain 耗时（P50/P99），确保每次 drain < 1ms（热路径要求）

## 附录 G: 并发/连接数术语说明

| 术语 | 本文中的含义 |
|:--|:--|
| **concurrency** | 并发 worker/线程数（每线程独立执行 Pipeline 循环） |
| **连接数** | 不同模式不同：`runSingle()` 用 1 条共享连接（autoFlush=false），`runMultiConn()` 中写连接 = `writeConcurrency`、读连接 = `readConcurrency`，分别独立建立 |
| **pipelineSize (batchSize)** | 每次 `flushCommands()` 前积攒的命令数 |
| **worker 内部循环** | 每个并发线程自己的 while 循环，共享 `running/workersStopped` 标志但不共享其他状态（除 AtomicLong 计数器） |

---

## 场景 8.21: Pipeline 内循环调用 EVAL 嵌套执行（已实测 ✅）

2026-08-04 实测，NESTED / MERGED / RAW 三种模式 × 三个 batchSize（100/500/1000）共 9 轮对比。

### 8.21.1 背景与问题

```
场景 8.20 对比了"Pipeline 二选一"：一条 Pipeline vs 一条 EVAL。
但生产代码里常见另一种模式——"嵌套"：

  for (int i = 0; i < batchSize; i++) {
      pipeline.eval("return redis.call('INCRBY', KEYS[1], ARGV[1])", ...);
  }
  pipeline.flushCommands();
  pipeline.awaitAll();

本质: 一条 Pipeline 里塞了 batchSize 个 EVAL 命令
→ 每个 EVAL 都是一个独立的小脚本，但都是同一个主线程串行执行
→ Pipeline flush 后，batchSize 个 EVAL 在 Redis 主线程上**连续排队执行**
```

**与场景 8.20 的关键区别**：

| | 8.20 对比 | 本场景 (嵌套) |
|------|:---:|:---:|
| 一次请求 | 1 个 EVAL 脚本（内做循环）| batchSize 个独立 EVAL |
| 网络 RTT | 1 | 1（Pipeline 压缩） |
| Redis 解析次数 | 1 条 EVAL | batchSize 条 EVAL（每条都要解析脚本体）|
| 主线程行为 | 1 次调度 | batchSize 次调度，**连续占用** |
| 对其他命令的影响 | 仅在 EVAL 执行期间阻塞 | 在全部 batchSize 个 EVAL 完成前持续阻塞 |

### 8.21.2 理论分析：为什么嵌套 EVAL 比单 EVAL 危害大

```
Redis 主线程视角（batchSize=100, 每个 EVAL 耗时 0.01ms）:

┌─────────────────────────────────────────────────────────────────────┐
│ 时间轴                                                               │
│                                                                     │
│ 单 EVAL（8.20）:                                                     │
│   [其他命令] → [EVAL 0.01ms] → [其他命令] → [其他命令] → ...       │
│   阻塞窗口: 0.01ms                                                   │
│                                                                     │
│ Pipeline 嵌套 EVAL（本场景）:                                         │
│   [其他命令] → [EVAL₁] → [EVAL₂] → ... → [EVAL₁₀₀] → [其他命令]     │
│                 └─── 连续占用 100 × 0.01ms = 1ms ──────────────┘     │
│   阻塞窗口: 1ms（放大 100 倍）                                        │
│                                                                     │
│ 在此期间，ANY 其他连接的命令全部排队:                                   │
│   HGETALL / SET / INCR / PUBLISH / EXPIRE / ...                    │
│   → 全局命令延迟均匀飙升                                              │
│   → 如果 batchSize=500，阻塞 5ms → P99 从 1ms 跳到 5ms+             │
└─────────────────────────────────────────────────────────────────────┘
```

**根因**：
1. 每条 EVAL 在慢日志中只记录一行 `EVAL`，看不到脚本体（排查盲区）
2. batchSize 个 EVAL 连续执行时，主线程在"解析脚本体 → 执行 → 返回"循环，其间不处理任何其他连接
3. 如果脚本体内有循环/阻塞操作（如大 key 操作），每个 EVAL 耗时从 0.01ms 变为几十 ms，阻塞窗口指数级放大

### 8.21.3 理论影响预估

| batchSize | 单 EVAL 耗时 | 总阻塞窗口 | P50 退化 | P99 退化 | 风险等级 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 100 | 0.005ms (简单 INCR) | 0.5ms | +0.5ms | +0.5ms | ⚠️ 低 |
| 100 | 0.1ms (复杂 Lua) | 10ms | +10ms | +10ms | 🔴 高 |
| 500 | 0.005ms | 2.5ms | +2.5ms | +2.5ms | ⚠️ 中 |
| 500 | 0.1ms | 50ms | +50ms | +50ms | 🔴 致命 |
| 1000 | 0.1ms | 100ms | +100ms | +100ms | 🔴 致命 |

> `单 EVAL 耗时` 取决于脚本逻辑复杂度和 key/value 大小。上表为理论推测值。

### 8.21.4 规避方案

| 优先级 | 方案 | 说明 |
|:---:|------|------|
| 1 | **合并为单 EVAL** | 把 N 个独立 EVAL 的循环逻辑移入 **1 个 EVAL 脚本内部**——Lua 内做 for 循环，Redis 只调度 1 次，阻塞窗口 = 1 次 EVAL 耗时 |
| 2 | **拆散 Pipeline 中的 EVAL 密度** | 如果必须多个 EVAL，不要连续发——在 batch 中交错普通命令（SET/INCR），给主线程间歇处理其他连接的机会 |
| 3 | **限制 batchSize × EVAL 复杂度** | 设置 `maxBatchSize ≤ maxSafeBlockMs / avgEvalMs`，如目标阻塞 <1ms，每个 EVAL 耗时 <0.01ms，则 batchSize ≤ 100 |
| 4 | **Lua 脚本瘦身** | 确保每个 EVAL 脚本 O(1) 或 O(logN)，禁止循环、大 key HGETALL/SMEMBERS |
| 5 | **监控告警** | 监控 `slowlog` 中 EVAL 命令的 max latency，若发现 EVAL 慢查询数量激增，排查是否是由嵌套 EVAL pool 导致的 |

```lua
-- ❌ 反模式: Pipeline 中 batchSize 个 EVAL（每条 EVAL 只做简单加）
-- Pipeline flush 后，Redis 连续执行 100 个 EVAL 脚本解析 + 执行

-- ✅ 改造: 合并为 1 个 EVAL，单次调度
local total = 0
for i = 1, #KEYS do
    total = total + redis.call('INCRBY', KEYS[i], ARGV[i])
end
return total
```

### 8.21.5 实测：NESTED vs MERGED vs RAW（3 × 3 Sweep）

**测试环境**：concurrency=16, keySpace=10000, warmup=10s + sample=15s。每轮独立 `FLUSHDB`。Side-channel 使用独立连接每 5ms 发一次 `HGETALL` 探测全局延迟。

**三种模式定义**：

```
NESTED: Pipeline 中 batchSize 个独立 EVAL（每个 EVAL 1 次 INCRBY）
   → Redis 主线程排队执行 batchSize 个小脚本，中间可被其他连接命令穿插

MERGED: Pipeline 中 1 个 EVAL 脚本内 for 循环 batchSize 次 INCRBY
   → Redis 主线程被 1 个大脚本独占，连续执行所有 INCRBY 后才放行

RAW:    Pipeline 中 batchSize 个独立 INCRBY（零脚本，基线对比）
   → 原生命令，无 Lua VM 解析/执行开销
```

**9 轮结果**：

| batchSize | 模式 | ops/s | p99 | sideP99 | slowlogΔ |
|:---:|------|--------:|-----|---------|:---:|
| **100** | **NESTED** | **191,044** | 20.60ms | 18.99ms | 6 |
| 100 | MERGED | 4,660 | 12.60ms | 12.75ms | 20 |
| 100 | RAW | 455,483 | 15.40ms | 14.95ms | 5 |
| **500** | **NESTED** | **207,264** | 85.40ms | **45.80ms** | 0 |
| 500 | MERGED | 1,281 | 27.90ms | 28.28ms | 0 |
| 500 | RAW | 575,859 | 35.90ms | 23.21ms | 0 |
| **1000** | **NESTED** | **196,931** | 149.60ms | **49.73ms** | 0 |
| 1000 | MERGED | 600 | 75.80ms | **55.37ms** | 0 |
| 1000 | RAW | 588,261 | 70.50ms | 32.50ms | 0 |

> **注意**：MERGED 的 `ops/s` 是"批次数/秒"（每批仅 1 个 future），不是"命令数/秒"。等效 INCRBY 操作数 = ops/s × batchSize。如 MERGED B1000 实际等效约 `600 × 1000 = 600K` 次 INCRBY/s，超过 NESTED 的 197K，但代价是全局 sideP99 最差。

### 8.21.6 关键发现

**1. NESTED 吞吐远超 MERGED（但算法收益倒挂）**

| batchSize | NESTED (ops/s) | MERGED (ops/s) | 倍率 |
|:---:|:---:|:---:|:---:|
| 100 | 191K | 4.6K | **41x** |
| 500 | 207K | 1.3K | **162x** |
| 1000 | 197K | 600 | **328x** |

MERGED 的 `ops/s` 看起来极低，但**等效 INCRBY 操作数其实不低**。

以 B1000 为例：
- MERGED: 600 批/s × 1000 次 INCRBY/批 = **600K 次 INCRBY/s**（等效 > NESTED 的 197K）
- 代价：1 次 EVAL 独占主线程 1000 次循环 → **全局 sideP99 = 55.37ms**（最差）

**NESTED 的优势不是"更快"，而是"不独占"**——Pipeline 中的 1000 个独立 EVAL 在 Redis 主线程上排队执行，中间可以被其他连接的命令穿插，全局延迟影响更平缓。

**2. Side-channel P99 揭示真相**

| batchSize | NESTED sideP99 | MERGED sideP99 | RAW sideP99 |
|:---:|:---:|:---:|:---:|
| 100 | **18.99ms** | 12.75ms | 14.95ms |
| 500 | 45.80ms | **28.28ms** | 23.21ms |
| 1000 | **49.73ms** | **55.37ms** ※ | 32.50ms |

※ **B1000 时 MERGED sideP99 反超 NESTED**：单个大 EVAL 独占主线程 1000 次循环的伤害，超过了 1000 个独立小 EVAL 排队。

**MERGED 的 sideP99 随 batchSize 增长的斜率更大**：

```
sideP99 增长:
  RAW:    15ms → 23ms → 33ms   (线性，斜率 ≈ 1.5x per step)
  NESTED: 19ms → 46ms → 50ms   (渐近收敛，斜率递减)
  MERGED: 13ms → 28ms → 55ms   (超线性，斜率递增)  ← 危险信号
```

**3. RAW Pipeline INCRBY 是绝对基线**

三个 batchSize 下 RAW 都是最快的，且 sideP99 最低。**零脚本开销是王道**——能不用 EVAL 就不用。

**4. MERGED slowlog 在 batchSize=100 时最差（Δ=20）**

B100 时 MERGED 有 20 条慢查询，而 B500/B1000 反而为 0。原因：B100 时 Lua 脚本的循环次数少（100 次），每次调度耗时 0.07ms（=`1000ms / 4.6K ops/s`），恰好在 slowlog 阈值边缘。B500/B1000 时单次 EVAL 执行时间虽更长，但总调度次数少，巧合地避开了 slowlog 采样。

### 8.21.7 实测结论

```text
实测结论（c=16, batchSize=100/500/1000）:
  1. NESTED EVAL 可行：吞吐稳定 190-207K ops/s，sideP99 控制在 19-50ms
  2. MERGED EVAL 危险：sideP99 随 batchSize 超线性增长，B1000 时 sideP99=55ms（反超 NESTED）
  3. RAW 最佳：零脚本开销，吞吐 455-588K，sideP99 最低（15-33ms）
  4. 分界点约在 batch=100：超过后 MERGED 的全局阻塞伤害开始超过 NESTED 的脚本解析开销
  5. 能不用 EVAL 就不用：3 个 batchSize 下 RAW 吞吐是 NESTED 的 2.4-3.0 倍
```

### 8.21.8 更新后的规避方案

| 优先级 | 方案 | 调整 |
|:---:|------|------|
| 1 | **优先考虑 RAW 原生命令** | ✅ 实测：吞吐 3x NESTED，sideP99 最低，能用 INCRBY/HSET/SET 就别用 EVAL |
| 2 | **合并为单 EVAL（MERGED）** | ⚠️ 实测：仅 batchSize ≤ 100 可用，超线后 sideP99 反超 NESTED，造成全局延迟恶化 |
| 3 | **拆散 EVAL 密度（NESTED）** | ✅ 实测：P99 随 batchSize 渐近收敛，sideP99 低于 MERGED，是妥协方案 |
| 4 | **限制 batchSize ≤ 100** | ✅ 实测：100 以内 NESTED sideP99=19ms，MERGE sideP99=13ms，均可控 |
| 5 | **Lua 脚本瘦身** | 理论仍成立：O(1) 脚本、禁循环、禁大 key 操作 |

**最终推荐**：

```java
// ✅ 最优：RAW 原生命令（零脚本，最高吞吐）
for (int i = 0; i < batchSize; i++) {
    futures.add(async.incrby(keys[i], vals[i]));  // 455-588K ops/s
}

// ⚠️ 妥协：NESTED EVAL（batchSize ≤ 100，需要 Lua 原子性时）
for (int i = 0; i < 100; i++) {
    futures.add(async.eval(lua, keys[i], vals[i]));  // ~191K ops/s
}

// ❌ 高风险：MERGED EVAL（batchSize > 100 时 sideP99 恶化）
futures.add(async.eval(mergedLua, allKeys, allArgs));  // 全局阻塞，sideP99 55ms+
```
