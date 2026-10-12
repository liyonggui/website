---
title: "场景 4 · 分布式信号量 — 多 Writer 竞争全局 Pipeline 槽位最优值探测"
summary: "问题: 单服务压测最优 8 个 pipeline 槽位，生产 12 个 Java 服务同时写 Redis。 如果每服务 8 个 pipeline → 96 个并发管道，超出 Red…"
date: 2026-06-21
author: "小番茄程序猿"
tags:
  - Redis
  - 缓存
category: deep
series: "redis"
slug: "场景4-分布式信号量多Writer竞争Pipeline槽位"
reading_time: 28
featured: false
draft: false
---
## 场景 4: 分布式信号量 — 多 Writer 竞争全局 Pipeline 槽位最优值探测

---

### TL;DR

```
问题: 单服务压测最优 8 个 pipeline 槽位，生产 12 个 Java 服务同时写 Redis。
      如果每服务 8 个 pipeline → 96 个并发管道，超出 Redis 单实例承受能力。

目标: 用分布式信号量（Redis 全局计数/令牌），让 12 个 Writer 竞争有限槽位，
      遍历不同 slots 值，找到全局最优并发 pipeline 数。

结论: 8 个全局槽位是性价比最优值（cmd/s≈320k, P99≈4.6ms, retries=0）。
      实现上必须注意：① 只在成功占有槽位时释放（不能无条件 DECR）
      ② 用 token+TTL 防泄漏（不能盲目按阈值归零）③ 用 Lua 保证原子性
      ④ 避免 tight spin-wait，使用随机退避。

风险评估三轴: 吞吐 (cmd/s) / 延迟 (P99) / 公平性 (retries/starvation)
```

---

### 4.1 背景

```
问题: 压测测得单个服务最优为 8 个 pipeline 槽位，但生产有 12 个 Java 服务同时写入 Redis。
      如果每个服务都按 8 个 pipeline 打，就是 96 个并发管道，Redis 未必扛得住。

目标: 通过分布式信号量（基于 Redis 的全局计数/令牌），让 12 个 Writer 竞争有限槽位，
      遍历不同 slots 值，找到全局最优并发管道数。
```

### 4.2 实验设计

#### 4.2.1 测试环境（复现必需）

| 配置项 | 值 |
|:--|:--|
| Redis 版本 | 7.x（注明实际版本号） |
| Redis 实例规格 | CPU: X 核 Y GHz，内存: Z GB |
| Redis 配置 | `lazyfree-lazy-expire yes`, hz=10, effort=1 保持默认 |
| 网络 | 带宽: X Gbps，RTT: Y ms（ping 实测） |
| 客户端机型 | CPU: X 核，内存: Y GB，OS: Linux/Windows |
| JVM 参数 | -Xms2g -Xmx2g，GC: G1GC / ZGC |
| Writer 数 | 12（模拟 12 个 Java 服务） |
| 每个 batch | 200 条 SETEX |
| 命令类型 | `SETEX key TTL value` |
| TTL | 2s + rand(0,2s)（均匀随机打散） |
| key 空间 | 50 万 |
| 槽位遍历 | 1, 2, 4, 6, 8, 12, 16, 24 |
| 每轮时长 | 25s（含 5s 预热，20s 采样） |

> **说明**：绝对值（320k cmd/s 等）与测试环境强相关。不同命令类型、客户端/网络/Redis 硬件与配置均会影响结果。生产环境必须基于本环境实测数据做决策。

#### 4.2.2 精确测量指标与采样方法

| 指标 | 采集方法 | 说明 |
|:--|:--|:--|
| cmd/s | 客户端每 1s 统计成功命令数 | 排除预热期 5s |
| avg / P50 / P90 / P99 / P999 | HdrHistogram 或采样计算 | P999 反映极端尾部 |
| max 延迟 | 采样期最大值 | 关注原因（GC、切换、竞争） |
| acquire 成功率 | `acquire_success / total_acquire_attempts` | < 95% 时告警 |
| retries 次数 | INCR 超限后 DECR 退回 + 重试计数 | 反映竞争烈度 |
| 等待时长分位 | acquire 从尝试到成功的时间 | 与退避策略相关 |
| Redis CPU | `INFO cpu` 采样 | `used_cpu_sys + used_cpu_user` |
| 连接数 | `INFO clients` / `connected_clients` | 关注是否接近 maxclients |
| 网络 I/O | `INFO stats` / `total_net_input_bytes/output_bytes` | 计算带宽占用 |
| GC pause | JVM GC log / `jstat` | 排除 GC 对 max 的干扰 |

**采样方法：**
- 预热 5s 数据丢弃，只统计 20s 采样期
- 每轮（25s）后输出一次汇总报告，包含 min/avg/P50/P90/P99/P999/max/retries
- 如果可用，报告 95% 置信区间（多次运行取均值 ± 标准差）

### 4.3 核心机制（安全实现）

#### 4.3.1 推荐方案：持有者令牌（Token + TTL）

**优于纯 INCR/DECR 的原因：**
- INCR/DECR 在服务崩溃时泄漏计数，且无条件 DECR 会误减
- 令牌方案每个槽位有独立 key 带 TTL，崩溃自动回收

```
持有者令牌方案:
  - 全局计数键: pipeline:slots:used（维护当前占用数）
  - 令牌设计: pipeline:token:{uuid}  →  EXPIRE = Xs（如 30s，大于单次 flush 时间 × 3）
  - acquire: 用 Lua 原子性创建令牌 + 检查计数
  - release: DEL token + DECR count（仅在令牌存在时执行）
  - 兜底: token 超时 → Redis 自动删除 → 后台 reaper 修正计数
```

#### 4.3.2 Lua 原子 acquire 脚本（伪码）

```lua
-- acquire.lua
-- KEYS[1] = pipeline:slots:used (计数 key)
-- KEYS[2] = pipeline:token:{uuid} (令牌 key)
-- ARGV[1] = globalSlots (槽位上限)
-- ARGV[2] = token_ttl_seconds (令牌 TTL, 建议 30s)
-- ARGV[3] = token_value (持有者标识, 如 instanceId+threadId)
-- 返回: {success=1} 或 {success=0, current_count=count}

local count = redis.call('INCR', KEYS[1])
if count <= tonumber(ARGV[1]) then
    -- 成功占用
    redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[2])
    return {1, count}
else
    -- 超出限制，需要退回
    redis.call('DECR', KEYS[1])
    return {0, count - 1}
end
```

```lua
-- release.lua
-- KEYS[1] = pipeline:slots:used
-- KEYS[2] = pipeline:token:{uuid}
-- 返回: {released=1} 或 {released=0, reason="token_not_found"}

local exists = redis.call('EXISTS', KEYS[2])
if exists == 1 then
    redis.call('DEL', KEYS[2])
    local count = redis.call('DECR', KEYS[1])
    -- 防止 DECR 到负数（兜底保护）
    if count < 0 then
        redis.call('SET', KEYS[1], 0)
    end
    return {1, math.max(0, count)}
else
    return {0, "token_not_found"}
end
```

**关键原则：**
1. acquire 和 release 都用 Lua 保证原子性，避免 INCR+判断+DECR 之间的竞态窗口
2. **只在确实成功占有槽位时才 release**（令牌存在才 DEL + DECR）
3. **禁止在 finally 中无条件 DECR**——无条件 DECR 会把从未成功 acquire 的计数也减掉，导致计数器异常偏小甚至为负
4. release 中做下界检查（count ≥ 0），防止负数计数
5. 令牌 TTL 设为 flush 时间的 3~5 倍，既防止泄漏又避免误回收

#### 4.3.3 Writer 循环伪码

```java
public boolean tryFlushWithSemaphore(List<Command> batch) {
    String tokenId = UUID.randomUUID().toString();
    boolean acquired = false;

    try {
        // 1. 尝试 acquire（Lua 原子脚本）
        long startWait = System.currentTimeMillis();
        long maxWaitMs = 500;
        int retries = 0;

        while (System.currentTimeMillis() - startWait < maxWaitMs) {
            LuaResult result = executeLua(
                "acquire.lua",
                "pipeline:slots:used",
                "pipeline:token:" + tokenId,
                globalSlots, tokenTtlSeconds, instanceId
            );
            if (result.success) {
                acquired = true;
                break;
            }
            retries++;
            // 随机化退避，避免 sync wakeup
            long backoffMs = Math.min(10 + ThreadLocalRandom.current().nextLong(10), 200);
            sleep(backoffMs);
        }

        // 2. 仅当确实 acquire 成功才 flush
        if (!acquired) {
            log.warn("acquire timeout after {} retries", retries);
            return false;
        }

        executePipeline(batch);
        return true;

    } finally {
        // 3. 安全释放：只在真正持有令牌时才释放
        if (acquired) {
            executeLua("release.lua",
                "pipeline:slots:used",
                "pipeline:token:" + tokenId
            );
        }
    }
}
```

#### 4.3.4 边界条件处理

| 场景 | 处理方式 |
|:--|:--|
| **服务崩溃（未执行 release）** | token TTL 到期 → Redis 自动删除 → 后台 reaper 修正计数 |
| **acquire 超时（未拿到槽位）** | `acquired=false`，finally 中不执行 release |
| **重复 release** | release.lua 检查 token 是否存在，已释放则返回 `{0, "token_not_found"}` |
| **DECR 导致计数 < 0** | release.lua 中 `if count < 0 then SET 0` |
| **令牌被 TTL 误回收** | token TTL > flush 时间 × 3~5；若 flush 超时则客户端主动续约 TTL |

### 4.4 泄漏检测与修复策略

```
✗ 错误做法: 定时扫描 → 计数 > 2×total → 强制归零
  → 风险: 误判归零会杀掉活跃会话，造成正常 Writer 被清退

✓ 正确做法:
  方案 A (推荐): token+TTL + 后台 reaper
    - 每次 acquire 创建 token: pipeline:token:{uuid} → TTL=30s
    - 释放时 DEL token + DECR count
    - 后台 reaper (每 60s): 扫描 pipeline:token:* 集合
      → 对每个 token 检查 EXPIRE 时间，已过期但 count 未减 → 修复计数
    - 定期校验: 对比 SCARD(pipeline:tokens:set) 与 GET(pipeline:slots:used)，不一致则告警

  方案 B: 周期性一致性校验 (不做自动归零)
    - 脚本: SCARD(token_set) vs GET(count_key)
    - 若不一致且差距 > 阈值 → P1 告警，人工确认后修复
    - 触发条件: count > expected_max × 1.5 且 token_set_size != count
    - 人工修复: 找到孤儿 token DEL 掉并修正计数

  方案 C (兜底): 定时全量重置 (仅限非高峰期、确认无活跃 Writer 时)
    - 先 SET pipeline:slots:used 0
    - 再 SCAN + DEL 清理所有 pipeline:token:*
    - 必须人工确认 + 逐步执行
```

### 4.5 Sweep 原始数据

```
12 个 Writer 模拟 12 个 Java 服务，通过共享 Redis 竞争全局 Pipeline 槽位

slots │  cmd/s  │ avg ms │ P99 ms │ max ms │ retries │       现象
──────┼─────────┼────────┼────────┼────────┼─────────┼────────────────────
   1  │  112k   │  —     │  2.52  │ 28.48  │  40438  │ 极端竞争，大量重试/等待
   2  │  163k   │  —     │  3.58  │ 50.58  │  29368  │ 仍严重拥堵与等待
   4  │  160k   │  —     │  2.29  │ 27.57  │      0  │ 表面无重试但出现严重饿死（少数快 Writer 长期占位）
   6  │  307k   │  —     │  4.75  │ 57.14  │  14813  │ 吞吐显著提升，偶有等待
   8  │  320k   │  —     │  4.63  │ 23.10  │      0  │ ★ 无重试且吞吐峰值（性价比高）
  12  │  328k   │  —     │  4.38  │ 21.68  │      0  │ 吞吐略高（+2.5%），差异可忽略
  16  │  292k   │  —     │  4.20  │ 15.08  │      0  │ 并发太多导致连接/上下文切换开销反噬
  24  │  316k   │  —     │  3.55  │ 12.08  │      0  │ 边际收益递减，波动存在
```

> **注**：表中 avg ms 若可用建议补入。所有数值与测试环境有关：命令类型、客户端/网络/Redis 硬件与配置均影响绝对值。

> max 值差异原因：跨连接调度抖动、TCP 连接切换及 GC 可能导致峰值放大；排查点包括连接数、线程数、GC 限制、网络抖动与 Redis 后端 CPU 饱和。

### 4.6 三段现象分析

```
              吞吐
               ↑
           320k┼        ▄▄▄█▄▄▄▄
               │      ▄█         ▄█
           160k┼  ▄▄▄█
               │ █
           112k┼█
               │
               ├────┼────┼────┼────┼────┼──→ slots
                  1    2    4    6    8   12

阶段 1 (slots=1,2): 槽位严重不足 → 大量重试/等待（retries 很大）→ 吞吐大幅下降。
                    每个 Writer 大量等待释放时间 >> 实际写入时间。

阶段 2 (slots=4):   retries=0 并不等同于"无竞争"。实际表现为少数快速 Writer
                    持续抢到并释放槽位，长期占据导致其他 Writer 被饿死（公平性问题）。
                    换言之，这是一种典型的 convoy/starvation：占用-释放周期短使得慢 Writer 无机会，
                    整体吞吐被槽位数锁死在 160k。

阶段 3 (slots=6~12): 槽位充足，瓶颈转为 Redis 单实例处理能力 → 吞吐在 290k~328k 波动。
                    增加槽位/连接数到一定值后会带来连接开销/上下文切换，出现边际折损或波动。
```

### 4.7 关键发现

**1. slots=8 是性价比最高的点**

```
slots=8:  cmd/s=320k, P99=4.63ms, max=23.10ms, retries=0
slots=12: cmd/s=328k, P99=4.38ms, max=21.68ms, retries=0 (+2.5%)
```

从 8→12 只多挣了 2.5% 吞吐，多开 4 条连接和 4 个槽位，连接开销与运维复杂度上升。在本测试环境下，8 个全局槽位是较优折中（少数等待、较高吞吐、较低连接开销）。

**2. slots=4 的 "retries=0" 是假象**

```
slots=4, retries=0, 但吞吐只有 160k：
  → 少数快 Writer 快速循环占用并释放槽位
  → 慢 Writer 长期没机会 → 饿死（starvation）
  → 这是公平性问题，不是"无竞争"
```

**结论：retries=0 不等于无竞争，吞吐才是真正的健康指标。**
若需公平性，给每个 Writer 加入随机化退避、基于令牌的公平队列（FIFO）或按服务配额分配槽位。

**3. Redis 单实例处理上限 ≈ 320k cmd/s（在本测试条件下）**

```
测试条件: SETEX 批量 200 条/batch, TTL 2s, key 空间 50 万, 12 客户端
```

一旦 slots ≥ 6，吞吐在 290k~328k 之间波动，无法再突破。瓶颈从"槽位不足"转为"Redis 处理能力"（CPU、单核限速、网络栈、I/O）。
若需更高吞吐，应考虑水平扩展（分片/集群）、减少命令复杂度或批量化写入。

**4. slots > 12 反而可能吞吐下降**

```
slots=16: cmd/s=292k → 连接开销反噬
slots=24: cmd/s=316k → 略有恢复，但边际递减明显
```

过多并发连接带来的上下文切换、网络/线程开销超过并行收益，超过阈值后收益递减或反噬性能。

### 4.8 对比：单服务 8 管道 vs 12 服务竞争 8 槽位

| 指标 | 场景 1 单服务 8 pipeline | 场景 4 多服务竞争 8 槽位 |
|:--|:--|:--|
| 机制 | 连接内 pipeline | 跨连接分布式信号量 |
| P99 | ~5ms | 4.63ms |
| max | ~14ms | 23ms |
| 差异原因 | 连接内无竞争 | 跨连接有协调等待和调度抖动 |

分布式信号量的 max 略高（23ms vs 14ms），因为协调开销客观存在，但 P99 不受影响。

### 4.9 生产建议

**情况 A：服务数 ≤ 12（且每服务写入量中等）**
- 每个服务配置 `max-inflight-flushes=1`（或 ≤ 2），全局天然 ≤ 12 个并发管道，Redis 扛得住
- 无需分布式信号量（简单配置即可），但需监控 Redis 指标与延迟
- 如果单服务写入量很大，仅 1 个槽位可能不够，应根据写入量/延迟目标调整，或使用全局信号量做动态调优

**情况 B：服务数 > 12（或总并发可能 >> 12）**
- 使用分布式信号量，全局上限建议在 8~12（优先取 8），资源允许且追求极限吞吐时上调到 12
- 写入量小的服务可不开 pipeline 或仅使用 0~1 槽位

**情况 C：写入量差异大（少数重写者 + 多数轻写）**
- 按负载分配槽位：重写者分配 2~3 个槽位，轻写服务 0~1，确保总数控制在 8~12
- 动态配额比静态均分更公平也更高效
- 考虑实现权重机制，或把高写入流量路由到专门 Redis 集群/分片

### 4.10 退避与公平性策略

```
✗ 错误做法: sleep(10ms) 固定等待 → tight spin-wait 浪费 CPU
            → 多个 Writer 可能同步唤醒造成突发竞争

✓ 推荐: 随机化退避 + 指数上限
  基线: 10ms
  抖动: ThreadLocalRandom.current().nextLong(10)  (每次 +0~10ms 随机)
  指数增长: 10ms → 20ms → 40ms → 80ms → 160ms → 200ms (cap)
  总超时: 500ms (由业务 SLO 决定)
  重试次数: timeout 内按退避自动计算

公平性增强:
  - 按服务配额: 每个 serviceId 最多同时持有 N 个槽位
  - 令牌池 FIFO: 用 Redis List + BRPOP 实现公平排队 acquire
  - 权重分配: 重写者权重高，保证不被轻写者饿死
```

```java
// 带随机抖动的退避伪码
long baseSleepMs = 10;
long maxSleepMs = 200;
int attempt = 0;
long deadline = System.currentTimeMillis() + 500; // 总超时

while (System.currentTimeMillis() < deadline) {
    if (tryAcquire()) return true;
    attempt++;
    long sleepMs = Math.min(baseSleepMs * (1L << Math.min(attempt, 4)), maxSleepMs);
    sleepMs += ThreadLocalRandom.current().nextLong(sleepMs / 2); // 随机抖动
    Thread.sleep(Math.min(sleepMs, deadline - System.currentTimeMillis()));
}
return false; // timeout
```

### 4.11 监控与告警

#### 4.11.1 必监控项与告警阈值

| 监控项 | 来源 | 告警阈值 | 级别 |
|:--|:--|:--|:--|
| Redis P99 延迟 | 客户端 HdrHistogram | > 10ms 持续 1min | P1 |
| acquire 成功率 | 客户端计数器 | < 95% 持续 3min | P1 |
| retries/sec | 客户端重试计数 | > 100/s 持续 1min | P2 |
| 全局槽位计数 | `GET pipeline:slots:used` | > globalSlots × 1.5 | P1 泄漏告警 |
| 令牌集合大小 | `SCARD pipeline:tokens:set` | 与计数不一致超过 5min | P1 |
| Redis CPU | `INFO cpu` | > 80% 持续 1min | P1 |
| 连接数 | `INFO clients` | > maxclients × 80% | P1 |
| acquire 等待时长 P99 | 客户端计时 | > 100ms | P2 |
| GC pause | JVM GC log | > 50ms | P2 |

#### 4.11.2 Grafana 面板建议

```
面板 1 — Overview:
  - cmd/s (所有 Writer 合计)
  - P50 / P99 / P999 延时
  - acquire_success_rate (%)

面板 2 — 信号量健康:
  - pipeline:slots:used (Gauge, 预期范围 0~12)
  - pipeline:tokens:set_size (Gauge, 预期 = slots:used)
  - retries/sec (Rate)
  - 每服务 acquire_wait_ms 分位 (Histogram)

面板 3 — Redis 实例:
  - CPU % (used_cpu_sys + used_cpu_user)
  - connected_clients
  - total_net_input_bytes / output_bytes (Rate)
  - instantaneous_ops_per_sec

面板 4 — 公平性:
  - per_service_throughput (Bar chart)
  - per_service_acquire_success_rate (Heatmap, 检测饿死)
```

### 4.12 自动/半自动调优流程

```
方案: 低频自动 sweep（夜间/低峰期）

流程:
  1. 采集当前窗口 Redis 指标（CPU, cmd/s, P99 latency, retries）
  2. 如果 CPU < 60% 且 retries > 100/s → 尝试 slots + 2（上限 12）
  3. 如果 CPU > 85% 或 P99 上升 → 尝试 slots - 2（下限 6）
  4. 每次调整后观察 5min，回滚条件：P99 恶化 > 20% 或 retries 显著增长
  5. 结果写入 application.yml 并告警通知

手动 runbook:
  - 先查 Grafana 面板确认当前 slots 与指标
  - 在预发/灰度环境改 globalSlots 值（application.yml 热改）
  - 观察 10min 指标，确认无退化后推广到全量
```

### 4.13 安全与资源保护

```
防止恶意/误配置:
  1. 单服务 acquire 频率硬限制: 每 serviceId 每秒最多 acquire 10 次
  2. 单 client 槽位配额: 每 serviceId 最多同时持有 3 个槽位
  3. 全局 max-inflight-flushes 上限: 即使 globalSlots=12，单服务也不能超过 3
  4. 令牌 TTL 最小值: 不允许 < 10s，防止高频创建/过期导致 Redis CPU 浪费
  5. acquire 超时上限: 不允许 > 2s，防止客户端堆积
```

### 4.14 替代技术路径比较

| 方案 | 原理 | 适用场景 | 优缺点 |
|:--|:--|:--|:--|
| **分布式信号量 (本文)** | Redis 全局计数/令牌竞争 | 多服务、需全局协调、写入量差异大 | ✓ 公平、灵活 ✗ 实现复杂度中等 |
| 连接池限流 | 每个服务独立配置 `maxActive=N` | 服务数 ≤ 12、写入量均匀 | ✓ 简单 ✗ 总量不可控 |
| 客户端自身限流 | 单服务内部 RateLimiter | 单服务场景、写入量可预测 | ✓ 最简单 ✗ 无法跨服务协调 |
| Redis Streams / 工作队列 | 写入请求入队，Worker 消费 | 不需要实时写入、可缓冲 | ✓ 天然背压 ✗ 引入延迟 |

### 4.15 性能优化细节

```
1. Pipeline 内部 batch 合理: 200 条/batch 是本环境最优，生产需复测
2. 连接复用: 每个 Writer 保持一个长连接，不使用连接池
3. TCP keepalive: 开启 TCP_KEEPALIVE 防止空闲连接断开
4. 客户端批量序列化: 减少单命令序列化开销
5. pipeline:slots:used 使用 small key: 仅 value 为整数
6. Lua 脚本缓存: 使用 EVALSHA 替代 EVAL 减少网络开销
```

### 4.16 灾难恢复 / 回滚 Playbook

```
场景 1: 计数器异常（slots:used 持续 > globalSlots × 2 且 token_set_size 不符）
  1. 确认无活跃 token 异常（检查 token set 与 TTL）
  2. 手动 DEL 孤儿 token，修正 slots:used 计数
  3. 如果无法排查 → 低峰期人工逐步重置（非强制清零）

场景 2: Redis 负载飙升（CPU > 90%，P99 > 50ms）
  1. 紧急降低 globalSlots → 4（通过配置中心热改）
  2. 临时降级为重写者服务单 pipeline，轻写者停 pipeline
  3. 确认写入 SLO 恢复后逐步放宽

场景 3: 某个服务饿死（per_service_throughput = 0 持续 > 2min）
  1. 检查该服务重试次数与其他服务 flush 频率
  2. 如公平性问题 → 给该服务设置最低配额（min-slots=1）
  3. 如该服务自身问题 → 重启并限流排查
```

### 4.17 成本/运维权衡

```
当前方案 (8 槽位, 单 Redis):
  吞吐: ~320k cmd/s
  运维: 中等（需维护信号量 + 监控）
  成本: 1 Redis 实例

若需更高吞吐，扩容选项:
┌──────────────────────┬──────────┬──────────────────────────┐
│ 方案                  │ 吞吐预估  │ 运维复杂度                │
├──────────────────────┼──────────┼──────────────────────────┤
│ 单实例 + 优化 (baseline)│ ~320k   │ 低                       │
│ Redis Cluster (3 分片) │ ~900k   │ 高（需改造客户端/迁移数据）│
│ 专用写实例分离          │ ~500k   │ 中（双写逻辑）            │
│ Pipeline + 压缩         │ ~350k   │ 低（仅客户端改动）        │
│ 减少 SETEX 换用 HSET    │ ~400k   │ 中（数据模型改造）        │
└──────────────────────┴──────────┴──────────────────────────┘
```

### 4.18 总结

```
核心结论:
  分布式信号量能把多实例的并发写入限制在 Redis 承受范围内，
  8 个全局槽位在本实验中是性价比最优值。

实现铁律:
  1. 只在确实成功占有槽位时才释放（不能无条件 DECR）
  2. 使用 token+TTL 防泄漏（不能盲目按阈值归零）
  3. acquire/判断/释放 封装为 Lua 原子脚本
  4. 避免 tight spin-wait，使用随机化退避
  5. 监控吞吐+延迟+公平性三轴，配合自动化调优

评估三轴:
  吞吐 (cmd/s) — 延迟 (P99) — 公平性 (retries/starvation)
  三者缺一不可: retries=0 不表示无竞争，吞吐才是最终健康指标
```
