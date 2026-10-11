---
title: "场景 2 — 大结构 Hash 批量写入"
summary: "多 worker 并发写入大量大 Hash（每 key 10 field，每 field ~256B），key 总量 500k，TTL=600s，Redis 配置 maxmemor…"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - Redis
category: deep
series: "redis"
slug: "场景2-大结构Hash批量写入"
reading_time: 65
featured: false
draft: false
---
# 场景 2 — 大结构 Hash 批量写入

## TL;DR

多 worker 并发写入大量大 Hash（每 key 10 field，每 field ~256B），key 总量 500k，TTL=600s，Redis 配置 maxmemory=256MB，policy=allkeys-lru。对比无持久化、RDB、AOF（percentage=100/500）四种模式下内存峰值、COW 尺寸、淘汰量和 OOM 风险。

> **关键结论**：RDB fork 在高写入且未预热情况下产生的 COW 可非常巨大（本测 ~245MB，超大 Value 场景冷启动可达 ~1.9GB），可能导致 OOM。AOF percentage=100 在本测试下虽然 rewrite 频繁但单次 COW 最小（~66MB），峰值内存压力最小。若要根除 COW 风险，最佳做法是**主节点关闭持久化 + 副本做持久化/备份**，并在副本上错峰做 BGSAVE / AOF rewrite。
> 
> **风险估算实用公式**：估算所需物理内存 ≈ `used_memory_rss` + 预计 `last_cow_size` + 安全裕度（建议 10~20% 或按 `used_memory_rss × 1.5~2` 做保守规划）。注意 `last_cow_size` 为历史观测值，未来 fork 的 COW 取决于实时写入速率、fork 持续时间以及是否开启 THP。

---

## 压测架构

```
Worker-1 ── Redis 连接 1 ── HSET hash:N f0..f9 + EXPIRE── flushCommands ── awaitAll ── 循环
Worker-2 ── Redis 连接 2 ── HSET hash:N f0..f9 + EXPIRE── flushCommands ── awaitAll ── 循环
...
Worker-N ── Redis 连接 N ── HSET hash:N f0..f9 + EXPIRE── flushCommands ── awaitAll ── 循环
```

> 注：本文使用 `HSET` 命令（Redis 4.0+ 已可替代 `HMSET` 写入多个 field；`HMSET` 在较新版本中已被标记为 deprecated，仅为历史参考）。

- 每个 Hash key 10 个 field，每个 field value 256 字节
- Hash key 空间 50 万，TTL 600 秒
- Redis 配置 `--maxmemory 256mb --maxmemory-policy allkeys-lru`

---

## 基线测试：无持久化

15 秒立即打满内存：

| 指标           | 值                |
|:------------ |:---------------- |
| 写入量          | 8,176,000 fields |
| 吞吐           | 545k fields/s    |
| 内存           | 256MB / 256MB    |
| RSS          | 268MB            |
| evicted_keys | 641,390          |
| 错误           | 0                |

> 15 秒即触发 LRU 淘汰，RSS 略超 maxmemory（Hash 结构碎片）。

---

## RDB fork 风险测试

配置 `--save "60 1"`（每 60 秒至少 1 个变更触发 BGSAVE）。

65 秒对比：

| 指标                    | 无持久化       | RDB (save 60 1)              |
|:--------------------- |:---------- |:---------------------------- |
| 65s 写入                | —          | 71,968 批 (35,984,000 fields) |
| RSS 峰值                | 268MB      | **275MB**                    |
| fork 次数               | 0          | **1**                        |
| **rdb_last_cow_size** | 0          | **~245MB**                   |
| BGSAVE 耗时             | —          | ~2s                          |
| evicted_keys          | 641k (15s) | **3,038k**                   |
| 丢数据风险                 | 全部         | 最多 60s                       |

**结论**：

- **COW 精确定义**：`rdb_last_cow_size ≈ 245MB` 表示"在上一次 BGSAVE 子进程存活期间，**父进程的写操作**导致被 copy-on-write 复制的内存总字节数（即父进程触发的 COW 字节数）"。注意：这是**历史的、已发生**的一次 fork 的 COW 度量值，不是未来 fork 的预测值。COW 内存由父进程承担，因此 fork + COW 显著放大了实际内存占用。

- **fork 阻塞 vs 子进程快照的明确区分**：`fork()` 调用的那一刻父进程会产生短暂内核开销（通常毫秒级停顿，取决于页表大小），之后子进程在后台执行快照，**不会阻塞**父进程的正常命令处理。但父进程在子进程存活期间的写入会触发 COW，导致内存被复制并推高 RSS，从而间接导致更频繁的淘汰和延迟上升。本测试观测到的 ~2s 是子进程执行快照的耗时，不等于父进程被阻塞 2s。

- **物理内存估算（历史 vs 预测）**：`last_cow_size` 是上一次 fork 的实际观测值，未来 fork 的 COW 取决于"当前写入速率 × 预计 fork 存活时间 × 页粒度（是否开启 THP/大页）"。因此用于规划应采用：
  
  ```
  估算所需物理内存 ≈ used_memory_rss + 预计_last_cow_size + 安全裕度(10~20%)
  ```
  
  若无法精确估算 `预计_last_cow_size`，采用保守策略：
  
  ```
  物理内存 ≥ used_memory_rss × 1.5~2
  ```
  
  示例：275MB + 245MB ≈ **520MB**，再 + 10~20% 裕度 ≈ **572~624MB**；若宿主机仅 512MB，OOM 很可能发生。保守估算法：275MB × 2 = **550MB**。

- RDB 开启后淘汰量暴增 4.7 倍（fork/COW 增加了写入路径的内存压力，allkeys-lru 更激进地淘汰以维持在 maxmemory 以内）。

---

## AOF 对比：percentage 100 vs 500

配置 `--appendonly yes --appendfsync everysec`。

> **appendfsync everysec 的语义**：Redis 每秒（由后台线程）将 AOF 缓冲区 fsync 到磁盘一次。最坏情况下（服务器崩溃），最多丢失最近 ~1 秒的写入（取决于 OS 调度与 flush 实际行为）。若需要更强持久化可用 `appendfsync always`（每条命令 fsync，严重影响性能）或在写入时使用 `WAIT` 命令等待 N 个副本确认写入。

### AOF percentage=100（文件翻倍即 rewrite）

| 指标           | 值                            |
|:------------ |:---------------------------- |
| 65s 写入       | 47,742 批 (23,871,000 fields) |
| RSS 峰值       | **287MB**                    |
| fork 次数      | **17 次**                     |
| 单次 COW       | **~66MB**                    |
| 总 COW（累计）    | ~1,122MB                     |
| evicted_keys | 1,989k                       |
| AOF 文件大小     | ~185MB                       |
| rewrite 单次耗时 | ~2s                          |

### AOF percentage=500（文件增长到 5 倍才 rewrite）

| 指标           | 值                            |
|:------------ |:---------------------------- |
| 65s 写入       | 48,083 批 (24,041,500 fields) |
| RSS 峰值       | **282MB**                    |
| fork 次数      | **5 次**                      |
| 单次 COW       | **~264MB**                   |
| 总 COW（累计）    | ~1,320MB                     |
| evicted_keys | 2,005k                       |
| AOF 文件大小     | **~735MB**                   |
| rewrite 单次耗时 | ~3s                          |

---

## 四模式终局对比

| 指标         | 无持久化  | RDB (60 1) | AOF 100%            | AOF 500%        |
|:---------- |:----- |:---------- |:------------------- |:--------------- |
| 65s 写入     | —     | 71,968 批   | 47,742 批            | 48,083 批        |
| **RSS 峰值** | 268MB | **275MB**  | 287MB               | 282MB           |
| fork 次数    | 0     | **1**      | 17                  | 5               |
| **单次 COW** | —     | ~245MB     | **~66MB**           | ~264MB          |
| 总 COW（累计）  | 0     | **~245MB** | ~1,122MB            | ~1,320MB        |
| evicted    | 641k  | 3,038k     | 1,990k              | 2,005k          |
| 丢数据风险      | 全部    | ≤60s       | **≤~1s** (everysec) | ≤~1s (everysec) |
| 磁盘文件       | 0     | dump.rdb   | ~185MB              | ~735MB          |

> 注：所有 "MB" 值均为近似值（来源于 INFO 输出或系统 RSS 换算后的四舍五入），如需严格计量请以 bytes 原始值为准。

---

## 超大 Value 管道写入压测

### TL;DR

将单个 key 的 value 从 256B 放至 **1KB / 10KB / 50KB / 100KB**，以 Pipeline SETEX 持续写入（keySpace=100k、TTL=120s），对比**无 RDB 与 RDB (save 60 1)** 两种模式下的**网络带宽、延迟尖刺、COW 放大率**。

> **关键结论**：带宽甜点在 **10KB**（490 MB/s，**本地回环测试值**），100KB 时 P99 从 5.6ms 飙升至 **122ms**（无 RDB）→ **176ms**（最大值），TCP 缓冲区压力明显。RDB 模式下，首次 BGSAVE 的 COW 最高可达 **1928MB**（即使数据仅 132MB），BGSAVE 耗时和写入强度共同决定 COW 规模。
> 
> ⚠️ **注意**：本文所有 MB/s 数值均为**本地回环（localhost）测试**结果，不代表 1Gbps/10Gbps 真实网卡可承受的吞吐。经过真实网卡后，千兆网卡理论限速约 125 MB/s，万兆约 1.25 GB/s。

### 压测架构

```
Worker-1 ── Redis 连接 1 ── [SETEX lv:0..99999 × batchSize] ── flushCommands ── awaitAll
Worker-2 ── Redis 连接 2 ── [SETEX lv:0..99999 × batchSize] ── flushCommands ── awaitAll
Worker-3 ── Redis 连接 3 ── [SETEX lv:0..99999 × batchSize] ── flushCommands ── awaitAll
Worker-4 ── Redis 连接 4 ── [SETEX lv:0..99999 × batchSize] ── flushCommands ── awaitAll
```

- 每条 SETEX = key `lv:N` + 随机 value（`1KB / 10KB / 50KB / 100KB`）+ TTL 120s
- Key 空间 10 万，反压 maxInflightFlushes=4，Pipeline timeout=5s
- 每梯度测试 25 秒（不含 5 秒预热），每 5 秒采样一次

### 无 RDB 模式：梯度带宽压测

> ⚠️ 以下 MB/s 为 **本地回环（localhost）** 测试值。经过真实网卡后，千兆网卡理论上限约 125 MB/s。

| 尺寸    | batch | conc | ops/s | MB/s (localhost) | avg(ms) | P50  | P90  | P99     | maxP99  | 内存    |
|:----- |:----- |:---- |:----- |:---------------- |:------- |:---- |:---- |:------- |:------- |:----- |
| 1KB   | 50    | 4    | 2524  | 123.2            | 1.6     | 1.0  | 2.2  | 7.2     | 9       | 132M  |
| 10KB  | 20    | 4    | 2441  | **476.8**        | 1.7     | 1.2  | 2.4  | 5.6     | 7       | 1.15G |
| 50KB  | 10    | 4    | 503   | 245.8            | 8.2     | 4.8  | 9.4  | 21      | 23      | 4.24G |
| 100KB | 10    | 4    | 216   | 211.1            | 19.6    | 16.6 | 33.6 | **122** | **176** | 4.78G |

**核心发现**：

1. **带宽峰值在 10KB**（476.8 MB/s），随后递减。大 value 的批量 Pipeline flush 开销越来越大，TCP 层无法持续维持高吞吐。
2. **P99 在 100KB 爆炸**：5.6ms（10KB）→ 21ms（50KB）→ **122ms**（100KB）。TCP 发送缓冲区被 10 × 100KB = 1MB 的 Pipeline 批次撑满，awaitAll 长时间阻塞。
3. **内存线性增长**：key 空间 10 万 × value 大小 ≈ 内存（加上少量 Set 元数据开销、碎片）。
4. **延迟 vs 吞吐权衡**：当目标 MB/s 最大化时，应选 10KB 左右 value；若对 P99 < 10ms 有强要求，value 不应超过 10KB。

### RDB 模式：fork COW 放大率

> ⚠️ 以下 MB/s 为 **本地回环（localhost）** 测试值。

| 尺寸    | batch | conc | ops/s | MB/s (localhost) | avg(ms) | maxP99 | COW(last)  | BGSAVE | saves | 内存    |
|:----- |:----- |:---- |:----- |:---------------- |:------- |:------ |:---------- |:------ |:----- |:----- |
| 1KB   | 50    | 4    | 2347  | 114.6            | 1.7     | 9      | **1928MB** | 25s    | 1     | 132M  |
| 10KB  | 20    | 4    | 2511  | **490.4**        | 1.6     | 7      | 9MB        | 3s     | 3     | 1.15G |
| 50KB  | 10    | 4    | 604   | 294.9            | 8.4     | 28     | 626MB      | 27s    | 4     | 4.24G |
| 100KB | 10    | 4    | 231   | 225.6            | 21.7    | 91     | 220MB      | 35s    | 5     | 4.78G |

**核心发现**：

1. **首次 BGSAVE COW 最大**：1KB 场景仅 132MB 数据，首次 BGSAVE COW 达 1928MB——原因是首次 BGSAVE 时 OS page cache 完全冷启动，25 秒快照期间父进程的每页写入都触发 COW。
2. **热数据 BGSAVE 快、COW 小**：10KB 场景 rdb_saves=3（已预热），最后一次 COW 仅 9MB / 3s。热页缓存大幅降低 BGSAVE 耗时和 COW。
3. **50KB/100KB 大 value 的 BGSAVE 依然昂贵**：50KB COW=626MB（27s），100KB COW=220MB/次 × 5 次 = 累计超 1GB。
4. **写入吞吐 RDB 影响有限**：RDB 和无 RDB 的 ops/s 差异在 5% 内，因为 Pipeline 写入相比 BGSAVE 子进程的 CPU 争抢有限。网卡先到瓶颈。
5. **100KB P99 反而改善**：RDB 模式 maxP99=91ms 低于无 RDB 的 176ms——因为 BGSAVE 将数据刷入磁盘，减少了内存压力。

### RDB COW 风险估算（超大 Value 版）

与场景2原始公式一致：

```
近似所需物理内存 ≈ used_memory_rss + last_cow_size（+ 安全裕度）
```

| 场景              | used_memory_rss | last_cow_size | 所需 ≈      | 示例宿主机 | OOM 风险 |
|:--------------- |:--------------- |:------------- |:--------- |:----- |:------ |
| 1KB 冷启动 BGSAVE  | 157M            | 1928M         | **~2.1G** | 2G    | **高危** |
| 10KB 热 BGSAVE   | 1.18G           | 9M            | ~1.2G     | 2G    | 安全     |
| 50KB            | 4.24G           | 626M          | **~4.9G** | 6G    | 注意     |
| 100KB × 5 saves | 4.78G           | 220M          | ~5.0G     | 6G    | 注意     |

> **关键**：即使内存数据仅 132MB，冷启动 BGSAVE 的 COW 也可达到 1.9GB。**仅看 `used_memory` 做规划会造成严重低估。**

### 超大 Value 的网卡与 TCP 风险

> ⚠️ 以下 MB/s 峰值为 **本地回环（localhost）测试值**。真实网络环境下，千兆网卡理论限速约 125 MB/s，万兆约 1.25 GB/s。实际吞吐受网络延迟、丢包、TCP 窗口等影响会更低。

| 指标          | 1KB  | 10KB  | 50KB  | 100KB     | 警戒线                         |
|:----------- |:---- |:----- |:----- |:--------- |:--------------------------- |
| MB/s 峰值     | 123  | 477   | 245   | 211       | >400 MB/s（千兆网卡 ~125MB/s 理论） |
| P99 延迟      | 7ms  | 6ms   | 21ms  | **122ms** | >50ms 预警                    |
| 单批大小（flush） | 50KB | 200KB | 500KB | **1MB**   | >256KB 风险                   |
| TCP 发送缓冲压满  | 不明显  | 不明显   | 略感    | **明显**    | —                           |

**100KB value 时**：单 Pipeline flush = 10 × 100KB = 1MB，4 个并发 flush = 4MB 同时在 TCP 发送缓冲中。本地回环（localhost）虽无真实网络带宽限制，但 TCP 缓冲区仍有上限，导致 `awaitAll` 长时间阻塞（P99=122ms）。

### 生产建议（超大 Value 场景）

1. **value 不超过 10KB**：超过此阈值 P99 显著上升，MB/s 反降。
2. **cold-start RDB 极度危险**：首次 BGSAVE COW 可达数据量的 10~15 倍。需在低峰触发或在刚重启后预热数据再开 BGSAVE。
3. **主节点关闭持久化**：超大 value 意味着每次 BGSAVE 都会产生巨大 COW。与原始场景 2 建议一致——**主节点关持久化，副本做备份**。
4. **网络规划**：如果走真实网络而非 localhost，1GB 网卡仅支持 ~125 MB/s，超大 value 的压力首先要过网卡带宽关。
5. **Pipeline 批次大小**：大 value 时减小 batch 大小（如 100KB → batch=5），避免单 flush 过大撑满 TCP 缓冲区。

### 压测现象与根因分析

#### 现象一：带宽在 10KB 达到峰值（477 MB/s），随后大幅衰减

| 尺寸       | ops/s | MB/s      | 衰减   |
|:-------- |:----- |:--------- |:---- |
| 1KB      | 2524  | 123.2     | —    |
| **10KB** | 2441  | **476.8** | 峰值   |
| 50KB     | 503   | 245.8     | -48% |
| 100KB    | 216   | 211.1     | -56% |

**根因：**

**(1) 1KB → 10KB 带宽翻 3.9 倍 — 小 value 协议开销占比大**

ops 几乎不变（2524 → 2441），但每次 Pipeline batch 的有效载荷从 `50 × 1KB = 50KB` 升至 `20 × 10KB = 200KB`。小 value 时系统调用和 RESP 协议编解码的**固定开销占比较大**，数据本身"塞不满管道"。10KB 正好是**协议层开销占比最小**的甜点。

**(2) 10KB → 50KB/100KB 带宽不升反降 — TCP 层反压机制**

单 Pipeline flush 大小：

- 10KB × 20 = 200KB — 安全
- 50KB × 10 = 500KB — 危险
- 100KB × 10 = **1MB** — 灾难

4 并发 flush → 4MB 同时在 TCP 发送缓冲区中。Linux localhost 的 `tcp_wmem` 默认最大值约 **4MB**。缓冲区被撑满后，`write()` 系统调用阻塞等待，后续 `awaitAll` 空转：

```
Client (4 workers)          TCP send buffer (max ~4MB)      Redis Server
    │                              │                           │
    ├─ flush 1MB ──────────────────►│  snd_buf=1MB/4MB          │
    ├─ flush 1MB ──────────────────►│  snd_buf=2MB/4MB          │
    ├─ flush 1MB ──────────────────►│  snd_buf=3MB/4MB          │
    ├─ flush 1MB ──────────────────►│  snd_buf=4MB/4MB ← 满！    │ 正在解析上一条 100KB 命令
    │  ◄── write() 阻塞 ────────────│                           │
    │  awaitAll 等待中...           │                           │ Redis 单线程，还在处理
    │                               │ ◄── ack ────────────────── │ 命令处理完成
    │  ◄── awaitAll 返回 (122ms) ───│                           │
```

Redis 是单线程处理命令的。前面 100KB 命令还没处理完，后面的命令只能在 Redis 输入缓冲区排队。输入缓冲区也有上限（`client-output-buffer-limit`），超限会断开连接。

---

#### 现象二：P99 在 100KB 爆炸（5.6ms → 122ms，增长 22 倍）

| 尺寸    | avg    | P50    | P99       | maxP99    | 放大倍数 |
|:----- |:------ |:------ |:--------- |:--------- |:---- |
| 10KB  | 1.7ms  | 1.2ms  | **5.6ms** | 7ms       | 基准   |
| 50KB  | 8.2ms  | 4.8ms  | 21ms      | 23ms      | 3.8× |
| 100KB | 19.6ms | 16.6ms | **122ms** | **176ms** | 22×  |

**根因：Pipeline awaitAll 的反压放大效应**

P99 非等比增长，而是在 100KB 处**突变**。原因是中间剩余几条流控缓冲区全部被填满：

- **P50 = 16.6ms**：在"普通情况"下，管道内的命令可在一个 RTT 内完成处理
- **P99 = 122ms**：在"不幸情况"下，4 个 worker 的 flush 恰好**同时到达**，TCP buffer 瞬间满载，形成**同步阻塞风暴**
- **maxP99 = 176ms**：是所有采样周期中 P99 的**最大值**，捕获了最坏时刻

`awaitAll` 必须等待所有在途命令的**完整 RESP 响应**。当 TCP 缓冲区满载 + Redis 单线程还在处理上一条大命令时，awaitAll 的空等时长就是 P99。

---

#### 现象三：冷启动 RDB COW 高达 1928MB（数据仅 132MB → COW 15 倍放大）

| 尺寸          | 数据内存  | last_cow   | BGSAVE耗时 | saves | 放大倍数      |
|:----------- |:----- |:---------- |:-------- |:----- |:--------- |
| **1KB 冷启动** | 132M  | **1928MB** | 25s      | 1     | **14.6×** |
| 10KB 预热后    | 1.15G | 9MB        | 3s       | 3     | <0.01×    |

**根因有三层叠加：**

**(1) jemalloc 内存扫描范围 > used_memory**

`used_memory = 132MB` 只是 KV 数据，但 jemalloc 分配的**虚拟地址空间**可能 > 200MB（含元数据、碎片、空闲 chunk）。BGSAVE 子进程需要扫描 Redis 的整个地址空间。

**(2) COW 的物理粒度是"页"，写 1 字节复制整页**

```
父进程写一次 SETEX（1KB value）:
  ├─ 修改 dictEntry 指针     → 1 个 4KB 页 COW
  ├─ 修改 key 的 sds          → 1 个 4KB 页 COW  
  ├─ 修改 value 的 sds        → 1 个 4KB 页 COW
  └─ 修改 expires dict        → 1 个 4KB 页 COW
───────────────────────────────────────────
  一次 SETEX 最多 COW 4 × 4KB = 16KB
```

2347 ops/s × 25 秒 BGSAVE × ~4 页/op × 4KB ≈ 938MB 直接 COW。

但 1928MB 远大于此，原因是下一层。

**(3) Transparent Huge Pages (THP) — COW 放大的隐藏元凶**

如果系统开启了 THP，COW 单位从 **4KB** 膨胀为 **2MB（大页）**：

```
写 1KB value，无 THP → 复制 ~4 个 4KB 页 = 16KB
写 1KB value，有 THP → 复制 ~2 个 2MB 页 = 4MB  ← 250× 放大！
```

这是 1928MB 远超 938MB 的最可能原因。生产环境 Redis **必须关闭 THP**，这是 Redis 官方文档反复强调的。

**(4) 关键洞察 — 冷页 vs 热页**

冷启动时，OS page cache 完全没有缓存 Redis 的内存页。BGSAVE 子进程需要从头读取整个地址空间（25s）。在此期间，父进程的**每一次写入**都在修改"干净"页，触发 COW。

预热后（saves=3），前两次 BGSAVE 已经把数据"写脏"，第三次 BGSAVE 时大部分页已经是父进程独占的，不在与子进程共享，COW 骤降至 9MB。

```
冷启动 COW = 父进程写入速率 × BGSAVE 存活时间 × 页粒度 × THP 放大系数
1928MB    ≈ 2347 ops/s × 25s × 4页 × 4KB × (THP 放大 ~10×)
```

---

#### 现象四：RDB 模式下 100KB 的 maxP99 反而"改善"（91ms vs 176ms 无 RDB）

| 模式          | avg(ms) | maxP99(ms) |
|:----------- |:------- |:---------- |
| 无 RDB 100KB | 19.6    | **176ms**  |
| RDB 100KB   | 21.7    | **91ms**   |

看似违反直觉，实际上：

- 两个测试是**独立跑**的，176ms 是无 RDB 测试中的一次性极端离群值（恰好 4 worker 同时 flush 触发同步阻塞风暴）
- RDB 模式下 BGSAVE 子进程占用 CPU/磁盘 IO，**间接限制了 Pipeline 写入峰值速率**，不容易触发同样恶劣的同步条件
- avg 延迟相近（19.6ms vs 21.7ms），说明吞吐层面影响不大，差异来自统计采样

**结论**：这不是 RDB 改善了延迟，而是"有 RDB"恰好没有触发同样恶劣的同步条件。不应解读为 RDB 有助于降低延迟。

---

#### 现象五：50KB/100KB 的 BGSAVE COW 持续昂贵

| 尺寸    | 数据量   | last_cow | saves | BGSAVE耗时 |
|:----- |:----- |:-------- |:----- |:-------- |
| 1KB 冷 | 132M  | 1928MB   | 1     | 25s      |
| 50KB  | 4.24G | 626MB    | 4     | 27s      |
| 100KB | 4.78G | 220MB    | 5     | 35s      |

**50KB 场景 COW 高达 626MB**：数据量大（4.24GB），BGSAVE 需要扫描大量页面（27 秒），父进程 604 ops/s 持续写入触发大量 COW。对于非常大的 value（>32KB），jemalloc 可能走 mmap 路径（取决于配置），这会改变页分布与 COW 行为——mmap 分配的页面与 sbrk 分配的页在内核 COW 处理上有差异，评估时需要关注 allocator 的具体行为。

**100KB 场景 COW 仅 220MB/次**，比 50KB 小：因为父进程写入速率更低（231 ops/s vs 604 ops/s），BGSAVE 期间产生的脏页更少。但 BGSAVE 更频繁（5 次），累计 COW 超 1GB；且 BGSAVE 耗时最长（35s），因为大 RDB 文件写入磁盘慢。

**正反馈循环**：写入越密集 → BGSAVE 越频繁 → 单次耗时越长 → COW 越大 → 内存压力增加 → 淘汰更多 key → 写入更多 → BGSAVE 再次触发。

---

### 根除方案

按现象逐一给出消除方案，每个方案标注**消除层级**（根除 > 大幅缓解 > 规避）。

#### 方案 A：拆大 value 为小 value（根除带宽衰减 + P99 爆炸）

```
❌ 当前：SETEX lv:0  {100KB value}       → 单命令 100KB
✅ 改进：HSET  lv:0  chunk0 → 10KB, chunk1 → 10KB, ..., chunk9 → 10KB
        或  LPUSH lv:0  {10KB} ... {10KB}（10次）
```

- 把 100KB 拆成 10 个 10KB 片段，存在 **Hash** 或 **List** 中
- 每批 flush 降至 200KB，**根除 TCP 缓冲阻塞**
- 代价：读取时需 `HGETALL` 再拼接，可用 Lua 脚本保证多 chunk 写入原子性

**实现注意事项**：

- **写入原子性**：多 chunk 写入需要通过 Lua 脚本打包为单条命令执行，避免部分 chunk 写入成功、部分失败：
  
  ```lua
  -- Lua 脚本：原子写入拆分后的 chunks
  for i, chunk in ipairs(KEYS) do
      redis.call('HSET', KEYS[1], 'chunk' .. i, ARGV[i])
  end
  redis.call('EXPIRE', KEYS[1], ARGV[#ARGV])  -- 设置 TTL
  ```
- **读取端**：按顺序 `HGETALL` 或 `HMGET chunk0 chunk1 ... chunk9` 后拼接
- **热点风险**：拆分后单个 key 的读写频率不变，不会引入额外热点。但注意若原 key 已是热点（>10k ops/s），拆分不减总负载，需配合 Redis Cluster 分片
- **存储开销**：Hash 结构的每个 field 有少量额外元数据（~32B/field），10 chunk × 32B ≈ 320B 额外开销，相比 100KB 总大小可忽略

#### 方案 B：控制 Pipeline 并发度（大幅缓解 P99 爆炸）

不改数据结构的流控方案：

```
batchSize     = 2     // 从 10 降至 2，单 flush = 200KB
maxInflight   = 2     // 从 4 降至 2，同时在途 ≤ 400KB
pipelineTimeout = 1s  // 从 5s 降至 1s，更快感知阻塞
```

| 调整项        | 调整前   | 调整后    | 效果          |
|:---------- |:----- |:------ |:----------- |
| 单 flush 大小 | 1MB   | 200KB  | TCP 缓冲区不会满载 |
| 在途数据       | 4MB   | 400KB  | 同步阻塞风暴概率→0  |
| P99（预期）    | 122ms | < 20ms | 延迟可控        |
| MB/s（预期）   | 211   | ~180   | 略降但可接受      |

#### 方案 C：Unix Domain Socket（根除 TCP 栈开销）

同机部署时将 localhost TCP 连接改成 UDS：

```yaml
# application.yml
spring:
  redis:
    host: /var/run/redis/redis.sock  # 跳过 TCP/IP 协议栈
```

- **消除 TCP 握手、拥塞控制、缓冲区限制的开销**
- localhost TCP 虽不走真实网卡，但协议栈的缓冲、重传、Nagle 算法仍在生效
- UDS 是纯内存拷贝，吞吐和延迟均优于 TCP localhost

#### 方案 D：主节点关闭持久化（根除 COW）

```conf
# redis.conf — 主节点
save ""                    # 关闭 RDB
appendonly no              # 关闭 AOF

# redis.conf — 副本节点
save "900 1"               # 只在副本做 BGSAVE
appendonly yes             # 副本持久化
```

**效果**：主节点永不 fork，COW = 0。这是唯一真正的 COW 根除。

**⚠️ 数据一致性风险**：主节点关闭持久化后，若主节点崩溃且副本尚未完全同步最近写入，这些写入将永久丢失。缓解措施：

- 使用至少 **2 个持久化副本**，分散风险
- 对关键写入使用 `WAIT n timeout` 命令，确认至少被 n 个副本确认后才返回
- 确保故障转移策略不会将无持久化的主降级为持久化副本而丢失数据（HA 设计注意点）
- `WAIT` 有性能代价：每次写入增加一个同步等待，取决于网络 RTT

#### 方案 E：关闭 Transparent Huge Pages（根除页放大）

**THP 是 COW 放大的隐藏元凶，生产环境 Redis 必须关闭。**

**关闭步骤（所有 Redis 节点需执行）：**

```bash
# 1. 检查当前 THP 状态
cat /sys/kernel/mm/transparent_hugepage/enabled
# 输出应为 [always] madvise never 或 [madvise] always never
# 目标：始终显示 [never]

# 2. 临时关闭（立即生效，重启失效）
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag

# 3. 永久关闭 — 方式 A：systemd（推荐）
# 创建 /etc/systemd/system/disable-thp.service：
# [Unit]
# Description=Disable Transparent Huge Pages
# After=sysinit.target
# [Service]
# Type=oneshot
# ExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled && echo never > /sys/kernel/mm/transparent_hugepage/defrag'
# [Install]
# WantedBy=multi-user.target

# 4. 永久关闭 — 方式 B：GRUB（重启生效）
# 在 /etc/default/grub 的 GRUB_CMDLINE_LINUX 中添加：
# transparent_hugepage=never
# 然后执行 update-grub 并重启

# 5. 重启后确认
cat /sys/kernel/mm/transparent_hugepage/enabled
# 必须显示：[always] madvise [never]（方括号在 never 上）
```

**为什么必须关 THP：**

```
有 THP：写 1 字节 → 可能触发 2MB 大页 COW  (500× 放大)
无 THP：写 1 字节 → 触发 4KB 普通页 COW    (正常)
```

本测试中 132MB 数据产生 1928MB COW，THP 是核心放大因子之一。Redis 官方文档反复强调必须在所有生产节点上永久禁用 THP。

#### 方案 F：预热 BGSAVE（大幅缓解冷启动 COW 炸弹）

如果必须保留主节点 RDB，在写入高峰来临前先触发低峰 BGSAVE：

**安全运维流程：**

```bash
#!/bin/bash
# 步骤 1：检查当前是否已有 BGSAVE 在运行
if redis-cli INFO persistence | grep -q "rdb_bgsave_in_progress:1"; then
    echo "已有 BGSAVE 运行中，跳过"
    exit 0
fi

# 步骤 2：检查当前写入速率是否足够低（< 1000 ops/s 视为低峰）
OPS=$(redis-cli INFO stats | grep instantaneous_ops_per_sec | cut -d: -f2 | tr -d '\r')
if [ "$OPS" -gt 1000 ]; then
    echo "当前 ops/s=$OPS，不处于低峰期，拒绝执行"
    exit 1
fi

# 步骤 3：触发 BGSAVE
redis-cli BGSAVE
echo "BGSAVE 已触发，等待完成..."

# 步骤 4：轮询等待 BGSAVE 完成
while redis-cli INFO persistence | grep -q "rdb_bgsave_in_progress:1"; do
    sleep 1
done

# 步骤 5：记录本次 COW 与耗时（用于后续容量规划）
redis-cli INFO persistence | grep "rdb_last_cow_size"
redis-cli INFO stats | grep "rdb_last_bgsave_time_sec"
echo "预热 BGSAVE 完成，COW 已记录"

# 或用 cron 每天凌晨 4 点触发
# 0 4 * * * /path/to/bgsave_warmup.sh >> /var/log/redis_warmup.log 2>&1
```

原理：首次 BGSAVE 做"脏活"——把冷页面标记为 dirty（与子进程解耦），后续 BGSAVE 只需处理新写入的页面，COW 可降 90%+。

#### 方案 G：凑够物理内存（规避 OOM）

当 COW 无法根除时，用物理内存兜底：

```
规划公式：物理内存 ≥ used_memory × 3 + 1GB（OS 开销）

示例：
  预期 used_memory = 4GB
  → 物理内存 ≥ 4 × 3 + 1 = 13GB → 选 16GB 规格
```

#### 综合架构推荐

```
                    ┌──────────┐
    写入请求 ──────→│  主节点   │ ──→ 关持久化, COW = 0
                    │ (Master) │     maxmemory 按要求配置
                    └────┬─────┘
                         │ 异步复制
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐
         │ 副本 1  │ │ 副本 2  │ │ 副本 3  │  ← 只读副本
         │ RDB ON │ │ RDB ON │ │ RDB ON │    错峰 BGSAVE
         └────────┘ └────────┘ └────────┘
```

**三条铁律**：

1. **主节点永远不 fork** — COW 根除
2. **副本 BGSAVE 错峰** — 副本 1 整点、副本 2 半点，分散 IO 压力
3. **value ≤ 10KB** — 超过 10KB 的业务必须拆片

**改造成本评估**：

| 改造项                     | 消除的目标                | 工作量                | 风险    |
|:----------------------- |:-------------------- |:------------------ |:----- |
| 主节点关持久化                 | 冷启动 COW 炸弹、BGSAVE 阻塞 | 1 行配置 + 哨兵确认       | 低     |
| 关闭 THP                  | COW 页放大 10~500×      | 1 行配置 + 重启         | 低     |
| 拆 100KB value → 10×10KB | 带宽衰减、P99 爆炸          | 业务代码改造（Hash chunk） | 中     |
| Unix Domain Socket      | TCP 缓冲阻塞             | 改连接串               | 极低    |
| 预热 BGSAVE               | 冷启动 COW 峰值           | 运维脚本               | 低     |
| 增加副本                    | 单点故障                 | 多 1 台机器            | 低     |
| 凑够物理内存                  | OOM                  | 升配                 | 中（成本） |

---

## 核心发现

### 1. COW 大小 = 父进程在子进程存活期间的写入触发的 copy-on-write 总字节数

`rdb_last_cow_size` / `aof_last_cow_size` 报告的是：在上一次 BGSAVE / AOF rewrite 的子进程存活期间，**父进程的写操作**导致被 copy-on-write 复制的内存总字节数（即父进程触发的 COW 字节数）。该值是历史度量（过去一次 fork 的结果），用作容量规划时需要结合**当前写入速率**与**预计 fork 持续时间**进行估算——不能直接用上一次的值当作下一次的准确预测。

```
RDB:            子进程存活 ~2s  → COW ~245MB
AOF 100%:       子进程存活 ~2s  → COW ~66MB   (AOF 文件小，rewrite 快，父进程写得少)
AOF 500%:       子进程存活 ~3s  → COW ~264MB   (AOF 文件 ~735MB，rewrite 慢，父进程写得多)
```

**调高 percentage 反而把单次 COW 做大了**。RDB 类似逻辑：文件是全量快照，子进程从头读到尾，期间写入不断修改页面。

### 2. AOF vs RDB 的 COW 对比

本测试中 AOF（percentage=100）单次 rewrite 快、子进程存活时间短，导致单次 COW（~66MB）小于 RDB（~245MB）。但 AOF rewrite 的 COW 与 AOF 文件大小和 rewrite 耗时紧密相关——当 AOF 文件很大（如 percentage=500 时 ~735MB，单次 COW 反超 RDB 达到 ~264MB），不可笼统认为"AOF 总是比 RDB 好"。**选择持久化策略时，需要结合具体的写入量级和 AOF 文件增长速度做决策。**

### 3. 高写入场景下 AOF percentage=100 有最低的单次峰值内存压力（COW 角度）

虽然 rewrite 频率高（17 次），但单次 COW 仅 ~66MB，**单次峰值内存压力最小**。适合对内存峰值敏感、不能承受 OOM 风险的场景。但累计 COW 总量较大（~1,122MB），需要权衡。

### 4. OOM 风险近似估算公式（历史观测 + 预测）

```
估算所需物理内存 ≈ used_memory_rss + 预计_last_cow_size + 安全裕度(建议10~20%)
```

- `used_memory_rss`：Redis 进程实际占用的物理内存（OS 视角），可从 INFO memory 中获取。
- `预计_last_cow_size`：`rdb_last_cow_size` 或 `aof_last_cow_size` 是**历史观测值**，不能直接等同于下一次 fork 的 COW。未来 fork 的 COW 取决于："当前写入速率 × 预计 fork 存活时间 × 页粒度（是否开启 THP）"。若无法精确估算，**建议采用保守策略**：`物理内存 ≥ used_memory_rss × 1.5~2`。
- **安全裕度**：覆盖内存碎片、内核占用、子进程固定开销和其他进程。

当前场景示例：

- 观测值法：275MB + 245MB = 520MB，再 + 10% 裕度 ≈ **572MB**
- 保守法：275MB × 2 = **550MB**
- 若宿主机仅 512MB，OOM Kill 几乎必然触发。

---

## 解决方案与建议

### 一、强烈建议（立刻采纳）

#### 1. 预留足够物理内存

给 Redis 实例预留 1.5–2× 的物理内存余量（按峰值估算：`used_memory_rss + last_cow_size` 的 1.5–2×）。

#### 2. 把持久化移到副本（replica）

- 主节点关闭持久化（`save ""` + `appendonly no`），将 RDB/AOF 全部关闭。
- 在一台或多台副本上开启持久化并作为备份。
- 效果：主节点写入不会触发持久化 fork，COW 降到接近 0。

### 二、高效运维

#### 3. 避免在高写时段触发 BGSAVE / AOF rewrite

- 把 `save` / `auto-aof-rewrite` 参数与业务低峰窗口对齐。
- 或手动在低峰期执行 `BGSAVE` / `BGREWRITEAOF`。

#### 4. 设置并监控告警

必监控项（INFO / 系统），含具体告警阈值：

| 监控项                                                  | 来源               | 告警级别    | 告警建议                                     |
|:---------------------------------------------------- |:---------------- |:------- |:---------------------------------------- |
| `used_memory_rss` / 系统总内存                            | INFO memory      | 警告 / 严重 | RSS > 70% 系统内存 → 警告；RSS > 90% → 严重       |
| `mem_fragmentation_ratio`                            | INFO memory      | 关注 / 调查 | > 1.5 → 关注碎片；> 2.0 → 调查原因                |
| `rdb_last_cow_size` / 系统总内存                          | INFO persistence | 警告 / 严重 | last_cow_size > 30% 可用内存 → 警告；> 60% → 严重 |
| `rdb_last_bgsave_time_sec`                           | INFO stats       | 警告      | BGSAVE 耗时 > 10s → 告警（阈值根据数据规模调整）         |
| `instantaneous_ops_per_sec`                          | INFO stats       | 突降告警    | 较正常均值下降 > 50% 可能表示 fork 或阻塞              |
| `evicted_keys` 增量                                    | INFO stats       | 警告 / 严重 | 每分钟持续上升 → 内存严重压力或配置异常                    |
| `aof_rewrite_in_progress` / `rdb_bgsave_in_progress` | INFO persistence | 关注      | 持续 > 预期时间（如 30s）→ 调查                     |
| 系统 `free` / `available` 内存                           | OS               | 警告      | 低于物理内存 20% 告警                            |
| `dmesg` / OOM 日志                                     | OS               | 严重      | 出现即严重                                    |
| `latest_fork_usec`                                   | INFO stats       | 警告      | > 100ms → fork 阻塞过长，页表或内存过大              |

记录每次 fork 的 `last_cow_size` 与 fork 耗时，用于后续容量规划和趋势分析。

### 三、配置调优

#### 5. AOF 参数调优

- 调小 `auto-aof-rewrite-percentage`（例如 100）+ 合理设置 `auto-aof-rewrite-min-size`，避免文件无限膨胀到 rewrite 极慢的阶段。

#### 6. 考虑关闭主节点持久化并把持久化委托给副本

或使用异步/外部持久化方案。

#### 7. 开启 active-defrag（如果 Redis 版本支持）

尽量降低内存碎片（`mem_fragmentation_ratio`）。

#### 8. 评估 maxmemory-policy

如果所有 key 都有 TTL，`volatile-*` 策略在概念上更匹配；但本测试所有 key 都有 TTL，因此效果上差别有限，仍需验证。

### 四、进一步试验（用于完善结论）

#### 9. 完善持久化配置对比

在固定物理机上对比更多 `auto-aof-rewrite-percentage` 值（50 / 100 / 200 / 500），记录单次 COW、rewrite 次数与 AOF 文件大小。

#### 10. 验证「主节点关持久化」方案

在 replica 模式下关闭主持久化再跑一次，验证 COW 是否真正接近 0 以及写入吞吐的差异。

#### 11. 不同写入负载的敏感性测试

在不同 key/value 大小和 hash field 数量下重复测试，找出写入单元大小对 COW 的敏感性（例如把 field 从 256B 改到 64B / 1KB）。

---

## 补充说明与注意事项（避免误解）

1. **MB 与 MiB**：报告中所有 "MB" 值为近似值（来源于 INFO 输出或系统 RSS 换算后四舍五入），若需要严格计量请用 bytes 原始值并标注单位。

2. **操作系统层面**：内核 `overcommit`、`swap` 与 OOM killer 行为会影响最终是否真的被杀（例如 `overcommit=1` 会更容易分配内存但不一定能避免 OOM）。在低内存环境下不要依赖 swap。

3. **Redis 的"短暂停顿"来源**：主要是 `fork()` 调用和内核分配页表的开销，而非子进程做 IO 本身阻塞父进程。应写成"短暂停顿 + COW 写放大"。

4. **HMSET 与 HSET**：本文使用 `HSET` 命令（`HSET key field1 value1 field2 value2 ...`），`HMSET` 在 Redis 4.0+ 中已被标记为 deprecated。若面向读者或脚本，建议统一用 `HSET` 以免混淆。

---

## 附录：常用诊断命令

### 内存与 COW 检查

```bash
# 查看内存使用
redis-cli INFO memory | egrep 'used_memory_rss|used_memory_human|mem_fragmentation_ratio'

# 查看 COW 与持久化状态
redis-cli INFO persistence | egrep 'rdb_last_cow_size|aof_last_cow_size|rdb_bgsave_in_progress|aof_rewrite_in_progress|rdb_last_bgsave_time_sec'

# 查看 fork 耗时（微秒）
redis-cli INFO stats | grep latest_fork_usec

# 查看写入速率
redis-cli INFO stats | grep instantaneous_ops_per_sec

# 查看淘汰量
redis-cli INFO stats | grep evicted_keys
```

### THP 检查与关闭

```bash
# 1. 检查当前 THP 状态（所有 Redis 节点需执行）
cat /sys/kernel/mm/transparent_hugepage/enabled
# 期望输出：always madvise [never]（方括号在 never 上）

# 2. 临时关闭（立即生效，重启失效）
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag

# 3. 永久关闭：参考方案 E 中的 systemd service 或 GRUB 配置
```

### 系统内存与 OOM 检查

```bash
# 查看系统可用内存
free -h

# 查看 Redis 进程 RSS
ps -o rss,pid,cmd -p $(pgrep redis-server) | awk '{print $1/1024 " MB"}'

# 查看最近的 OOM 事件
dmesg | grep -i "out of memory" | tail -5
dmesg | grep -i "killed process" | tail -5
```

### 实时监控（在压测期间使用）

```bash
# 每秒输出一次 COW、内存、淘汰量（Ctrl+C 停止）
while true; do
  echo "=== $(date +%H:%M:%S) ==="
  redis-cli INFO persistence | grep 'rdb_last_cow_size\|aof_last_cow_size'
  redis-cli INFO memory | grep 'used_memory_rss'
  redis-cli INFO stats | grep 'evicted_keys\|instantaneous_ops_per_sec'
  sleep 1
done
```

---

## 补充场景：管道批量删除冷热混合 key（已实测 ✅）

### 背景与问题

```
生产现象: 批量刷新缓存——先把旧的全删了，再写入新的。
  
  for (...) {
      pipeline.del(randomOldKey);    // 存量 key（内存中已存在）
      pipeline.setex(newKey, ttl, v); // 新增 key（内存中不存在）
  }
  pipeline.flushCommands();

问题:
  1. DEL 存量 key → 释放 dict entry + value 空间 → 内存碎片
  2. SETEX 新 key → 分配新 dict entry + value → 可能触发 dict 扩容
  3. DEL + SET 交替进行 → alloc/free 频繁交错 → 碎片率飙升（mem_fragmentation_ratio > 1.5）
```

### 根因：冷热混合引发的三连击

```
Redis 内存管理是 jemalloc/glibc malloc 的封装——不是 GC 语言。
每次 DEL 释放的内存不会立即还给 OS，而是留在 allocator 的空闲链表中。

冷热混合的三连击:

第 1 击: DEL 存量 key（"热" = 已插入 / "冷" = 不常访问但占用空间）
  → 释放 dict entry → allocator 标记为空闲
  → 释放 value 空间（可能是不同 size class）→ 产生碎片
  → dict 可能触发缩容（hashtable_resize_min = 4）

第 2 击: SETEX 新增 key
  → 分配新 dict entry → allocator 不一定复用刚释放的块（size 可能不同）
  → dict 可能触发扩容（hashtable_resize，重新 hash 所有 entry）

第 3 击: DEL + SET 交替在一个 Pipeline 批量中
  → alloc/free 快速交替 → jemalloc 的 size class 缓存污染
  → RSS（实际物理内存）不降但 used_memory 在波动
  → mem_fragmentation_ratio 飙升
```

**dict 扩容/缩容的 CPU 成本**：

```
dict 默认负载因子:
  扩容阈值: used / size > 1 且 dict_can_resize = 1
  缩容阈值: used / size < 0.1

假设 dict size = 2^21 = 2M entries:
  扩容到 2^22 = 4M: 需要 rehash 所有 2M 个 entry → O(N)
  缩容到 2^20 = 1M: 同理 O(N)

  Pipeline 中大量 DEL → used 骤降 → 触发 dict 缩容
              → 紧接着大量 SETEX → used 骤升 → 触发 dict 扩容

  → 主线程在缩容→扩容之间反复 rehash → CPU 拉高 → 全局延迟飙升
```

### 理论影响矩阵

| 因素 | 影响 | 风险 |
|------|------|------|
| 删除 key 比例 | 越高 → 碎片越多 | ⚠️ |
| 新增 key 数量 = 删除 key 数量 | alloc/free 对齐 → 碎片适中 | ⚠️ |
| 新增 key 数量 ≠ 删除 key 数量 | alloc/free 不匹配 → 碎片严重 | 🔴 |
| 旧 key size ≠ 新 key size | jemalloc 不同 size class → 碎片最严重 | 🔴 |
| Pipeline batchSize 大（>500） | dict 缩容→扩容窗口内，主线程阻塞 >1ms | 🔴 |

### 生产规避方案

| 优先级 | 方案 | 说明 |
|:---:|------|------|
| 1 | **用 `UNLINK` 代替 `DEL`** | 异步删除，将内存释放移到后台 BIO 线程，主线程只标记删除，碎片产生时机延后（不在 Pipeline 热路径上） |
| 2 | **用 `RENAME` 代替 DEL+SET** | 批量刷新时：写临时 key → RENAME 原子替换，旧 key 被原子删除（Redis 内部的 del 操作），避免了 Pipeline 中的显式 DEL |
| 3 | **错峰删除** | 把 DEL 操作拆到单独的低峰窗口，不和写入 Pipeline 混一起 |
| 4 | **控制 dict 大小** | 如果 dict 必然大幅缩小，主动 `DEBUG HTSTATS` 监控，考虑 `activerehashing` 让 rehash 以步进方式执行（分摊 CPU） |
| 5 | **碎片监控** | 监控 `mem_fragmentation_ratio`，定期 `MEMORY PURGE`（jemalloc 版）或 `activedefrag`（若开启） |

```java
// ❌ 反模式: Pipeline 中 DEL + SETEX 交替
for (String oldKey : oldKeys) {
    pipeline.del(oldKey);                        // 同步删除 → 碎片
    pipeline.setex(newKeyGen.next(), 600, val);  // 立即分配 → 碎片加倍
}

// ✅ 方案1: UNLINK 异步删除 + 错峰
for (String oldKey : oldKeys) {
    pipeline.unlink(oldKey);                     // 后台异步删，不占主线程
}
pipeline.flushCommands();
// ... 等 UNLINK 后台完成（监控 lazyfree_pending_objects）
for (...) {
    pipeline.setex(newKey, 600, val);            // 纯写入，无 alloc/free 交错
}

// ✅ 方案2: RENAME 原子替换（推荐）
String tmpKey = "cache:tmp:" + batchId;
for (...) {
    pipeline.setex(tmpKey + ":" + i, 600, val);  // 写入临时 key
}
pipeline.flushCommands();
// 然后 low-frequency RENAME（非高频 Pipeline 热路径）
redis.rename(tmpKey + ":" + i, realKey);          // 原子替换，旧 key 被隐式删除
```

**原理**：`UNLINK` 在主线程只做 `dictDelete()`（O(1) 摘链 + mark），实际 `free()` 放到 `bio_lazy_free` 后台线程。而 `RENAME` 是单命令原子操作，删旧写的成本在 Redis 内部一条命令完成，不在应用层 Pipeline 里混。

### 待测维度

| 测试项 | 方法 | 关键指标 |
|------|------|------|
| 碎片率对比 | 相同写入量/速率，对比 DEL/UNLINK/RENAME 三种方案的 `mem_fragmentation_ratio` | 碎片率曲线 |
| dict 扩容 CPU | `INFO stats` 跑满一轮后记录 `instantaneous_ops_per_sec` + `used_cpu_sys` | CPU 吞吞吐降幅% |
| slowlog 增长 | 记录 `slowlog len` 增量，对比三种方案 | slowlog count/s |
| dict size 震荡 | `DEBUG HTSTATS 0` 每 5 秒一次，记录 size/used 变化 | size 震荡范围 |

---

### 实测：DEL vs UNLINK vs RENAME 3 轮对比

**测试环境**：c=32, batchSize=200, prefill=200K, warmup=10s, sample=15s。每轮独立 `FLUSHDB` + 预填充。

| 指标 | **DEL + SETEX**<br>(反模式) | **UNLINK + SETEX**<br>(异步删除) | **SETEX + RENAME**<br>(原子替换) |
|------|:---:|:---:|:---:|
| **cmds/s** | 446,182 | **512,796** ✅ | 358,129 |
| vs DEL | — | **+14.9%** | -19.7% |
| **avg 延迟** | 28.97ms | **25.23ms** ✅ | 36.27ms |
| **p50** | 25.60ms | **24.30ms** | 33.44ms |
| **p99** | 87.21ms | **47.62ms** ✅ | 99.29ms |
| **max** | 161.70ms | **65.53ms** | 158.36ms |
| **slowlogΔ** | 15 | **5** ✅ | 23 |
| **碎片率** | 1.28→1.28 | 1.29→1.28 | 1.29→1.28 |
| **errors** | 0 | 0 | **8** ❌ |

#### 关键发现

**1. UNLINK 全面领先 DEL**

| 维度 | 提升 |
|------|------|
| 吞吐 | **+14.9%** |
| P99 延迟 | **-45%**（87ms → 48ms） |
| slowlog | **-67%**（15 → 5） |

`UNLINK` 在主线程只做 O(1) 摘链 + mark，实际 `free()` 交给 `bio_lazy_free` 后台线程。Pipeline 不被内存释放阻塞，延迟分布更稳定。

**2. RENAME 反而最差（与理论预期相反）**

理论认为 RENAME 是单命令原子操作，零显式删除。实测却最差：

| 退化 | 幅度 | 原因 |
|------|------|------|
| 吞吐 | **-19.7%** vs DEL | RENAME 内部隐式 DEL + 字典查找 + 重命名三步开销 |
| slowlogΔ | **23**（是 UNLINK 的 4.6x） | Pipeline 中密集调用时，隐式删除不可异步化 |
| errors | **8** 次超时/冲突 | tmp key 在并发 Pipeline 中存在竞态 |

**RENAME 的核心问题**：在 32 连接并发下，`tmp:0..N` 的写入和 `RENAME(tmp→old)` 不是原子的跨命令。当 Worker A 写入 `tmp:42` 但还没 RENAME 时，Worker B 也可能写入 `tmp:42` → `RENAME` 时数据错乱或 key 已不存在。

**3. 碎片率无明显差异**

三轮测试碎片率都在 1.28-1.29，没有明显恶化。原因：200K × (64B value + 56B dict overhead) ≈ 24MB，在 `used_memory` 37M 中占 65%，但在 jemalloc arena 复用范围内——释放的 `dictEntry`（~24B）和 value（64B）是不同 size class，相互不产生跨 class 碎片。

#### 实现解析

**3 轮扫测架构**：

```
runSweep()
  ├─ FLUSHDB + prePopulate()  // 插入 200K 存量 key (mc:cold:old:0..199999)
  ├─ Round1: runRound("DEL_SETEX", "DEL")
  │     └─ 32 Workers, 每轮: del(oldKeys[idx]) + setex(newKeys[idx])
  ├─ FLUSHDB + prePopulate()
  ├─ Round2: runRound("UNLINK_SETEX", "UNLINK")
  │     └─ 32 Workers, 每轮: unlink(oldKeys[idx]) + setex(newKeys[idx])
  ├─ FLUSHDB + prePopulate()
  └─ Round3: runRound("SETEX_RENAME", "RENAME")
        └─ 32 Workers, 每轮: setex(tmpKeys[idx]) + rename(tmp → old)
```

**Key Pool 优化**：一次性分配 3 × 200K = 600K 个 String 到数组，热路径用下标取 key（`oldKeys[rng.nextInt(200000)]`），零分配零拼接。

**Worker 热路径**：

```java
// 每批次 batchSize × 2 个命令，执行完后 await 等待
List<RedisFuture<?>> futures = new ArrayList<>(batchSize * 2);

switch (mode) {
    case "DEL":
        del(oldKey);      // 同步释放 dict entry + value
        setex(newKey);    // 立即重新分配 → 碎片
    case "UNLINK":
        unlink(oldKey);   // 只标记，不释放
        setex(newKey);    // alloc 可复用已标记块
    case "RENAME":
        setex(tmpKey);    // 写入临时 key
        rename(tmp→old);  // 原子替换（内部隐式 DEL）
}

async.flushCommands();          // 整批发出
for (f : futures) f.get(2s);   // 同步等待完成
```

**指标采集**：每个 Round 前后抓取：

- `INFO memory` → `mem_fragmentation_ratio`、`used_memory_human`、`used_memory_rss_human`
- `SLOWLOG LEN` → 慢查询增量
- `INFO stats` → `used_cpu_sys` CPU 系统态增量
- `DBSIZE` → key 数量变化
- 客户端采样：所有 batch 的 `(await_end - batch_start)` 延迟，计算 p50/p90/p99

### 实测结论

```text
实测结论（c=32, batch=200, prefill=200K）:
  1. UNLINK 全面领先 DEL: 吞吐 +15%、P99 -45%、slowlog -67%
  2. RENAME 反而最差: 吞吐 -20%、slowlog 是 UNLINK 的 4.6 倍，且存在并发竞态（8 次错误）
  3. 碎片率无显著差异（1.28-1.29），原因: 200K key 在 jemalloc arena 复用范围内
  4. 生产推荐: UNLINK 替代 DEL，改动最小（1 行代码），收益最显著
  5. 不推荐 RENAME: 在并发 Pipeline 中存在 tmp key 冲突，且性能不如 UNLINK
```

**生产最优方案**：

```java
// ✅ 一行改动：DEL → UNLINK，立即生效
- futures.add(async.del(oldKey));
+ futures.add(async.unlink(oldKey));   // +15% 吞吐, P99 -45%

// 备选：错峰删除（UNLINK 单独成批 + SETEX 纯写）
for (String oldKey : oldKeys) {
    pipeline.unlink(oldKey);           // 全异步删除
}
pipeline.flushCommands();
Thread.sleep(100);                      // 给 BIO 线程一点时间
for (...) {
    pipeline.setex(newKey, ttl, val);  // 纯写入，零删除开销
}
```

