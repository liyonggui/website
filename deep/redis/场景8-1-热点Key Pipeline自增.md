## 场景 8: 热点 Key Pipeline INCR 自增

### 8.1 背景与问题

```
Redis Cluster + Pipeline 高并发 INCR 同一个热点 key:

  所有 Pipeline 并发 INCR 同一个 key → 该 key 落在唯一 slot → 唯一节点处理
  
  Worker-1 ── INCR hotkey:counter ─┐
  Worker-2 ── INCR hotkey:counter ─┤
  Worker-3 ── INCR hotkey:counter ─┤ → 全部打到同一个节点
  Worker-4 ── INCR hotkey:counter ─┘
  
  → 单节点 CPU 100%，其他节点空闲
  → 主从复制压力激增（详见下文复制原理分析）
  → 从节点复制延迟飙升，可能触发主从切换
```

```
Redis 是单线程模型:
  - 所有 INCR 在同一个节点上排队执行
  - 单个节点 CPU 跑满 → 延迟飙升 → 超时
  - 扩容集群对热点 key 无效（加机器不分散热点 key 的负载）
```

**这是比 CROSSSLOT 更隐蔽的生产事故——不会报错，但延迟会从 1ms 恶化到 50ms+。**

#### 8.1.1 主从复制（Replication）压力原理

```
Redis 复制是命令流层面的复制（RDB 快照 + 实时命令流）：
  Master 执行每条写命令 → 写入 replication buffer (output buffer) → 通过 socket 发送到每个 replica
  → Replica 接收命令流 → 应用到本地 → 更新 replication offset

关键机制：replication backlog
  ┌─ 是一个循环字节缓冲区（circular byte buffer），位于 Master 侧
  ├─ 存储最近已发送给 replica 的写命令流（以字节衡量，非命令条数）
  ├─ 大小由 repl-backlog-size 配置（例如 256MB / 512MB）
  ├─ 当 replica 断连后重连：若 offset 仍落在 backlog 窗口内 → 可增量 PSYNC（部分同步）
  └─ 若 offset 已被覆盖（写入太快 + 断连太久）→ 必须触发全量同步（RDB + 后续命令流）
```

```
高频 INCR 对复制的危害：
  ├─ 每秒 360K 次 INCR 都要通过复制管道逐条传输
  ├─ Replica 的 apply 速度可能跟不上 Master 写入速度
  ├─ Replication output buffer 持续积压（Master 侧可能 OOM）
  ├─ Replica offset lag 持续增大 → 主从切换时数据丢失风险
  ├─ Replica 忙于应用复制日志 → 无法有效服务读请求
  └─ 高频写入导致 backlog 被快速覆盖 → 增量 psync 失效 → 触发全量同步（见 8.14 详述）

通过 LOCAL_BUFFERED 优化后:
  Master 执行: 每秒 1 次 INCRBY（而非 360K 次 INCR）
  → 复制命令数从 ≈360k 条/s 降到 1 条/s（命令级别减少 ≈360k×）
  → 复制字节/带宽近似线性减少，但实际带宽减小比例还取决于每条命令的字节大小
  → Replica 轻松跟上 → 不再有延迟问题
```

### 8.2 热点 Key 与集群的矛盾

```
┌──────────────────────────────────────────────────────────────────────┐
│            集群的 slot 机制 vs 热点 key                              │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  slot = CRC16(key) % 16384 是固定的，同一 key 跨环境 slot 不变。      │
│  本文示例环境中 hotkey:counter 的 slot 为 15254：                    │
│                                                                      │
│  CRC16("hotkey:counter") % 16384 = 15254                             │
│    → slot 15254 → 对应的 master（在本测试环境为 172.18.0.4:6379，     │
│       可通过 CLUSTER KEYSLOT hotkey:counter / CLUSTER SLOTS 验证）     │
│                                                                      │
│  无论集群有多少个节点，同一个 key 永远只在同一个节点上：               │
│                                                                      │
│    某 master-A: 不含 slot 15254   ← 0 流量                           │
│    某 master-B: 不含 slot 15254   ← 0 流量                           │
│    某 master-C: 含 slot 15254     ← 100% 流量（热点 key 在这）        │
│                                                                      │
│  → 水平扩容对热点 key 无效，加再多机器也只是空闲 +1                   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 8.3 风险现象

| 现象 | 原因 | 危害 |
|:--|:--|:--|
| **单节点 CPU 瓶颈** | 所有 INCR 排队在同一节点执行 | 延迟飙升，超时雪崩 |
| **主从同步流量打满** | 每秒几十万次 INCR → 几十万条复制命令 | replica 延迟增大，数据不一致窗口变大 |
| **集群利用率极低** | 3 主节点只有 1 个工作 | 资源浪费，形同虚设 |
| **扩容无效** | 加节点不改变热点 key 的 slot | 欺诈性扩容——加了机器性能不变 |

### 8.4 五种模式对比

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  SINGLE_KEY: 全部 INCR 同一个 key — 演示热点瓶颈                                  │
│                                                                                  │
│    for i in 1..batchSize:                                                        │
│      INCR hotkey:counter    ← 100 条全打同一个 key                                │
│    → 同一 slot 同一节点 → CPU 100%                                                │
│                                                                                  │
│  DISTRIBUTED: hashtag 前缀 + 随机后缀 — 分散负载，不可聚合                         │
│                                                                                  │
│    for i in 1..batchSize:                                                        │
│      INCR {hot:rnd}:counter   ← 1000 个随机后缀                                  │
│    → 多 slot 多节点分担，但无法 MGET 聚合                                         │
│                                                                                  │
│  SHARDED: 固定 N 个分片 — 分散负载 + 可 MGET 聚合                                 │
│                                                                                  │
│    for i in 1..batchSize:                                                        │
│      INCR {incr:shardId}:counter  ← 1000 个固定分片                              │
│    → 多 slot 多节点分担 + MGET 所有分片即可得到总数                               │
│                                                                                  │
│  LOCAL_BUFFERED: 本地 AtomicLong 计数 + 定时 INCRBY — Redis 零压力               │
│                                                                                  │
│    本地: counter.incrementAndGet() × N  ← 原子操作，纳秒级                        │
│    定时: INCRBY hotkey:counter sum  ← 1 次/秒，汇总写回                          │
│    → Redis 每秒只收 1 次 INCRBY，承受能力趋于无穷                                 │
│    → 每条请求 1 次 AtomicLong.incrementAndGet（CAS），高并发下竞争重试多          │
│                                                                                  │
│  COMPUTED: 模拟复杂业务计算 + 本地缓冲 + 批量 INCRBY — 生产级方案                  │
│                                                                                  │
│    每条请求: delta = computeWeight(userId, videoId, quality, duration, isVip)    │
│    本地汇总: batchDelta += delta（局部变量累加，0 次原子操作）                     │
│    批量提交: AtomicLong.addAndGet(batchDelta)  ← 每批仅 1 次 CAS，几乎无竞争    │
│    定时写回: INCRBY hotkey:computed:total sum ← 1 次/秒，批量入库                │
│    → Redis 完全不参与计算，只做最终持久化                                        │
│    → 比 LOCAL_BUFFERED 更快：减少 CAS 竞争次数（100 → 1/batch）                  │
│    → 吞吐提升约 2.7 倍（核心因素：减少共享原子操作次数 + 计算留本地避免共享写）   │
│                                                                                  │
│ Pipeline 与 RTT 的关系：                                                          │
│  Pipeline 是 client-side batching，N 条命令在一个网络往返（RTT）内发送。           │
│  吞吐 ≈ (并发数 × pipelineSize) / RTT。Docker 单机 RTT ≈ 0.1ms，可跑出高吞吐；   │
│  在真实分布式环境 RTT=1~5ms 时，同配置下的 cmd/s 会显著降低。                       │
│                                                                                  │
│ Hashtag {} 原理（补充）：                                                          │
│  键名中 {} 内的内容用于 hash；若 key 不含 {}，整个 key 被 hash。                    │
│  示例: {user:1}:follows 与 {user:1}:profile 同在 slot=CRC16("user:1") % 16384。  │
│  注意: 不要误用同一 hash tag 把不同逻辑 key 强制放到同一 slot（会制造新热点）。     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 8.5 压测架构

```
SINGLE_KEY 模式 Worker:
  ┌──── Pipeline 100 INCR ────┐
  │ INCR hotkey:counter × 100 │  ← 全部同一个 key
  └────────────────────────────┘
  → 同一 slot → 同一节点 → CPU 热点
  → 1 个节点扛全部压力

DISTRIBUTED 模式 Worker:
  ┌──── Pipeline 100 INCR ──────┐
  │ INCR {hot:rnd}:counter × 100│ ← 1000 个随机后缀
  └──────────────────────────────┘
  → 多 slot 多节点分摊 → 无热点
  → 但无法 MGET 聚合（后缀不确定）

SHARDED 模式 Worker:
  ┌──── Pipeline 100 INCR ───────┐
  │ INCR {incr:shard}:counter    │ ← 1000 个固定分片
  │ 随机选一片写入              │
  └──────────────────────────────┘
  → 多 slot 多节点分摊 → 无热点
  → MGET {incr:0}~{incr:999} → 实时聚合总数 ✅

LOCAL_BUFFERED 模式 Worker:
  ┌──── 本地循环 100 次 ────┐
  │ counter.incrementAndGet  │  ← 纯内存操作，纳秒级
  └──────────────────────────┘
  ┌──── 后台定时任务 ────────┐
  │ 每 1s: sum 所有本地计数器│
  │ INCRBY hotkey:counter    │  ← 汇总后 1 次写回 Redis
  └──────────────────────────┘
  → Redis 每秒 1 次 INCRBY → 几乎零压力
  → 本地吞吐取决于 CPU 核数 → 千万级别 cmd/s

COMPUTED 模式 Worker（生产级"计算在应用层 → 批量写回"范式）:
  ┌──── 模拟 100 条业务请求 ───────────────────────┐
  │ for i in 1..batchSize:                         │
  │   userId=随机, videoId=随机, quality=0~3, ...  │
  │   delta = computeWeight(userId, videoId, ...)  │  ← 复杂计算在本地完成
  │   if delta > 0: batchDelta += delta            │
  └────────────────────────────────────────────────┘
  ↓ 合并为 1 次 AtomicLong.addAndGet(batchDelta)
  ┌──── 后台定时任务 ─────────┐
  │ 每 1s: sum 所有本地计数器 │
  │ INCRBY hotkey:computed    │  ← 批量写回
  └──────────────────────────┘
  → 15s 内完成 13 亿次业务计算，仅 15 次 Redis INCRBY
  → 吞吐量比 LOCAL_BUFFERED 更高（批量 CAS 减少原子竞争）
```

### 8.6 REST 端点

```
GET  /hotkey/metrics         -- 当前指标（cmd/s / avgMs / targetNode）
POST /hotkey/start           -- 启动（见下方参数说明）
POST /hotkey/stop            -- 停止
POST /hotkey/sweep           -- 自动遍历 concurrency 梯度
GET  /hotkey/sweep/results   -- Sweep 对比报告
POST /hotkey/sweep/compare   -- 五种模式全跑对比 (?concurrency=16&batchSize=100)
POST /hotkey/sweep/clear     -- 清空 Sweep 结果

参数说明:
  concurrency: Worker 并发线程数（每线程独立执行 Pipeline / 本地循环）
  batchSize:   每个 Worker 在单个工作周期内的命令/操作数
               (例: batchSize=100 表示 1 个 Worker 在 1 个周期执行 100 次操作)
               总吞吐 ≈ concurrency × batchSize × 工作频率(Hz)

mode 参数:
  SINGLE_KEY:     所有 Pipeline INCR 同一个 key → 单节点瓶颈
  DISTRIBUTED:    随机 hashtag 后缀分散写 → 多节点分担（不可聚合）
  SHARDED:        固定 N 个分片随机写入 → 多节点分担 + MGET 可聚合
  LOCAL_BUFFERED: 本地 AtomicLong 自增 + 定时 INCRBY 批量写回 → Redis 零压力
  COMPUTED:       模拟复杂业务计算 + 本地缓冲 + 批量 INCRBY → 生产级高并发计数
```

### 8.7 测试方法

#### 8.7.1 启动集群

```bash
# 确保 3 主 3 从集群已运行
docker-compose --profile cluster ps
```

#### 8.7.2 SINGLE_KEY 模式 — 热点瓶颈演示

```bash
# 16 并发 × 100 batch → 全部 INCR 同一个 key
curl -X POST "http://localhost:8082/hotkey/start?concurrency=16&batchSize=100&mode=SINGLE_KEY"

curl -s http://localhost:8082/hotkey/metrics
# targetNode: slot=15254 node=172.18.0.4:6379  ← 可通过 CLUSTER KEYSLOT/CLUSTER SLOTS 验证
# cmdPerSec: ~307K
# avgLatencyMs: ~0.051
```

#### 8.7.3 DISTRIBUTED 模式 — 随机 hashtag 后缀分散

```bash
curl -X POST "http://localhost:8082/hotkey/start?concurrency=16&batchSize=100&mode=DISTRIBUTED"

# cmdPerSec: ~184K   ← 负载分散到多 slot
# targetNode: slots=[3592, 7721, 11850, 15979, 3724]  ← 5 个不同 slot
```

#### 8.7.4 SHARDED 模式 — 固定分片 + MGET 聚合

```bash
curl -X POST "http://localhost:8082/hotkey/start?concurrency=16&batchSize=100&mode=SHARDED"

# cmdPerSec: ~199K   ← 固定分片均匀分布
# targetNode: slots=5个不同slot, MGET sum=3,387,893  ← 支持实时聚合
```

#### 8.7.5 LOCAL_BUFFERED 模式 — 本地内存计数

```bash
curl -X POST "http://localhost:8082/hotkey/start?concurrency=16&batchSize=100&mode=LOCAL_BUFFERED"

curl -s http://localhost:8082/hotkey/metrics
# localIncr/s: ~35,000,000  ← 本地内存操作，纳秒级
# redisFlush/s: ~1.0        ← 每秒只需 1 次 INCRBY 写回 Redis
```

#### 8.7.6 COMPUTED 模式 — 复杂业务计算 + 本地缓冲

```bash
curl -X POST "http://localhost:8082/hotkey/start?concurrency=16&batchSize=100&mode=COMPUTED"

curl -s http://localhost:8082/hotkey/metrics
# localIncr/s: ~95,000,000   ← 批量 CAS，比 LOCAL_BUFFERED 还快 2.7x！
# redisFlush/s: ~1.0         ← 每秒 1 次 INCRBY 入库
# computation: weight = f(userId, videoId, quality, duration, isVip) × 100

# 停止后查看 Redis 中的累计值
docker exec -it redis-project-m2-1 redis-cli -c GET hotkey:computed:total
```

#### 8.7.7 Sweep Compare — 五模式全跑对比

```bash
# 自动跑 5 种模式（每模式 5s 预热 + 15s 采样，共约 100s）
curl -X POST "http://localhost:8082/hotkey/sweep/compare?concurrency=16&batchSize=100"

# 等待完成查看对比报告
curl -s http://localhost:8082/hotkey/sweep/results
```

### 8.8 实测对比数据（16 并发，batchSize=100，15s 采样）

| 模式 | cmd/s（客户端操作速率） | 延迟 | Redis 写入频率（命令/s 发送到 Redis） | 目标节点/槽位 | 核心价值 |
|:--|:--|:--|:--|:--|:--|
| **SINGLE_KEY** | **359,610** | 0.044ms | 359k INCR/s | slot=15254 单节点 | 单节点扛全部压力 |
| **DISTRIBUTED** | 293,819 | 0.054ms | 293k INCR/s | 5 个不同 slot | 分散但不可聚合 |
| **SHARDED** | 254,313 | 0.062ms | 254k INCR/s | 5 个不同 slot | MGET 实时聚合 |
| **LOCAL_BUFFERED** | **34,846,307** 🔥 | N/A (本地) | 1 INCRBY/s（仅 16 次 Redis INCRBY） | 本地内存 → 1 次/秒写回 | **97x 于 SINGLE_KEY** |
| **COMPUTED** | **95,226,717** 🔥🔥 | N/A (本地) | 1 INCRBY/s（仅 15 次 Redis INCRBY） | 本地内存 → 1 次/秒写回 | **265x 于 SINGLE_KEY** |

> **测试条件说明**（供复现参考）：
> - 硬件: Docker 单机，3 主 3 从 Redis 7.x 集群
> - 网络: Docker bridge 内网，延迟 < 0.1ms
> - 客户端: Lettuce 6.x，16 并发 × batchSize=100
> - LOCAL_BUFFERED: 本地 AtomicLong 自增 + 1s 间隔 INCRBY 写回
> - COMPUTED: 本地 computeWeight() + AtomicLong 批量累加 + 1s 间隔 INCRBY


#### 8.8.1 为什么 SINGLE_KEY 还有 ~360k cmd/s？

```
Pipeline 机制决定了单节点 INCR 也能跑出高吞吐：

  ├─ Docker 内网延迟 < 0.1ms（接近本地回环）
  ├─ Pipeline 批量发送: 100 条 INCR 合并为 1 次网络往返
  │   → 16 并发 × 100 batch × 约 225 次/秒 = ~360k
  │   → 近似公式: throughput ≈ (concurrency × pipelineSize) / RTT
  │      Docker 单机 RTT ≈ 0.1ms → (16 × 100) / 0.0001 ≈ 16M 理论上限（实际受限于 Redis 单线程）
  │      真实分布式 RTT=1~5ms → 同配置下 cmd/s 会大幅降低
  ├─ Lettuce 连接池复用 + 异步 IO（Netty 事件驱动）
  └─ Redis 单线程处理 INCR 非常快（纯内存操作，无磁盘）

瓶颈在单节点 CPU/网络，而非 Pipeline 本身。
Docker 单机环境下这个瓶颈不明显，但在真实分布式集群中会退化到 ~150k。

与 LOCAL_BUFFERED (34.8M) 的 97 倍差距源于:
  SINGLE_KEY:    每条请求都触发 Redis INCR（网络 + 排队 + 复制）
  LOCAL_BUFFERED: 纯内存操作，Redis 完全不参与请求路径
```

```
关键发现:

  ┌─ SINGLE_KEY vs LOCAL_BUFFERED ──────────────────────────────────────┐
  │                                                                      │
  │  Redis INCR:      ~359k cmd/s（受单节点 CPU/网络限制）                │
  │  本地 AtomicLong: ~34.8M cmd/s（纯内存操作，纳秒级）                  │
  │                                                                      │
  │  差距: 97 倍。15 秒内仅 16 次 Redis INCRBY，完成 5.2 亿次自增。      │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘

  ┌─ LOCAL_BUFFERED vs COMPUTED ────────────────────────────────────────┐
  │                                                                      │
  │  本地自增:  ~34.8M cmd/s（每条命令 1 次 AtomicLong.incrementAndGet） │
  │  批量计算:  ~95.2M cmd/s（100 条合并为 1 次 AtomicLong.addAndGet）   │
  │                                                                      │
  │  COMPUTED 比 LOCAL_BUFFERED 快 2.7 倍！                              │
  │                                                                      │
  │  原因分解（减少 CAS 竞争 + 计算留在本地避免共享写）:                    │
  │    LOCAL_BUFFERED:                                                  │
  │      ├─ 每条请求做 1 次 AtomicLong.incrementAndGet → 高并发 CAS 冲突  │
  │      ├─ batchSize=100 → 100 次 CAS / batch                          │
  │      └─ 16 并发 → 每秒上千次 CAS 竞争同一个 AtomicLong → 重试成本高  │
  │    COMPUTED:                                                        │
  │      ├─ delta = computeWeight(...) → 纯本地计算，0 次 CAS           │
  │      ├─ batchDelta += delta → 局部变量累加，0 次 CAS（线程私有）     │
  │      ├─ 100 条合并为 1 次 addAndGet → 1 次 CAS /batch               │
  │      └─ 16 并发 × 1 次 CAS = 极低竞争 → 几乎无重试                  │
  │                                                                      │
  │  核心公式: 吞吐 ∝ 1 / CAS竞争次数                                   │
  │  不是批量合并本身快，而是大幅减少了共享原子操作次数。                 │
  │  计算开销（几百 ns）远小于 CAS 失败重试开销。                        │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘

  ┌─ COMPUTED 的真实生产价值 ───────────────────────────────────────────┐
  │                                                                      │
  │  15 秒内:                                                            │
  │    • 模拟 13 亿条业务请求（userId/videoId/quality/duration/VIP权重）  │
  │    • 每条经过完整的 computeWeight() 计算                             │
  │    • 只调了 15 次 Redis INCRBY（1次/s）                              │
  │    • Redis 完全不受负载影响                                          │
  │                                                                      │
  │  代码模式可直接复用:                                                  │
  │    请求 → 本地权重计算 → AtomicLong.addAndGet(delta)                 │
  │    → 定时器汇总 → INCRBY hotkey:total batchSum                       │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘

  ┌─ SHARDED vs DISTRIBUTED ────────────────────────────────────────────┐
  │                                                                      │
  │  SHARDED:      ~254k cmd/s  + MGET 实时聚合 ✅                       │
  │  DISTRIBUTED:  ~293k cmd/s  + 无法聚合 ❌                            │
  │                                                                      │
  │  SHARDED 牺牲 ~13% 写入吞吐，换取 MGET 实时可读。                     │
  │                                                                      │
  │  差异原因分析:                                                        │
  │    ① Hashtag 分散均匀性:                                             │
  │       DISTRIBUTED: {hot:随机ID} → CRC16 覆盖全 16384 个 slot 空间   │
  │       SHARDED:     {incr:0~999} → 1000 个固定 CRC16 值              │
  │       → 固定分片的 slot 分布可能不如纯随机均匀                        │
  │    ② MGET 采样开销:                                                  │
  │       SHARDED 在 collectTargetNodeInfo 中执行 MGET 1000 个 key       │
  │       → 有边际开销（不在采样窗口内，但连接/拓扑查询有影响）            │
  │    ③ 改进方案:                                                       │
  │       ├─ 增加分片数（如 1000 → 10000）                               │
  │       ├─ 或在多机真实集群环境重新验证                                │
  │       └─ MGET 结果本地缓存（不必每次全量查询）                       │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘
```

### 8.9 解决方案对比

| 方案 | Redis 压力 | 本地吞吐 | 读取方式 | 一致性 | 推荐场景 |
|:--|:--|:--|:--|:--|:--|
| **SINGLE_KEY** | 极高 | ~359k | INCR 直读 | 强一致 | ❌ 不推荐 |
| **DISTRIBUTED** | 中 | ~293k | 不可聚合 | 不可读 | 只写不读的计数 |
| **SHARDED** | 中 | ~254k | MGET 所有分片求和 | 可实时聚合 | 需要可读计数的写入 |
| **LOCAL_BUFFERED** | **极低**（1次/s） | **34.8M** | GET hotkey:counter | 最终一致（≤1s） | 简单计数高并发 |
| **COMPUTED** | **极低**（1次/s） | **95.2M** 🔥 | GET hotkey:computed:total | 最终一致（≤1s） | **复杂业务高并发计数首选** |

### 8.10 解决方案实现

#### 8.10.1 SHARDED: 固定分片方案

```java
/*
 * Hashtag {} 原理（Redis Cluster hash tag）：
 *   Redis Cluster 只对 {} 内的内容计算 CRC16 哈希，{} 外的部分被忽略。
 *   若 key 不包含 {}，整个 key 被 hash。
 *
 *   例如:
 *     {incr:0}:counter  → slot = CRC16("incr:0")  % 16384  → slot A
 *     {incr:1}:counter  → slot = CRC16("incr:1")  % 16384  → slot B (不同)
 *     {incr:2}:counter  → slot = CRC16("incr:2")  % 16384  → slot C (不同)
 *     incr:0:counter    → slot = CRC16("incr:0:counter") % 16384 → slot X (全文哈希)
 *
 *   {incr:0}:v1 + {incr:0}:v2  → 同为 slot A → 可 MGET/SET/DEL/Lua 批量操作
 *   {incr:0}:v1 + {incr:1}:v2  → 不同 slot → 集群客户端自动拆分路由
 *
 * ⚠ 注意事项:
 *   - 不要误把不同逻辑 key 用同一 hash tag 集中到同一 slot → 会制造新热点
 *   - 仅在有跨 key 操作需求时（MGET / Lua / Transaction）才用 hash tag
 *   - Hashtag 是固定映射，不受集群节点数变化影响
 *
 * 此处 SHARDED 通过固定的 {incr:0}~{incr:999} 使每条 INCR 写入不同 slot，
 * 热点被均匀分散到多个节点。MGET 读取时集群客户端自动跨节点路由。
 */

// 写入: 随机选一片 INCR
int shard = ThreadLocalRandom.current().nextInt(1000);
redis.incr("{incr:" + shard + "}:counter");

// 读取: MGET 所有分片求和
String[] keys = new String[1000];
for (int i = 0; i < 1000; i++) {
    keys[i] = "{incr:" + i + "}:counter";
}
List<Long> values = redis.mget(keys);
long total = values.stream().filter(Objects::nonNull).mapToLong(Long::longValue).sum();
```

#### 8.10.2 LOCAL_BUFFERED: 本地内存方案（简单计数）

```java
// 每线程一个本地计数器
private final AtomicLong localCounter = new AtomicLong();

// 高频写入: 纯内存操作
public void increment() {
    localCounter.incrementAndGet();  // 纳秒级
}

// 后台定时 flush: 汇总 → INCRBY
@Scheduled(fixedRate = 1000)
public void flush() {
    long delta = accumulator.get();  // 汇总所有线程的计数器
    if (delta > 0) {
        redis.incrby("hotkey:counter", delta);  // 1 次/s
    }
}
```

#### 8.10.3 COMPUTED: 生产级"计算在应用层 → 批量写回"方案

这是完整的生产级模式，直接可用于视频观看计费、广告曝光计费等复杂计数场景。

**核心思路**：复杂计算在应用层完成 → 只把最终 delta 批量 INCRBY 写回 Redis。

```java
/**
 * 生产级高并发计数：本地权重计算 → 批量 INCRBY 入库。
 *
 * 场景: 视频观看计费 score = f(userType, videoQuality, duration, ...)
 * Redis 不参与计算，只做持久化汇总。
 */
public class ComputedCounter {
    // 每个 Worker 线程一个本地计数器
    private final List<AtomicLong> localCounters;
    private final ThreadLocalRandom rng = ThreadLocalRandom.current();

    // ── 步骤1: 业务请求 → 本地计算 delta（不调 Redis） ──

    public void recordView(int workerId) {
        // 模拟一条业务请求参数
        long userId   = rng.nextLong(1, 10_000_000);
        long videoId  = rng.nextLong(1, 5_000_000);
        int quality   = rng.nextInt(4);                // 0=标清 1=高清 2=超清 3=4K
        int duration  = rng.nextInt(180);              // 观看时长(秒)
        boolean isVip = rng.nextDouble() < 0.1;        // 10% 用户是 VIP

        // 复杂业务计算（纯本地，纳秒级）
        int delta = computeWeight(userId, videoId, quality, duration, isVip);

        if (delta > 0) {
            localCounters.get(workerId).addAndGet(delta);  // 合并写入
        }
    }

    // ── 步骤2: 权重计算函数（可随业务灵活定制） ──

    static int computeWeight(long userId, long videoId,
                              int quality, int duration, boolean isVip) {
        if (duration < 5) return 0;        // 不足 5s 不算

        double weight = 1.0;               // 基础分

        if (isVip) weight *= 2.0;          // VIP ×2

        switch (quality) {                  // 清晰度加成
            case 1:  weight += 1.0; break;  // 高清 +1
            case 2:  weight += 2.0; break;  // 超清 +2
            case 3:  weight += 3.0; break;  // 4K   +3
        }

        if (duration >= 120) weight += 5.0;       // 完整看完 +5
        else if (duration >= 60) weight += 2.0;    // 看完一半 +2

        return (int) Math.round(weight * 100);     // ×100 定点数
        /*
         * 为什么 ×100？
         *   1. Redis 仅支持整数 INCR/INCRBY（不支持浮点增量）
         *   2. 定点数编码：weight=1.5 → 存储为 150
         *   3. 读取时还原：GET 返回 150 → /100.0 → 1.5（分）
         *
         *   生产示例:
         *     long rawVal = Long.parseLong(redis.get(key));  // 150000
         *     double actual = rawVal / 100.0;                // 1500.0 分
         */
    }

    // ── 步骤3: 定时批量写回 Redis ──

    @Scheduled(fixedRate = 1000)
    public void flushToRedis() {
        // 汇总所有 Worker 线程的本地计数器
        long totalDelta = localCounters.stream()
                .mapToLong(AtomicLong::getAndSet)
                .sum();

        if (totalDelta > 0) {
            // 1 次 Redis 调用，写入全量增量
            redis.incrby("hotkey:computed:total", totalDelta);
        }
    }
}
```

**与传统方案对比**：

| | 传统 Redis INCR | COMPUTED 方案 |
|:--|:--|:--|
| **每次请求调 Redis** | 1 次 INCR | 0 次 |
| **计算位置** | 无（纯自增） | 应用层（完整业务逻辑） |
| **Redis 写入频率** | 35 万次/秒 | 1 次/秒 |
| **吞吐** | ~359k cmd/s | ~95M cmd/s |
| **扩展性** | 受单 Redis 节点限制 | 水平扩展应用即可 |

> **适用场景**：任何可以"本地计算增量 + 周期性汇总"的计数场景。
> 不适用：需要精确原子性、实时去重等必须读 Redis 当前值的场景（此时用 Lua + BloomFilter 前置）。

### 8.11 生产级计算策略决策树

```
                  ┌─────────────────────────────────┐
                  │ 你的计数场景需要什么？            │
                  └─────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        纯自增(+1)        复杂计算         需要读当前值
              │         delta 本地可算       (实时去重/限流)
              │               │               │
              ▼               ▼               ▼
    ┌─────────────┐  ┌─────────────┐  ┌──────────────┐
    │ LOCAL_      │  │ COMPUTED    │  │ Lua + Bloom  │
    │ BUFFERED    │  │ 批量 INCRBY │  │ Filter 前置  │
    │ ~34M cmd/s  │  │ ~95M cmd/s  │  │ ~10万 cmd/s  │
    └─────────────┘  └─────────────┘  └──────────────┘
            │               │               │
            ▼               ▼               ▼
    最终一致 ≤1s    最终一致 ≤1s        强一致/原子性

  需要可实时读取？
    ├─ 是: SHARDED 分片 + MGET 聚合 (低吞吐) 
    └─ 否: LOCAL_BUFFERED / COMPUTED (高吞吐)
```

### 8.12 核心结论

- **热点 Key 是 Redis Cluster 的单点瓶颈**，因为 `slot = CRC16(key) % 16384` 将同一个 key 固定映射到同一个 slot → 固定映射到某个 master（该映射与集群拓扑无关，仅由 key 文本决定）。
- **高频对同一 key 的 INCR/INCRBY 会把所有写请求路由到同一 master**，导致：
  - 该 master CPU/线程成为瓶颈（Redis 为单线程执行写命令）；
  - 复制（replication）压力成倍增长：每条写命令都必须发送给所有 replicas → replication backlog 被覆盖 → psync 失效 → 触发全量同步 → BGSAVE/网络带宽/主进程 COW 爆发 → 业务延迟尖刺或超时。
- **五种解决方案按"Redis 写入压力 / 吞吐 / 可读性"权衡**：
  - **SINGLE_KEY**（直接 INCR 每次）—— Redis 写入与复制压力最高，吞吐受单节点限制（示例：本测 ~0.36M cmd/s），不推荐用于高并发热点。
  - **DISTRIBUTED**（随机 hashtag 后缀）—— 写入分散到多 slot，降低单点压力，但无法对多个后缀做聚合读取（不可 MGET 聚合），适用于只写少读的场景。
  - **SHARDED**（固定 N 分片，随机写入分片）—— 写入分布且可通过 MGET 聚合实时读取；代价是略低的写入吞吐和 MGET 的聚合开销（需合理选 N）。
  - **LOCAL_BUFFERED**（本地 Atomic counters + 定期 INCRBY）—— 将高频写转为纯内存操作并批量写回 Redis（如 1 次/s），极大缓解 Redis 负载并保留最终一致性（延迟窗口 ≤ 刷写周期）。
  - **COMPUTED**（本地业务计算后批量写回）—— 复杂计算在应用层完成并批量写回，能进一步降低原子竞争与系统开销，适合需要业务加权的计数场景。
- **关键工程原则**：
  - "计算在应用层，汇总写回 Redis" 是优先策略，尤其适合可容忍最终一致性的业务（如曝光计数、PV/UV 聚合等）。
  - 需要强一致或实时去重的场景，仍需用 Lua 脚本 / WAIT / 单点原子操作（会牺牲吞吐）。
  - 针对全量同步风险，应采取三层防护：基础设施（repl_backlog、client-output-buffer-limit、repl-diskless-sync、lazyfree 等）、应用层（自动降速/流控）与运维层（错峰重启、告警）。
- **监控与诊断要点**：
  - 观察 `INFO replication` 与 `INFO stats`（`master_link_status`、`master_repl_offset`、`sync_partial_ok`、`sync_partial_err`、`instantaneous_output_kbps`、`rdb_last_cow_size`、`latest_fork_usec`、`used_memory_rss` 等）。
  - 使用 `CLUSTER KEYSLOT key` / `CLUSTER SLOTS` / `redis-cli --hotkeys` 或客户端埋点判断 key 热点。
  - 在自动降速/限流策略中加入平滑与告警，避免抖动引起误降速。

### 8.13 停止

```bash
curl -X POST "http://localhost:8082/hotkey/stop"
```

---

## 场景 8.14: 主从同步压测 — 管道高写入 + 从库全量重同步

### 8.14.1 背景与问题

```
生产事故场景:
  业务高峰期，Pipeline 持续高吞吐写入主节点 →
  从节点因网络抖动/重启/升级触发全量 PSYNC →
  主节点 fork 子进程生成 RDB + 发送 RDB 到从节点 →
  主节点输出带宽瞬间打满 →
  正常业务 Pipeline 写入被阻塞 → 延迟尖刺 → 客户端超时雪崩

  ┌──────────┐  高吞吐 Pipeline 写入        ┌──────────┐
  │  应用层   │ ──────────────────────────→ │  主节点   │
  │ (8 worker)│ ←── 延迟从 2ms → 80ms ──── │ (Master) │
  └──────────┘                              └────┬─────┘
                                                 │ RDB 全量传输
                                                 │ + 增量命令流
                                                 ▼
                                           ┌──────────┐
                                           │  从节点   │
                                           │ (Replica) │
                                           └──────────┘
```

**这是比热点 Key 更隐蔽的连锁故障——不会报错，但业务延迟会从 2ms 恶化到 80ms+，且会持续数秒到数十秒。**

### 8.14.2 全量同步触发条件

从节点在以下情况必须走全量同步（RDB 完整传输），无法使用增量 PSYNC：

| 触发条件 | 原因 |
|:--|:--|
| 从节点重启 | 本地 replication ID 丢失 |
| replication backlog 不足 | 主节点写入太快，backlog 被覆盖 |
| 主节点重启/切换 | replication ID 变更 |
| `REPLICAOF NO ONE` 再 `REPLICAOF` | 复制状态被主动丢弃 |
| 网络长时间断开 | backlog 大小不足以覆盖断连期间的写入 |

> **关键公式**：replication backlog 能"兜住"断连时间的上限：
> ```
> repl_backlog 是循环字节缓冲（按字节衡量，不是按命令条数），
> 长度决定能回放的字节窗口大小。
> 
> 最大断连时间 ≈ repl_backlog_size / 主节点平均写入字节速率(bytes/s)
> 
> 示例（基于本测试环境假设）:
>   写入速率 ≈ 10KB value × 200 batch × 8 workers × ~30 batches/s
>            ≈ 48 MB/s（含命令包头估算）
>   256MB backlog → 最大断连 ≈ 5.3 秒
>   512MB backlog → 最大断连 ≈ 10.6 秒
> ```
> 断连超过此时间 → 必然全量同步。实际速率取决于 key/value 大小及并发度，生产环境需按实测写入字节率推算。

### 8.14.3 压测架构

```
                    ┌──────────────────────────────────────┐
                    │      主节点 (Master)                   │
                    │  redis-repl-master:6379 (容器内)      │
                    │  Port 6381 (宿主机映射)                │
                    │  repl-diskless-sync yes               │
                    │  repl-diskless-sync-delay 5           │
                    │  repl-diskless-load swapdb            │
                    │  client-output-buffer-limit           │
                    │    replica 512mb 128mb 60             │
                    │  lazyfree-lazy-eviction/expire yes    │
                    └──────────┬───────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │ RDB + cmd stream │                  │ Pipeline SETEX
            ▼                  │                  │ × batchSize=200
     ┌──────────────┐          │                  │ concurrency=8
     │   从节点      │          │                  │
     │ redis-repl-  │          │          ┌──────────────┐
     │ replica:6379 │          │          │  压测应用     │
     │ Port 6380    │          │          │ (8082 REST)  │
     │ repl-timeout │          │          │ 双客户端:     │
     │   120        │          │          │  masterConn  │
     └──────────────┘          │          │  replicaConn │
                               │          │  infoConn    │
              监控 INFO replication/stats memory/persistence
```

### 8.14.4 实测数据：三轮逐步演进

> **⚠ 环境声明**：以下数值为 Docker 单机测试所得，硬件/网络条件与真实分布式环境差异显著（Docker bridge 延迟 < 0.1ms，无真实网络带宽瓶颈）。数值仅供参考方法有效性，不应直接作为生产容量规划的绝对值。

**轮次设计**：R1 初始参数暴露问题 → R2 基础设施修复验证 → R3 应用层自动降速根除

| 参数 | R1 (无修复) | R2 (基础设施修复) | R3 (+自动降速) |
|:--|:--|:--|:--|
| value-size | 10KB | 10KB | 10KB |
| batchSize | 200→50 | 200 | 200 |
| concurrency | 8 | 8 | 8 |
| repl-backlog-size | 256MB | **512MB** | 512MB |
| client-output-buffer-limit | 0 0 0 (无限) | **512mb 128mb 60** | 512mb 128mb 60 |
| repl-diskless-sync-delay | 0 | **5** | **5** |
| repl-diskless-load | — | **swapdb** | **swapdb** |
| lazyfree-lazy-eviction/expire | — | **yes** | **yes** |
| 应用层自动降速 | — | — | **200→25 batch** |

#### 8.14.4.1 三轮实测数据对比

| 指标 | R1 (无修复) | R2 (基础设施修复) | R3 (+自动降速) | 改善幅度 |
|:--|:--|:--|:--|:--|
| **Pipeline 总命令数** | 1.52M | 1.05M | 868k | — |
| **Pipeline 总错误数** | **681** | 319 | **0** ✅ | ↓ 100% |
| **HTTP 超时率** | **30%** (6/20) | 0% | **0%** ✅ | 根除 |
| **COW** | **2.94 GB** | 392 KB | 392 KB | ↓ 7500× ✅ |
| **BGSAVE 耗时** | **285s** (未完成) | 9s (1次完成) | 14s (1次完成) | ↓ 31× ✅ |
| **Fork 停顿** | 205ms | 129ms | 156ms | ↓ 25~37% |
| **全量同步尝试** | 5 次 | 9 次 | 1 次 | ↓ 5~9× ✅ |
| **sync_partial_err** | 5 (全部失败) | 8 | 0 ✅ | 根除 |
| **全量同步完成** | 0 次 | 1 次 | 1 次 | — |
| **evicted_keys** | 大量驱逐 | 0 | 0 ✅ | 根除 |
| **used_memory_rss** | ~5GB (估算) | 4.20GB | — | ↓ ~16% |
| **expired_stale_perc** | 24.98% | — | — | — |

### 8.14.5 核心风险现象（实测验证）

#### 现象 1：全量同步死循环 — "同步-断连-再同步" 无限循环（R1）

```
┌─────────────────────────────────────────────────────────────────┐
│  1. Pipeline 持续写入 10KB value → 内存快速增长至 2GB            │
│  2. 触发全量同步 → master fork BGSAVE (205ms 停顿)               │
│  3. BGSAVE 生成 2GB RDB，耗时 285 秒                             │
│  4. COW 爆炸至 2.94GB → RSS 逼近物理内存                         │
│  5. Pipeline flush + RDB 发送 → 总输出带宽 38 Mbps               │
│  6. 从节点 sync_read_bytes 超过 master buffer limit → 断连       │
│  7. 从节点重连 → 再次触发全量同步 → 回到步骤 2                    │
│  8. 重复 5 次 → 5 次 sync_partial_err                            │
│                                                                  │
│  后果: 业务 HTTP 超时 30% + 681 命令级错误 + 从节点永久无法同步    │
└─────────────────────────────────────────────────────────────────┘
```

实测 Redis 内部指标：

| 指标 | 值 | 含义 |
|:--|:--|:--|
| `sync_full` | 5 | 5 次全量同步尝试 |
| `sync_partial_err` | 5 | **全部失败** |
| `expired_keys` | 171 万 | TTL 120s 致大量 keys 自然过期 |
| `expired_stale_perc` | 24.98% | 1/4 采样 keys 已是过期的 |

#### 现象 2：主节点输出带宽瞬间打满

```bash
# R1 全量同步期间（主节点 INFO stats）
instantaneous_output_kbps: 38000    # 38 Mbps
instantaneous_output_repl_kbps: 27584  # 复制流独占 27.6 Mbps
# 复制流占主节点输出带宽的 72.6%
```

#### 现象 3：COW 放大 — 本质是写量 × 子进程存活时间

**核心公式**：`COW ∝ (主进程在子进程存在期间的写量) × (子进程存活时间 = BGSAVE 耗时)`

R1 的 COW=2.94GB → R2 的 COW=392KB 的根本原因：

```
R1: BGSAVE 285s → 长时间内持续写入累积 → COW = 2.94 GB
R2: BGSAVE 9s    → 同写入速率仅 9 秒累积 → COW = 392 KB

仅 BGSAVE 耗时差 (285s / 9s ≈ 32×) 就几乎解释了 COW 的全部差异
```

> **关于 THP**：Docker Desktop Linux 引擎默认 THP 模式为 `madvise`（非 `always`），此模式下仅应用显式调用 `madvise(MADV_HUGEPAGE)` 才会用大页——Redis (jemalloc) 不会主动请求。THP 不是本场景 COW 变化的主因；降低 BGSAVE 耗时（或降低同步期间的写入速率）才是控制 COW 的核心手段。

**那 BGSAVE 为什么从 285s 骤降到 9s？**

| 修复项 | 机制 | 影响 |
|:--|:--|:--|
| **client-output-buffer-limit 设上限** | R1 无限制 → buffer 无限膨胀 → 内存/I/O 争抢 → 拖慢 BGSAVE | 从 285s → 9s 的核心因素 |
| **repl-diskless-sync-delay 0→5** | 给 kernel I/O scheduler 缓冲时间，避免 RDB 和复制流同时抢磁盘 | 间接加速 |
| **lazyfree 系列** | eviction/expire 异步释放，减少主线程阻塞 | 间接加速 |

#### 现象 4：replication backlog 被高写入冲掉

```bash
# R1: 256MB backlog
写入速率 ≈ 10KB × 200 batch × 8 workers × 30 batches/s ≈ 48 MB/s
最大断连时间 ≈ 256MB / 48MB/s ≈ 5.3 秒
→ 从节点断连超过 5.3s → 必然全量同步

# R2/R3: 512MB backlog
最大断连时间 ≈ 512MB / 48MB/s ≈ 10.6 秒
→ 窗口扩大 2 倍，但仍不够覆盖 BGSAVE 耗时
```

### 8.14.6 根除方案：三层防御体系

经过三轮迭代验证的最优组合：

| 层级 | 方案 | 实现 | 效果 |
|:--|:--|:--|:--|
| **基础设施层** | 扩大 repl-backlog-size | 256MB → **512MB** | 断连窗口扩大 2 倍 |
| **基础设施层** | 设 client-output-buffer-limit 合理上限 | `512mb 128mb 60` | 防止 buffer 无限膨胀拉垮 BGSAVE |
| **基础设施层** | repl-diskless-sync-delay 延时 | 0 → **5** | I/O 调度缓冲，避免同时争抢 |
| **基础设施层** | lazyfree 系列 | eviction/expire/server-del yes | 异步释放内存，减少主线程阻塞 |
| **基础设施层** | repl-diskless-load swapdb | 先加载到临时 DB 再 swap | 零停机完成同步 |
| **应用层** | 全量同步期间**自动降速** | batch 200→**25** | 写入速率降为 1/8，彻底保护 backlog |

#### Docker Compose 最终配置

```yaml
# docker-compose.yml — replication profile
redis-repl-master:
  command: >
    redis-server
    --save ""
    --appendonly no
    # ⚠ 关闭持久化仅用于压测/可恢复测试环境；生产中应谨慎或让 replica 做持久化
    --maxmemory 8gb
    --maxmemory-policy allkeys-lru
    --repl-backlog-size 512mb                          # ★ 512MB
    --repl-diskless-sync yes
    --repl-diskless-sync-delay 5                       # ★ 延迟 5s
    --repl-diskless-load swapdb                        # ★ swapdb
    --client-output-buffer-limit "normal 0 0 0 replica 512mb 128mb 60 pubsub 0 0 0"  # ★ 512mb/128mb/60
    --lazyfree-lazy-eviction yes
    --lazyfree-lazy-expire yes
    --lazyfree-lazy-server-del yes

redis-repl-replica:
  command: >
    redis-server
    --replicaof redis-repl-master 6379
    --replica-read-only yes
    --save ""
    --appendonly no
    # ⚠ 同上：关闭持久化仅在 bench 环境使用；生产 replica 应开启 RDB/AOF 作为持久化兜底
    --repl-timeout 120
    --client-output-buffer-limit "normal 0 0 0 replica 512mb 128mb 60 pubsub 0 0 0"
```

### 8.14.7 应用层自动降速 — 最终防线

#### 设计思路

```
全量同步死循环根因:
  写入速率 > backlog 容量 / BGSAVE 耗时
  → backlog 被冲掉 → replica 永远追不上

解决:
  检测到 replica link=down 且 resync 已触发 → 自动降低 batch size
  replica link=up → 自动恢复全速

实现:
  每 5 秒查询 lastMasterLinkStatus (缓存在 Java 内存)
  → down + resyncTriggerCount>0 → effectiveBatchSize = 25
  → up   + syncThrottled=true    → effectiveBatchSize = 200
```

> **⚠ 鲁棒性提醒**：基于 `master_link_status` 的单次瞬时值做降速/恢复判断存在误触发风险——短暂网络抖动可能导致频繁降速/恢复震荡。生产落地建议：
> - 降速触发：`link=down` 持续 ≥ N 秒（如 5s）且 `sync_partial_err` 递增或多于阈值后降速。
> - 恢复条件：`link=up` 且稳定持续 ≥ M 秒（如 10s）后逐步恢复（可阶梯式恢复，非一次性回到全速）。
> - 记录降速事件并发出 P1 告警，保留降速日志供事后分析。

#### 核心代码

```java
@Value("${app.repl-bench.sync-throttle-batch-size:25}")
private int syncThrottleBatchSize;

private volatile int effectiveBatchSize;   // 动态变化的实际 batch
private volatile boolean syncThrottled;    // 是否处于降速状态

// Worker 方法中直接引用 effectiveBatchSize
private void runBatch(RedisAsyncCommands<String, String> async, String value) {
    int bSize = effectiveBatchSize;  // 可能被 adjustThrottle 动态修改
    List<RedisFuture<String>> futures = new ArrayList<>(bSize);
    for (int i = 0; i < bSize; i++) {
        futures.add(async.setex(key, ttl, value));
    }
    async.flushCommands();
    LettuceFutures.awaitAll(Duration.ofSeconds(2), futures.toArray(new RedisFuture[0]));
}

// 每 5 秒检测一次，动态调节
private void adjustThrottle() {
    boolean isDown = "down".equalsIgnoreCase(lastMasterLinkStatus);

    if (isDown && resyncTriggerCount > 0 && !syncThrottled) {
        effectiveBatchSize = syncThrottleBatchSize;   // 200 → 25
        syncThrottled = true;
        log.warn("[自动降速] batch {} → {} (每 worker)", original, effectiveBatchSize);
    } else if (!isDown && syncThrottled) {
        effectiveBatchSize = batchSize;               // 25 → 200
        syncThrottled = false;
        log.info("[恢复全速] batch {} → {}", original, effectiveBatchSize);
    }
}
```

#### 配置文件

```yaml
# application.yml
app:
  repl-bench:
    batch-size: 200
    sync-throttle-batch-size: 25       # 全量同步期间降至 25
    master-internal-host: redis-repl-master   # Docker 内部 hostname
    master-internal-port: 6379                # 容器内端口（非宿主机映射）
```

### 8.14.8 开发踩坑记录

在 Docker 环境下触发 REPLICAOF 有两个容易忽视的 Bug：

| Bug | 原因 | 修复 |
|:--|:--|:--|
| REPLICAOF 用 `localhost` | 命令发到从容器内，`localhost`=容器自身 → 无法连接 true master | 新增 `master-internal-host: redis-repl-master` |
| REPLICAOF 用宿主机端口 `6381` | 容器内 master 监听 `6379`，宿主映射 `6381` 在容器内不可见 | 新增 `master-internal-port: 6379` |

### 8.14.9 REST 端点

```bash
GET  /replication/metrics       — 当前指标 + 主从状态快照
POST /replication/start         — 启动 Pipeline 写入 (?batchSize=200&concurrency=8)
POST /replication/stop          — 停止
POST /replication/resync        — 触发从节点全量重同步 (REPLICAOF NO ONE → REPLICAOF)
POST /replication/replica/restart — 重启从节点容器 (docker restart)
```

**关键参数说明**：

| 参数 | 含义 | 默认值 | 调节方向 |
|:--|:--|:--|:--|
| `batchSize` | 每批 SETEX 命令数 | 200 | 加大 → 更高吞吐 |
| `concurrency` | Worker 线程数 | 8 | 加大 → 更高带宽压力 |
| `valueSize` | 每条 value 字节数 | 256 | 加大 → 更快占满带宽 |
| `keySpace` | 随机 key 范围 | 500k | 缩小 → 更多覆盖写 → 内存更紧凑 |
| `sync-throttle-batch-size` | 全量同步期间降速后的 batch | 25 | 更小 → 更保护 backlog |

### 8.14.10 测试方法

```bash
# 步骤 1: 启动主从环境
docker-compose --profile replication up -d redis-repl-master redis-repl-replica

# 步骤 2: 启动压测应用
mvn spring-boot:run -f redis-lettuce-bench/pom.xml

# 步骤 3: 启动 Pipeline 写入
curl -X POST "http://localhost:8082/replication/start?batchSize=200&concurrency=8"

# 步骤 4: 预热 15s 后触发全量同步
curl -X POST "http://localhost:8082/replication/resync"

# 步骤 5: 实时观测（自动降速日志）
tail -f app.log | grep 自动降速

# 步骤 6: 停止
curl -X POST "http://localhost:8082/replication/stop"
docker-compose --profile replication down
```

#### 自动化压测脚本（推荐）

```powershell
# loadtester/replication-bench.ps1 — 6 步全自动脚本
.\loadtester\replication-bench.ps1 -WarmupSec 15 -StableWaitSec 15
```

### 8.14.11 观测维度

| 维度 | 指标 | 来源 | 理想值 |
|:--|:--|:--|:--|
| **带宽** | `instantaneous_output_kbps` | INFO stats | 基线 < 50Mbps, 同步期间 < 200Mbps |
| **复制** | `master_link_status` | INFO replication | up |
| **同步进度** | `master_sync_in_progress` | INFO replication | 0 (同步完成) |
| **COW** | `rdb_last_cow_size` | INFO persistence | < used_memory × 0.5 |
| **Fork** | `latest_fork_usec` | INFO stats | < 100ms |
| **内存** | `used_memory_rss` | INFO memory | < 物理内存 80% |
| **淘汰** | `evicted_keys` | INFO stats | 0 |
| **错误** | `totalErrors` | 客户端 | **0** |
| **HTTP超时** | 监控端采集 | 客户端 | **0%** |

### 8.14.12 生产部署建议

```
┌─────────────────────────────────────────────────────────────────────┐
│              主从全量同步防护体系 — 三层防御                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  第一层：基础设施防护（不依赖业务代码）                                 │
│    ☐ repl-backlog-size ≥ 写入速率(MB/s) × 预期最长断连时间(s) × 1.5  │
│    ☐ client-output-buffer-limit 设合理上限: 512mb 128mb 60          │
│    ☐ repl-diskless-sync yes + delay=5                               │
│    ☐ repl-diskless-load swapdb                                      │
│    ☐ lazyfree-lazy-eviction/expire/server-del yes                   │
│    ☐ 持久化策略：压测环境可关闭持久化，生产由 replica 负责 RDB/AOF     │
│      （主节点关闭持久化仅在数据可接受丢失或短期基准测试中使用）         │
│                                                                      │
│  第二层：应用层防护（强烈推荐）                                        │
│    ☐ 全量同步期间自动降低写入速率（batch/并发减半或更多）               │
│    ☐ 检测 master_link_status → down → 触发降速                      │
│    ☐ 检测 master_link_status → up   → 自动恢复全速                   │
│    ☐ 关键写入加 WAIT n timeout 确保至少 n 副本确认                    │
│                                                                      │
│  第三层：运维防护                                                     │
│    ☐ 从节点错峰维护（低峰期重启/升级）                                │
│    ☐ 至少 2 个持久化副本                                             │
│    ☐ 监控告警: link=down 超过 X 秒 → P1 告警                        │
│    ☐ 监控告警: bg_save 耗时 > 10s → P2 告警                         │
│    ☐ 监控告警: latest_fork_usec > 100ms → P2 告警                   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.14.13 停止

```bash
curl -X POST "http://localhost:8082/replication/stop"

# 停止主从容器
docker-compose --profile replication down
```

### 8.14.14 监控指标建议

以下指标建议纳入监控系统（如 Prometheus/Grafana），并设置告警阈值：

| 分类 | 指标 | 来源 | 告警建议 |
|:--|:--|:--|:--|
| **复制状态** | `master_link_status` | INFO replication | down > 5s → P1 |
| **复制积压** | `repl_backlog_size` / `repl_backlog_first_byte_offset` / `master_repl_offset` / `repl_backlog_histlen` | INFO replication | backlog_histlen > 80% backlog_size → P2 |
| **持久化** | `rdb_last_bgsave_status` / `rdb_last_cow_size` / `latest_fork_usec` | INFO persistence | bgsave fail → P1; COW > used_memory × 0.5 → P2; fork > 100ms → P2 |
| **吞吐/带宽** | `instantaneous_ops_per_sec` / `instantaneous_input_kbps` / `instantaneous_output_kbps` | INFO stats | 突变 ±50% vs 基线 → P2 |
| **内存** | `used_memory` / `used_memory_rss` / `mem_fragmentation_ratio` | INFO memory | RSS > 物理内存 × 0.8 → P1; frag_ratio > 2 → P2 |
| **慢查询** | `slowlog` | SLOWLOG GET | 新增 > 0 条/min → P2 |
| **持久化重建** | `aof_rewrite_in_progress` | INFO persistence | rewrite 耗时 > 30s → P2 |
| **复制延迟** | replica 侧 offset lag | INFO replication on replica | lag > 1MB → P2 |

> 建议使用 `redis-cli --latency-history` 或 `redis-cli --intrinsic-latency` 辅助诊断延迟异常。

### 8.14.15 自动降速设计规范（生产级）

在 8.14.7 的应用层降速基础上，生产环境应增加平滑逻辑：

- **降速触发条件**（防止短暂抖动误触发）：
  ```
  master_link_status=down 且持续 ≥ 5s
  AND (sync_partial_err 递增 OR repl_backlog_histlen 持续 > 80% backlog_size)
  AND 重试计数 ≥ 3
  ```
- **恢复条件**（防止反复震荡）：
  ```
  master_link_status=up 且稳定持续 ≥ 10s
  → 阶梯式恢复（如有效 batch: 25 → 50 → 100 → 200，每步间隔 5s）
  ```
- **记录与告警**：
  - 每次降速/恢复事件记录日志（时间、触发指标、降速幅度）
  - 发出 P1 告警通知人工介入
  - 保留降速窗口内的详细指标快照供事后分析

### 8.14.16 repl-diskless-sync 注意事项

| 优势 | 风险 |
|:--|:--|
| 减少磁盘 I/O（不写临时 RDB 文件） | 消耗主节点内存（在内存中准备 RDB） |
| 无磁盘空间争抢 | 主节点网络带宽被大量占用（通过 socket 发送 RDB） |
| `swapdb` 模式：先加载到临时 DB 再 swap，零停机 | 内存占用翻倍（旧 DB + 新 DB 同时存在） |

**适用**：内存充裕（RDB 大小 < 可用内存 × 0.5）、网络带宽充足的环境。
**不适用**：内存紧张或网络带宽瓶颈环境（应改用 disk-based sync + SSD）。

### 8.14.17 持久化与数据安全性警示

- 本文 Docker Compose 配置中 **`--save ""` / `--appendonly no`** 仅适用于 bench/可恢复测试环境。
- **生产环境绝不应随意关闭持久化**，除非：
  - 数据可接受丢失（如纯缓存场景），或
  - 外部存储（如 Kafka/DB）已做替代持久化方案。
- 生产建议：让 **replica 节点负责持久化**（RDB 定时 + AOF），主节点可不开启以减少 fork 开销。
- 若主节点必须持久化，确保 RDB 间隔合理，并监控 `latest_fork_usec` 与 COW。

### 8.14.18 额外可选架构 — 极端高并发计数

对于 Redis 单节点无法承载的极端高并发计数，可考虑：

1. **外部聚合存储**
   ```
   应用层 → Kafka（计数事件）→ 消费者批量聚合 → ClickHouse / Timeseries DB
   Redis 仅做二级缓存（短期热数据，定时从 ClickHouse 回填）
   ```

2. **Client-side sharding + 并行聚合**
   ```
   写入: 将热点 key 拆为 N 个逻辑分片 → 每个分片落到不同 slot/节点
   读取: 并行查询 N 个分片 → 客户端 merge 求和
   优于 MGET（避免单次 MGET 阻塞等待最慢的分片）
   ```

3. **CRDT / Redis Modules**
   ```
   使用 RedisGears 或 Redis Stack 的 CRDT 计数器（如 RedisBloom CountMin Sketch）
   在 Redis 内部实现近似聚合，减少应用层复杂度
   ```

> **选型原则**：复杂计算在应用层完成 → 批量 INCRBY 写回 Redis / 外部存储。让 Redis 只做"汇总持久化"，不做"实时原子计算"。

