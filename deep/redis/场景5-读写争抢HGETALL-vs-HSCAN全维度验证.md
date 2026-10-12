---
title: "场景 5 · 读写争抢 — HGETALL vs HSCAN 全维度验证"
summary: "问题要点： - HGETALL 在大 Hash（field 数数千以上）时做 O(N) 操作，导致 Redis 单线程长时间阻塞，写请求在此期间全部排队并出现 P99 激增或超时。…"
date: 2026-06-29
author: "小番茄程序猿"
tags:
  - Redis
category: deep
series: "redis"
slug: "场景5-读写争抢HGETALL-vs-HSCAN全维度验证"
reading_time: 33
featured: false
draft: false
---
## 场景 5: 读写争抢 — HGETALL vs HSCAN 全维度验证

### TL;DR

**问题要点：**
- HGETALL 在大 Hash（field 数数千以上）时做 O(N) 操作，导致 Redis 单线程长时间阻塞，写请求在此期间全部排队并出现 P99 激增或超时。
- HSCAN 是增量遍历，不阻塞主线程（单次开销小），但返回不保证快照一致性，且 count 参数只是 hint（建议值）。

**原则性建议（优先级）：**
1. 优先在数据建模阶段避免大 Hash（建议单 key field 数 ≤ 1000，推荐更保守的 500 并复测）。
2. 禁止在生产直接对大 Hash 调用 HGETALL；新增调用必须经过 `RedisHashUtil` 封装并遵循阈值策略。
3. 默认 reader-mode = HSCAN（或 `hScanAllSafe`）+ count 参数可配置，且在目标环境复测最优值（经验值 200）。
4. 若明确需要全部字段且字段名已知，优先用 HMGET（按需取）以最小化影响。
5. 在迁移期或紧急场景，用双写 + HSCAN 迁移 + 灰度切换替代一次性 HGETALL。

**RedisHashUtil 推荐实现要点：**
- `hGetAll(key)`: 先 HLEN；若 HLEN < soft_threshold（经验 500），直接 HGETALL；若 soft_threshold ≤ HLEN < hard_threshold（经验 5000），记录 warn 并推荐 hScanAllSafe；若 HLEN ≥ hard_threshold，拒绝（RedisSafetyException）。
  - 说明：阈值以"估算总字节"优先，field-count 为快速判定。实现可 sample 部分 field 估算 avg_field_size。
- `hScanAllSafe(key, count)`: 在单连接内循环 hscan 直到 cursor=="0"；客户端合并并做去重（Set）；明确返回弱一致语义（并发写可能导致遗漏/重复）。

**Spring Data / 客户端注意：**
- 若需要可恢复的游标，不要依赖 Spring Data scan 的内部 Cursor（它通常从 0 开始自动迭代）；改用 Lettuce 原生 hscan 并显式传递 ScanCursor。
- SCAN 系列可能返回重复或漏行，客户端需去重或容忍弱一致性。

**监控与 CI：**
- 监控 HLEN Top-N、HGETALL slowlog、命令延时分布（latency-monitor）、hotkeys。
- CI 强制禁止直接 `opsForHash().entries()` / `opsForHash().values()` 调用，`RedisHashUtil` 作为唯一入口。

**Lua 补充：**
- Lua 脚本执行是整个脚本原子且会阻塞 Redis；不要在 Lua 中做大循环或 HGETALL/SMEMBERS 大 key 操作。
- Redis 7+ 支持 EVAL_RO，可以把只读逻辑发到只读副本执行（注意副本延迟）。

---

### 5.1 背景与问题

```
生产现象: Pipeline 批量写入 + 并发 HGETALL 大 Hash 遍历
         → 主线程被 HGETALL 长时间阻塞
         → 写请求 P99 剧烈抖动，大面积超时

核心矛盾: HGETALL 一次拉取全字段 = 1 次 RTT × 超长执行时间
         Redis 单线程意味着这期间所有写请求都在排队
```

### 5.2 实验设计

| 参数 | 值 |
|:--|:--|
| Redis | lazyfree-lazy-expire yes, hz=10 默认 |
| Writer | 6 线程, batch=200, TTL=2s+rand(0,2s) |
| Reader | 3 线程, 间隔 200ms |
| 读目标 | 5 个 big hash × **50,000 fields** |

### 5.3 四项验证

#### 验证 1: HSCAN count 最优值探测

遍历 5 个 count 值，每组 10s 预热 + 15s 采样。

**注意：count 是 hint（建议值），实际返回条数与内部遍历耗时并不严格线性。**
实际耗时受 field 大小（value 字节数）、哈希桶分布、Redis 实例 CPU、内存布局和网络 RTT 等影响。以下为本次基准环境下的观测值，生产环境必须复测。

| count | W-P50 | W-P99 | W-max | cmd/s | R-avg | 分析 |
|:--|:--|:--|:--|:--|:--|:--|
| **HGETALL** | **63ms** | **1024ms** | **1065ms** | 4.7k | 1057ms | 灾难级退化 |
| 50 | 17~31ms | 63~107ms | 84~134ms | 21~37k | — | 观测 ~0.3ms/页，对写友好但读慢 |
| 100 | 14~22ms | 56~117ms | 58~142ms | 15~23k | — | 适中 |
| **200** | **10~34ms** | **62~89ms** | **78~90ms** | **24~28k** | — | 本环境下读写平衡最优（见下方调优说明） |
| 500 | 10~20ms | 51~62ms | 62~98ms | 8~16k | 831~1508ms | 观测 ~1ms/页，读开始影响写 |
| 1000 | 38~59ms | 101~126ms | 102~150ms | 18~27k | 1623~2202ms | 观测 ~2ms/页，退化回升 |

```
最优值分析（本测试环境下）:

count=200: W-P99 62~89ms, cmd/s 24~28k, 读写两端都接近最优
count=50:  W-P99 63~107ms, cmd/s 21~37k, P99 与 200 接近但读吞吐低
count=1000:W-P99 101~126ms, cmd/s 18~27k, 开始出现 P99 回升
```

**结论：在本测试环境中（字段 avg size = X bytes、网络 RTT = Y ms、并发写线程 6），count=200 在读写平衡上表现最好。count 是 hint，生产必须复测并把该值做成可配项。** 调优流程：在预发环境扫 50/100/200/500/1000，观察 W-P99 / cmd/s / R-latency，选出折中值并将其参数化。太小读慢（RTT 多），太大又回到阻塞问题。

#### 验证 2: hScan() 游标正确性（Spring Data Cursor bug 修复）

**发现的 bug：**

```java
// 修复前：Spring Data ScanOptions 不支持外部游标
public RedisResult<HScanResult> hScan(String key, long cursor, int count) {
    Cursor<Map.Entry<Object, Object>> c = redisTemplate.opsForHash()
        .scan(key, options);  // ← 每次从 cursor=0 开始，cursor 参数被忽略！
}
```

**修复方案：**

```java
// 修复后：用 nativeLettuceCommand 访问 Lettuce 原生 hscan API
public RedisResult<HScanResult> hScan(String key, long cursor, int count) {
    return nativeLettuceCommand(commands -> {
        ScanCursor lettuceCursor = (cursor == 0) ? ScanCursor.INITIAL
            : ScanCursor.of(Long.toString(cursor));  // ← 正确传递游标
        MapScanCursor<byte[], byte[]> result = commands.hscan(rawKey, lettuceCursor, ...);
        return new HScanResult(Long.parseLong(result.getCursor()), fields);
    });
}
```

**SCAN/HSCAN 语义约束（重要）：**
1. **游标格式**：ScanCursor 的 cursor 是字符串形式（`"0"` 表示遍历完成），需正确解析和传递，不能当数值直接比较。
2. **并发修改影响**：SCAN/HSCAN 在并发写时**可能返回重复 field 或遗漏新增/已删除的 field**。它不是一致性快照，不能当成事务性读操作。
3. **调用端处理**：客户端需做去重（如用 HashMap 覆盖/合并）或容忍弱一致性。如果业务要求强一致性（如准确读取某时刻的完整数据），应在应用层加分布式锁或采用双写/迁移策略，不可依赖 SCAN 来做事务级操作。

#### 验证 3: HGETALL_BLOCK_THRESHOLD 硬阈值

```
HGETALL 50,000 fields: W-P99=1024ms, W-max=1065ms   → 绝对不可接受
HGETALL  5,000 fields: 粗略估算 ~100ms (量级推算)     → 仍不可接受
HGETALL  1,000 fields: 粗略估算 ~20ms                → 边缘情况
HGETALL    500 fields: 粗略估算 ~10ms                → 可容忍

注意：以上耗时为粗略量级估算（order-of-magnitude），实际耗时受 field value
      字节数、编码方式（zipmap/hashtable）和实例负载大幅影响。
      不应用固定的 "field数→耗时" 线性比例来精确推算，必须基于实测数据。
```

**阈值设计（经验值，需以估算字节/实测延时为准）：**

```
策略优先级：
  首选：基于估算数据量（HLEN × avg_field_size）或历史 HGETALL 耗时分布做阈值
  兼顾：保留 field-count 快速判定（便于实现），但标注为"经验值，需在不同环境复测调整"

实现建议：hGetAll() 逻辑中先 HLEN，再可选地 sample 少量 field 估算 avg 字节，
        然后按软警告/硬拒绝策略执行。

经验性保护策略（需以字节/延时为最终判定依据）：
  估算总字节 > B_hard（或 HLEN ≥ 5000，经验值）  → 硬拒绝 (RedisSafetyException)
  HLEN 在 [500, 4999] 或估算字节接近阈值 → 软警告（log.warn，推荐 hScanAllSafe）
  HLEN < 500（或估算字节很小）           → 放行
注：阈值为经验值，请在不同数据形态下复测并调整。
```

#### 验证 4: hScanAllSafe() 全量安全遍历

```java
public RedisResult<Map<Object, Object>> hScanAllSafe(String key, int count) {
    // 单连接内循环 hscan，每页 count 个 field
    // 500 万 fields 也安全——每页只占主线程约 0.5ms（本次基准观测值）
    // 但 RTT 多：50000/200 = 250 轮
}
```

代价：多次 RTT（50k field @200 = 250 次往返，~250ms 总耗时），但对写请求几乎零影响。

**重要语义说明：**

1. **弱一致性**：HSCAN 遍历期间若有并发写，可能遗漏被并发删除/新增的 field，也可能返回重复 field（与验证 2 中 SCAN 语义一致）。这不是一致性快照，不能用于"必须准确反映某时刻全量数据"的场景。
2. **去重**：客户端合并结果时建议用 HashSet/HashMap 覆盖做去重，避免重复 field 导致的业务异常。
3. **强一致性需求**：如果业务必须获取一致快照，应在应用层加分布式锁，或在数据迁移期使用双写+灰度切换方案，不可依赖 HSCAN 做事务级操作。

### 5.4 混合场景

```
12 个服务中：
  - 8 个已迁移到 HSCAN（安全）
  - 2 个仍用 HGETALL（遗留代码）
  - 2 个用 hMGet（按需取字段，安全）
```

**实测 MIXED 模式（2 HGETALL + 2 HSCAN Reader, 6 Writer）：**

```
mode=MIXED  Writer: bat/s=0, cmd/s=0, totalBatches=0
            Reader: 620 reads, R-avg=40~68ms

结果：Writer 吞吐直接归零！
```

**混合模式的残酷真相：** 只要有一个服务还在用 HGETALL 读大 Hash，就会拖垮全体。

**为什么 Writer 吞吐为 0：** 即使 HGETALL 频率不高，但单次 HGETALL 大 Hash 会独占 Redis 主线程数百毫秒。在这段时间内，所有写请求（包括 Pipeline 中的批量写入）全部在单线程上排队，无法被处理。如果写方还使用了阻塞式 pipeline 或事务，排队效应会更加严重——一个 HGETALL 的长阻塞将导致写侧批量请求在 Redis 单线程上排队，写吞吐瞬间归零。

### 5.5 四种模式全景对比

| 模式 | W-P99 | W-max | cmd/s | Reader 阻塞 | 安全？ |
|:--|:--|:--|:--|:--|:--|
| 纯写 (无 Reader) | 10~11ms | 24~31ms | 234~277k | N/A | ✅ |
| HGETALL Reader | **42~62ms** | **74~121ms** | 179~222k | 7~11ms/次 | ❌ |
| HSCAN count=200 | **16~32ms** | **34~43ms** | 180~211k | ~252ms/次 | ✅ |
| **MIXED** | **∞ (死锁)** | **∞** | **0** | 40~68ms/次 | 💀 |

### 5.6 生产落地清单

| 修复项 | 文件 | 作用 |
|:--|:--|:--|
| 硬拒绝阈值 | `RedisConstants.HGETALL_BLOCK_THRESHOLD=5000` | ≥5000 field 直接报错 |
| hGetAll() 升级 | `RedisHashUtil.hGetAll()` | 先 HLEN 再判断：拒绝/告警/放行 |
| hScan() 修复 | `RedisHashUtil.hScan(key, cursor, count)` | 用 nativeLettuceCommand 正确传递游标 |
| hScanAllSafe() | `RedisHashUtil.hScanAllSafe(key, count)` | 全量安全遍历，推荐替代 hGetAll |
| 默认 reader-mode | `application.yml: reader-mode=HSCAN` | 新服务默认安全模式 |

### 5.7 取舍总结

```
                读性能      写保护      实现复杂度
─────────────────────────────────────────────────
HGETALL         快 (~7ms)   无保护      一行代码
HSCAN (200)     慢 (~252ms) 保护较好    游标循环
hScanAllSafe()  慢 (~252ms) 保护较好    一行代码（内部封装）
hMGet (按需)    快 (~1ms)   保护最好    需知道 field 名

推荐策略:
  1. 知道要哪些 field → 用 hMGet（最快 + 最安全）
  2. 必须全量遍历   → 用 hScanAllSafe(key, count)，count 可配置并在目标环境复测
  3. HGETALL        → 仅限 < 500 fields（经验值，需以估算字节/实测延时复测），超阈值硬拒绝
  4. 混合共存       → ❌ 严禁！一个 HGETALL 拖垮全体

阈值调优流程:
  1. 在预发/压测环境复测不同 count（50/100/200/500/1000），记录 W-P99 / cmd/s / R-latency
  2. 以字节数或历史 HGETALL 耗时为最终判定依据
  3. 将 HGETALL 阈值和 HSCAN count 做成可热改的配置项（application.yml）
```

### 5.8 两个控制层的职责边界

```
层级 1 — 分布式信号量（跨服务协调层）
  管: 12 个服务谁可以同时 flush pipeline
  不管: 你 flush 的是 200 条 SETEX 还是 1 条 HGETALL
  失守后果: 并发过多 → P99 升高 1~2x

层级 2 — RedisHashUtil（单服务安全层）
  管: 单个服务内部合法调用（拒绝危险命令）
  不管: 其他服务在做什么
  失守后果: 一个 HGETALL → 全体 Writer 吞吐归零

两层互补，缺一不可:
  ✓ 有信号量 + 无安全层 → 拿到槽位的人发 HGETALL 照样阻塞
  ✓ 有安全层 + 无信号量 → 大家都安全但并发可能超限
  ✅ 两者都有         → 真正的生产级保护
```

### 5.9 大 Hash 的生产拆分方案

#### 两种"大"的本质区别

```
场景 A — 大 Hash key:
  HSET user:1234 field_0001 val
  HSET user:1234 field_0002 val
  ...
  → HLEN user:1234 = 50000   ← 单一 key 的 field 数过多
  → 问题: HGETALL 阻塞主线程

场景 B — 大量 Hash key:
  HSET order:1 status paid
  HSET order:2 status shipped
  ...
  → 50 万个 key，每个只有 2~3 个 field
  → 问题: 写入总量打满 Redis CPU（不是大 Hash 问题）
```

| 维度 | 大 Hash key（场景 A） | 大量 Hash key（场景 B） |
|:--|:--|:--|
| **定义** | 单个 key field 数过多（经验值 > 5000）或估算总字节过大 | key 数量 > 10 万 |
| **瓶颈** | 单命令阻塞主线程 | Redis CPU + 网络带宽 |
| **症状** | P99 尖刺，写请求排队 | 整体吞吐下降，无尖刺 |
| **读操作** | HGETALL 危险 | 每个 HGETALL 才几个 field，安全 |
| **解决方向** | 拆分 key / HSCAN | Pipeline + 限流 + 分布式信号量 |

#### 拆分方案 1：按业务维度垂直拆分

```
拆之前（1 个 key, 50000 fields）:
  user:1234  { field_0001, field_0002, ..., field_50000 }

拆之后（按业务模块）:
  user:1234:basic      → 姓名、手机号、身份证等（10 fields）
  user:1234:config     → 主题、语言、通知开关（30 fields）
  user:1234:meta       → 评分、标签、画像数据（200 fields）
  user:1234:history    → 历史记录（若仍超 5000，见方案 2）
```

**优点**：每个 HGETALL 都安全，不改代码习惯
**缺点**：多次 RTT（但每次极快），需要事先设计分桶规则

#### 拆分方案 2：按数量/时间水平分片

```
拆之前:
  user:1234:history  { item_1, item_2, ..., item_50000 }

拆之后（按 1000 条一片）:
  user:1234:history:0     → item_1 ~ item_1000
  user:1234:history:1     → item_1001 ~ item_2000
  ...
  user:1234:history:49    → item_49001 ~ item_50000
```

**优点**：彻底杜绝大 Hash
**缺点**：需要维护分片索引，跨片查询需多次 HGETALL

#### 拆分方案 3：不拆分数据，只改读方式（兜底）

```java
// ❌ 危险 — hGetAll() 硬拒绝兜底
Map<Object, Object> all = hashUtil.hGetAll("user:1234").getData();
// → size ≥ 5000 → 抛 RedisSafetyException

// ✅ 安全替代 — 内部 HSCAN 分页，一行调用
Map<Object, Object> all = hashUtil.hScanAllSafe("user:1234", 200).getData();
```

**优点**：零改造成本
**缺点**：读变慢（多次 RTT），治标不治本

### 5.10 写入端如何避免产生大 Hash

```
┌────────────────────────────────────────────────────────────┐
│                        写入端设计                            │
├────────────────────────────────────────────────────────────┤
│  1. 预估 field 增长上限                                      │
│     → 历史记录？先拆分（方案 2）                               │
│     → 固定属性？按模块拆分（方案 1）                            │
│                                                             │
│  2. 写入时自检                                               │
│     → HLEN 检测 + 告警 → 超过阈值触发拆分                      │
│     → 同时检查估算总字节数（HLEN × avg_field_size）           │
│                                                             │
│  3. 定时巡检 + 自动拆分                                       │
│     → 定时任务扫描 HLEN > 5000 或估算字节超阈值的 key           │
│     → 自动执行分片逻辑，平滑迁移                               │
│                                                             │
│  4. 服务端兜底                                               │
│     → hGetAll() 硬拒绝（≥5000，经验值，需以字节/延时复测）       │
│     → hScanAllSafe() 作为安全替代                             │
└────────────────────────────────────────────────────────────┘
```

### 5.11 大 Hash 与热点 Key 的区别

```
大 Hash = 数据维度（一个 key 存了多少数据）
热点 Key = 访问维度（一个 key 被请求了多少次）
```

| | 大 Hash | 热点 Key |
|:--|:--|:--|
| **定义** | 单个 key 的 value 过大 | 单个 key 的 QPS 过高 |
| **根因** | 数据模型设计不当 | 访问集中（如秒杀商品、热门用户） |
| **瓶颈** | HGETALL 卡主线程（单命令耗时） | 单实例 CPU 打满（总请求量） |
| **解决** | 拆分 key / HSCAN | 多级缓存 / 读写分离 |

**最坏的叠加：**

```
# 大 Hash + 热点 Key 叠加 → 无法拯救
HSET user:666666 profile_field_1 ... 50000 fields
→ 10000 QPS 在 HGETALL 它
→ 大 (50k fields) × 热 (10k QPS) = 主线程被连续占用，Redis 直接崩溃
```

### 5.12 防患于未然 — 预防体系

#### 数据建模阶段就杜绝大 Hash

```
设计原则: 一个 Hash key 的 field 数永不超过 1000（推荐更保守的 500，需复测）
         同时控制单个 field value 的字节数，以"估算总字节 + 实测耗时"为最终判定

风险评估三轴: field 数 / 估算 bytes / 实测耗时（缺一不可）
  - field 数小但 value 很大（如每个 value 10KB）→ 同样会阻塞
  - field 数大但 value 极小（如 flag=1）→ 可能无问题但 SCAN RTT 次数多
  - 最终需以实测耗时为准，阈值标注为"经验值（需复测）"

反例:
  HSET user:{id} field1 val1 ... field50000 val50000   ← 不可控增长

正例:
  user:{id}:basic     → ≤ 20 field（固定属性）
  user:{id}:config    → ≤ 30 field（固定属性）
  user:{id}:history:0 → ≤ 1000 field（达到上限自动切分片 :1）
```

#### CI/上线前自动化检查

```
代码扫描规则（静态检查）:
  1. 禁止直接使用 redisTemplate.opsForHash().entries(key)
     禁止直接使用 redisTemplate.opsForHash().values(key)
     → 强制走 RedisHashUtil 封装（将 RedisHashUtil 设为唯一入口）
  2. 检测到 HGETALL → 确认 key 的 field 数 < 500
  3. 检测到 HSET 未被分片逻辑包裹 → 告警
```

#### 监控基线（可操作化）

**监控项与指标：**

| 监控项 | 工具/来源 | 阈值建议 | 告警级别 |
|:--|:--|:--|:--|
| HLEN TopN | `redis-cli --bigkeys` + 定时脚本 | 单 key HLEN > 1000 且增长速率 > 阈值 | P2 告警 |
| HGETALL 慢查询 | `SLOWLOG GET 10` | 某 key HGETALL 耗时 > 50ms | P0 紧急告警，审查调用方 |
| 命令延时分布 | `latency-monitor` 或 `INFO commandstats` | HGETALL avg > 10ms 持续 1min | P1 告警 |
| 热点 Key | `redis-cli --hotkeys`（4.0+） | 单 key QPS > 1000 | P2 告警 |
| 实例 CPU / 延时 | `INFO stats` + `INFO cpu` | CPU > 80% 或 latency spike > 2x baseline | P1 告警 |

**SLO 建议：**
- HGETALL P99 耗时 < 5ms（针对小 Hash）
- HSCAN 单页 P99 耗时 < 1ms
- 大 Hash（HLEN ≥ 5000）禁止直接 HGETALL，统一走 hScanAllSafe

**CI 集成：**
- 在 CI pipeline 中加入静态扫描规则（禁止 `opsForHash().entries` / `opsForHash().values`），`RedisHashUtil` 作为唯一 Hash 读入口。
- 上线前在预发环境复测不同 count 值（50/100/200/500/1000），选出折中值并写入配置项。

```yaml
# application.yml 示例：HSCAN count 与 HGETALL 阈值可热改配置
redis:
  hashutil:
    hscan-default-count: 200       # count hint，生产需复测
    hgetall-soft-threshold: 500    # 软警告阈值（经验值）
    hgetall-hard-threshold: 5000   # 硬拒绝阈值（经验值）
    reader-mode: HSCAN             # 默认安全模式
```

#### 热点 Key 预防

```
┌───────────────────────────────────┐
│ L1: Caffeine 本地缓存             │
│  expireAfterWrite=5s              │
│  maximumSize=10000                │
│  10k QPS 穿透后只剩 0.2 QPS       │
├───────────────────────────────────┤
│ L2: Redis 远程缓存                 │
│  数据已拆小，单命令 0.5ms           │
├───────────────────────────────────┤
│ L3: DB                            │
└───────────────────────────────────┘
```

### 5.13 亡羊补牢 — 紧急补救

#### 判定严重程度

```bash
redis-cli HLEN user:1234              # field 数 > 5000？ → 大 Hash（需同时估算总字节）
redis-cli --hotkeys                   # 4.0+ 自带热点检测
redis-cli SLOWLOG GET 10              # 慢日志里是否全是 HGETALL？
redis-cli --bigkeys                   # 扫描全库大 key，关注 Largest hash 的 field 数
redis-cli DEBUG OBJECT user:1234      # 查看 serializedlength 字节数，辅助评估真实数据量
```

#### 止血：立即行动（分钟级）

```java
// 客户端紧急适配器 — 自动降级
public RedisResult<Map<Object, Object>> safeGetAll(String key) {
    long size = hashUtil.hLen(key).getData();
    if (size >= 5000) {  // 硬阈值（经验值，建议同时检查估算字节或历史耗时）
        log.error("[EMERGENCY] key={} 字段数={}，自动降级到 hScanAllSafe", key, size);
        return hashUtil.hScanAllSafe(key, 200);
    }
    return hashUtil.hGetAll(key);
}
```

说明：5000 为经验值快速判定。更稳健的做法是先 HLEN，再 sample 少量 field 估算 avg 字节，
以估算总字节（HLEN × avg_field_size）或历史 HGETALL 耗时分布作为降级依据。

#### 拆数据：平滑迁移（四阶段）

```
Phase 1: 双写（新旧两套 key 同时写）
Phase 2: 历史数据回灌（用 HSCAN 非阻塞迁移）
Phase 3: 灰度切读（10%→50%→100% 流量切换）
Phase 4: 清理旧 key（UNLINK 异步删除）
```

### 5.14 两阶段完整对比

| | 预防（设计期） | 补救（事故后） |
|:--|:--|:--|
| **成本** | 多花 10% 建模时间 | 紧急修复 + 数据迁移 + 灰度验证 |
| **线上风险** | 零风险 | 迁移期间可能丢数据或不一致 |
| **时间** | 0 事故时间 | 分钟级止血 → 小时~天级迁移 |
| **效果** | 永不大 Hash，永无热点 | 治本但周期长 |

```
最终警示:
  Redis 单线程模型下，一条 HGETALL 对大 Hash（field 多或 value 大） = 全局 DDoS
  一分钟的偷懒（不拆分、不设护栏）= 一晚上的救火

  风险评估不能只看 field 数，同时要关注估算传输字节和实测耗时。
  文档中所有阈值（500/5000）均为经验值，生产环境必须基于本环境实测数据复测调整。
```

### 5.15 补充：Lua 脚本阻塞 — 区别于大 Hash 的另一种慢操作

#### 5.15.1 大 Hash vs Lua 脚本过长

| | 大 Hash | Lua 脚本过长 / 循环量大 |
|:--|:--|:--|
| **根因** | 数据结构体量大（单个 Key 下字段数多） | 脚本逻辑体量大（代码量大 or 循环次数多） |
| **阻塞发生在** | Redis **命令执行**阶段 | Lua **脚本执行**阶段（整个 EVAL 期间） |
| **典型场景** | `HGETALL big_hash`（10 万 field 全量拉取） | `for i=1,100000 do redis.call('HSET',...) end` |
| **Redis 感知** | slowlog 能看到具体命令耗时 | slowlog 只记录一条 `EVAL`，看不到内部 |
| **最坏组合** | 大 Hash × 大循环 = 致命 | `EVAL "for i=1,100000 do redis.call('HGETALL', 'big_hash') end"` |
| **解决方向** | 拆分数据（分片 / 拆分字段） | 拆分脚本（分批执行 / 减少单次循环量） |

```
slowlog 排查口诀:
  看到具体命令耗时长 (HGETALL / HGETALL big_key)    → 大 Hash
  看到 EVAL 耗时长，但内部 REDIS.call 都很快       → Lua 循环量大
```

#### 5.15.2 Lua 脚本 ≠ Redis 命令

```
Redis 命令 = 一条 SQL 语句
Lua 脚本   = 一个存储过程（把多条 SQL 打包原子执行）
```

| | 发 HGETALL | 发 EVAL |
|:--|:--|:--|
| Redis 收到 | 一条命令 | 一段脚本（内含多条命令） |
| 阻塞粒度 | 一条命令的时间 | 整段脚本执行完的时间 |

#### 5.15.3 EVAL 中某条命令失败 ≠ 全部回滚

**Redis Lua 脚本没有事务回滚机制。** 只保证**原子性**（执行期间不穿插其他命令），不保证**事务性**（失败不撤回已执行操作）。

#### 5.15.4 `redis.call()` vs `redis.pcall()`

| | `redis.call()` | `redis.pcall()` |
|:--|:--|:--|
| 命令失败时 | **直接抛错，脚本终止** | **返回 error 对象，脚本继续** |
| 适用场景 | 失败就应该停（不容错） | 允许部分失败、需要感知哪条错了 |

#### 5.15.5 什么时候需要 Lua 脚本？

| 条件 | 说明 |
|:--|:--|
| **需要原子性** | 多条命令之间不能被插队（库存查+扣、锁获取+设值） |
| **需要服务端逻辑** | 用 Lua 在 Redis 端做判断/计算，减少网络往返 |

绝大多数场景只发单条命令不需要 Lua。

#### 5.15.6 Lua 脚本性能红线

| 红线 | 原因 |
|:--|:--|
| **单次 EVAL 执行时间 < 1ms** | 超过就是慢脚本，会影响所有客户端 |
| **循环量 ≤ 100 次** | 再大就应拆成多次 EVAL |
| **不要在 Lua 里做 HGETALL / SMEMBERS 大 Key 操作** | 大 Key 本身已经慢，放进 Lua 只会雪上加霜 |
| **Lua 脚本不要超过 100 行** | 可维护性差 + 调试困难 + 容易写出大循环 |
| **Redis 7.0+ 使用 `EVAL_RO`** | 只读脚本可以在只读副本执行（集群模式），减轻主实例压力。注意副本存在数据延迟，业务需容忍弱一致性。 |
| **slowlog 排查** | 看到 EVAL 耗时很长时，需区分是脚本内部"循环量大"还是单条命令慢（如 HGETALL 大 key）。 |
