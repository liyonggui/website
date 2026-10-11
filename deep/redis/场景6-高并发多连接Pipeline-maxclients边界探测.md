## 场景 6: 高并发多连接 Pipeline — maxclients 边界探测

### 6.1 背景与问题

```
生产现象:
  微服务拆分后，十几个服务共享一个 Redis 实例
  每个服务内部又有多线程 Pipeline 写入
  连接数暴涨 → 逼近 maxclients → 新建连接被拒绝 → 连接池耗尽 → 大面积报错

核心问题:
  1. 当前架构每个 Worker 线程独占一条 Redis 连接，并发 = 连接数
  2. 没有客户端侧连接数限制
  3. 超过 maxclients 后新连接被拒，业务直接报错
```

### 6.2 压测架构

```
模式 DIRECT（当前默认）:
  Worker-1 ── redisClient.connect() ── 连接-1 ── Pipeline 循环（占住不放）
  Worker-2 ── redisClient.connect() ── 连接-2 ── Pipeline 循环（占住不放）
  ...
  Worker-N ── redisClient.connect() ── 连接-N ── Pipeline 循环
  ↑ 并发 = N → 连接数 = N，无上限，终将撞向 maxclients

模式 POOL（对比方案）:
  Worker-1 ── pool.borrowObject() ── 拿到连接 ── Pipeline ── pool.returnObject()
  Worker-2 ── pool.borrowObject() ── 拿到连接 ── Pipeline ── pool.returnObject()
  ...
  Worker-N ── pool.borrowObject() ── 池已满 → 等待超时 → 返回错误
  ↑ 连接数上限 = poolMaxTotal，超出的 Worker 排队等待而不是创建新连接
```

### 6.3 探测指标

| 维度 | 指标 | 来源 |
|:--|:--|:--|
| Redis 服务端 | `connected_clients` | `INFO clients` |
| | `rejected_connections` | `INFO stats`（累计 reject 数，监控 delta 更重要） |
| | `maxclients` | `INFO clients`（硬上限，以实际部署的 `INFO clients` 返回值为准） |
| | `blocked_clients` | `INFO clients`（**正在执行阻塞命令的客户端数**，如 BLPOP/BRPOP/XREAD BLOCK，并非被拒绝的连接数） |
| 客户端 | `connectSuccess / connectFail` | 应用层计数 |
| | 当前活跃连接数 | `directConns.size()` |
| | 连接池状态 | `poolActive / poolIdle / poolWaiters` |
| | 吞吐量 / 延迟 | Pipeline 批次指标 |
| | 等待队列长度 / 平均等待时间 | Semaphore queue / pool waiters 趋势（衡量用户感知延迟） |
| 系统层 | 文件描述符 (`ulimit -n`) | OS 级 |
| | TCP 连接数 / ephemeral port 使用率 | `netstat` / `ss` |
| | 网络丢包 / CPU | `ifconfig` / `top` |

### 6.4 测试方法

#### Redis maxclients 说明

```
Redis 默认 maxclients 常见为 10000，但不同发行版、容器镜像或配置可能修改此值。
务必以实际部署的 redis-server 执行 INFO clients 获取的 maxclients 值为准：
  docker exec <redis-container> redis-cli INFO clients | grep maxclients
```

#### Docker Compose 调整

```yaml
# docker-compose.yml 中限制 maxclients 为一个较小值（便于触发边界）
command: redis-server --appendonly no --save "" --maxclients 500
```

#### REST 端点

```
GET  /maxclients/metrics          -- 当前指标快照（含 Redis INFO 采样）
POST /maxclients/start            -- 启动压测
      ?concurrency=100&batchSize=20&mode=DIRECT
POST /maxclients/stop             -- 停止压测
POST /maxclients/sweep            -- 自动遍历 concurrency 梯度
GET  /maxclients/sweep/results    -- 查看 Sweep 对比报告
```

#### 单点测试

```bash
# 1. DIRECT 模式 100 并发（预期连接数=100，正常）
curl -X POST "http://localhost:8082/maxclients/start?concurrency=100&batchSize=20&mode=DIRECT"

# 观察指标
curl http://localhost:8082/maxclients/metrics

# 2. DIRECT 模式 600 并发（预期超过 maxclients=500，出现拒绝）
curl -X POST "http://localhost:8082/maxclients/start?concurrency=600&batchSize=20&mode=DIRECT"

# 3. POOL 模式对比：600 并发但连接池上限=50
curl -X POST "http://localhost:8082/maxclients/start?concurrency=600&batchSize=20&mode=POOL"

# 4. 停止
curl -X POST "http://localhost:8082/maxclients/stop"
```

#### Sweep 自动遍历

```bash
# DIRECT 模式 Sweep
curl -X POST "http://localhost:8082/maxclients/sweep?batchSize=20&mode=DIRECT"

# POOL 模式 Sweep 对比
curl -X POST "http://localhost:8082/maxclients/sweep?batchSize=20&mode=POOL"

# 查看结果
curl http://localhost:8082/maxclients/sweep/results
```

### 6.5 DIRECT vs POOL 预期行为

| | DIRECT 模式 | POOL 模式 |
|:--|:--|:--|
| **连接数** | = concurrency（无上限） | ≤ poolMaxTotal（硬上限） |
| **超出 maxclients 时** | `redisClient.connect()` 抛异常 → Worker 退出 → `connectFail++` | 连接池已满 → Worker 阻塞等待 → 不创建新连接 |
| **Redis 行为** | `rejected_connections++`，日志 `max number of clients reached` | `rejected_connections` 不增加 |
| **对业务影响** | 部分请求直接失败（不可恢复） | 请求排队/超时（可恢复） |
| **吞吐** | 连接成功前吞吐高，达到上限后新 Worker 全失败 | 受 poolMaxTotal 限制，但所有请求都能排队获得服务 |
| **延迟** | 低延迟（直连）直到崩溃 | 高并发下 borrowObject 有等待延迟 |
| **Pub/Sub** | ✅ 天然支持 | ✅ 支持，但**订阅连接需专用独立长连接**（不能混入通用池被复用） |

### 6.6 关键发现

#### 6.6.1 连接数临界点

```
connected_clients 逼近 maxclients 时：
  ├── connected_clients < maxclients × 0.8   → 正常，延迟稳定
  ├── connected_clients = maxclients × 0.9   → 延迟开始抖动
  └── connected_clients = maxclients         → 新连接REJECT，connectFail 飙升
```

#### 6.6.2 DIRECT 模式的致命缺陷

```java
// 当前代码：每个 Worker 独占一条连接，停不下来
StatefulRedisConnection<String, String> conn = redisClient.connect(...);
// 如果 connect 成功，这个连接就一直被这个线程占着
// Worker 线程在执行 Pipeline 主循环，不会退出
// 连接数 = Worker 数，没有收缩机制
```

#### 6.6.3 POOL 模式的 "熔断" 效应

```java
// 连接池：硬上限保护
GenericObjectPool pool = ConnectionPoolSupport.createGenericObjectPool(...);
pool.setMaxTotal(50);              // 最多 50 个连接
pool.setBlockWhenExhausted(true);  // 池满了就等
pool.setMaxWait(Duration.ofMillis(200));  // 等 200ms 还拿不到就超时
```

### 6.7 核心结论

```
┌─────────────────────────────────────────────────────────────┐
│                   场景 6 核心发现                            │
├─────────────────────────────────────────────────────────────┤
│  1. DIRECT 模式 = 连接数无上限，高并发必撞 maxclients       │
│     - 连接被拒后业务直接失败，没有优雅降级机制              │
│     - 连接一旦创建就占住不放，没有复用                      │
│                                                             │
│  2. POOL 模式 = 连接池硬上限，天然的熔断器                  │
│     - 连接数稳定在 poolMaxTotal，绝不超标                   │
│     - "池满等待" 比 "连接被拒" 更优雅                      │
│     - 连接复用减少 TCP 开销                                 │
│                                                             │
│  3. 生产环境强烈建议:                                       │
│     - 永远用连接池或 Semaphore，不要裸露无限制 connect()    │
│     - poolMaxTotal 配置公式（含运维预留）:                   │
│       poolMaxTotal_per_instance ≤                           │
│         floor((maxclients - reserved_for_ops)               │
│               / instance_count / safetyFactor)              │
│       · reserved_for_ops: 哨兵/监控/运维预留, 建议 50~200   │
│       · safetyFactor: 1.1~1.3（缓冲突发峰值）              │
│       · 例: maxclients=10000, 实例=10, reserved=200,       │
│               safetyFactor=1.2 → floor(9800/10/1.2)=650    │
│     - 以实际 INFO clients 的 maxclients 为准（发行版可能不同）│
│     - 监控 connected_clients 逼近 maxclients 时告警         │
│                                                             │
│  4. 监控 rejected_connections 的 δ（增长速率）:             │
│     - 累计值 > 0 只说明"曾经发生过拒绝"                    │
│     - delta > 0 说明"当前正在发生拒绝" → 紧急告警          │
└─────────────────────────────────────────────────────────────┘
```

### 6.8 三种模式实测对比（Docker, maxclients=500, sweep 8 步）

#### SETEX Pipeline, batch=20, sweep conc 10→360

| conc | DIRECT 裸<br>cmd/s / 连接 | DIRECT+Semaphore<br>cmd/s / 连接(sem) | POOL<br>cmd/s / 连接 |
|------|------|------|------|
| 10 | 133k / 9 | **164k** / 8 (6/50) | 52k / 10 |
| 60 | 141k / 6 | **191k** / 14 (11/50) | 59k / 14 |
| 110 | 164k / 9 | **154k** / 8 (4/50) | 58k / 13 |
| 160 | 150k / 9 | **178k** / 10 (5/50) | 59k / 14 |
| 210 | 131k / 11 | **186k** / 11 (5/50) | 58k / 14 |
| 260 | 142k / 16 | **141k** / 10 (3/50) | 58k / 16 |
| 310 | 137k / 14 | 63k / 9 (1/50) | 59k / 17 |
| 360 | 139k / 13 | **137k** / 13 (4/50) | 64k / 18 |
| **均吞吐** | ~**140k** | ~**155k** ⭐ | ~58k |
| **均连接** | ~**360**条（=并发数） | ~**10** 条（上限 50） | ~**14** 条（上限 50） |
| **拒绝** | 0 | 0 | 0 |
| **吞吐/连接** | 388 cmd/s/conn | **15,500 cmd/s/conn** | 4,143 cmd/s/conn |

> **测试条件**: Docker 单机, Redis 7.x, maxclients=500, SETEX Pipeline batch=20, value=16B, TTL=2s, Lettuce 异步 IO。
> "吞吐/连接" 受 batch size、Pipeline depth、命令类型、网络延迟等因素显著影响，跨环境对比时需注明条件。

#### 6.8.1 极限压测：Semaphore 硬上限验证

```bash
# 500 并发，Semaphore 仅允许 5 条连接，证明硬上限生效
curl -X POST "http://localhost:8082/maxclients/start?concurrency=500&mode=DIRECT&directMaxConn=5&directConnTimeoutMs=500"
```

| 指标 | 值 | 含义 |
|:--|:--|:--|
| semaphoreMax / semaphoreUsed | **5 / 5 (100%)** | 全部许可被占满 |
| semaphorePermits | 0 | 无空闲许可 |
| semaphoreQueueLength | 231（瞬时值） | 231 个 Worker 排队等许可 |
| connectSuccess | 271 | 271 个 Worker 获得许可 → 连接成功 |
| **connectFail** | **231** | 231 个 Worker 等 500ms 超时 → 客户端优雅拒绝 |
| directConnCount | 5 | 始终 5 条 Redis 连接 |
| redisConnectedClients | 7 | Redis 侧确认（5+1+1） |
| **redisRejectedConnections** | **0** | Redis 零拒绝！全部压力在客户端消解 |
| totalCommands | 1,640 万 | 仅 5 条连接 60 秒内处理 |
| totalErrors | 0 | 管线命令零错误 |

```
关键结论：
  Semaphore 在客户端侧完成了"熔断"，231 个 Worker 连接请求被拦截在客户端，
  没有到达 Redis。Redis 只需承受 5 条连接的管线压力。
  如果不用 Semaphore，500 条连接直连 Redis → 全部 connect() 成功 → connected_clients 飙升 → 撞 maxclients。
```

#### 6.8.2 三种模式对比结论

| | DIRECT（裸） | POOL | **DIRECT + Semaphore** ⭐ |
|:--|:--|:--|:--|
| **吞吐** | 140k cmd/s | 58k cmd/s | **155k cmd/s**（最高） |
| **连接数** | = 并发数（无上限） | ≤ poolMaxTotal | ≤ semaphoreMax |
| **连接效率** | 388/cmd/s/conn | 4,143/cmd/s/conn | **15,500/cmd/s/conn** |
| **Redis 压力** | ❌ 高（N 条连接） | ✅ 低 | ✅ 极低（仅 5-10 条） |
| **故障模式** | 撞 maxclients 后全炸 | 池满排队/超时 | 许可耗尽 → 客户端拒绝 |
| **适用场景** | 低并发已知安全 | 多服务共享 Redis | **单租户高吞吐 + 安全兜底** ⭐ |
| **Pub/Sub** | ✅ 天然 | ✅ 需专用独立长连接（不混入池复用） | ✅ 天然 |
| **redisRejectedConnections** | 可能>0 | 永远 0 | **永远 0** |

```
DIRECT + Semaphore = 裸 DIRECT 的吞吐 + POOL 的安全性 + Pub/Sub 支持

  只用 10 条连接打满 ~155k cmd/s，同时有 Semaphore 硬上限兜底，
  永远不会让 Redis 看到超额连接。

  不是"DIRECT 没有用"，而是"裸 DIRECT 不安全"——
  加一个 Semaphore 就是又快又安全的最优方案。
```

### 6.9 核心架构：DIRECT + Semaphore 实现原理

```java
// Semaphore 许可 = 连接数硬上限
// fairness=true: 公平模式，FIFO 排队，避免饥饿，但吞吐略低于非公平模式
Semaphore connSemaphore = new Semaphore(directMaxConnections, true);

// 每个 Worker 通过 Semaphore 获取连接许可
void directPipelineWorker(int workerId, String value) {
    // 1. 获取许可（超时 → 优雅降级）
    if (!connSemaphore.tryAcquire(timeoutMs, TimeUnit.MILLISECONDS)) {
        connectFail++;   // 客户端侧熔断，不连 Redis
        return;
    }
    try {
        // 2. 获取许可后创建连接
        StatefulRedisConnection conn = redisClient.connect(...);
        connectSuccess++;
        // 3. 持许可跑 Pipeline 循环（高吞吐）
        while (running) { runBatch(...); }
    } finally {
        // 4. 归还许可 → 注意同步关闭/归还连接，避免连接泄漏
        conn.close();
        connSemaphore.release();
    }
}
```

**Semaphore 优化建议**：

```
当前实现: 每次获取许可 → 新建连接 → 用完关闭
问题: TCP 握手、认证等操作重复发生，在许可频繁交接时产生额外开销

优化方案 A（推荐）: Semaphore 许可槽 = 长连接
  - 每个 permit 对应一条常驻连接
  - Worker 拿到 permit → 直接复用对应连接
  - Worker 释放 permit → 连接回到槽中供下个 Worker 使用
  - 无需重复 connect()/close()

优化方案 B: Semaphore + 小型连接池
  - Semaphore 控制同时活跃的 Worker 数
  - 连接池（maxTotal = directMaxConnections）负责连接复用与重连
  - Worker: semaphore.tryAcquire() → pool.borrowObject() → ... → pool.returnObject() → semaphore.release()

选择:
  高 Pipeline 吞吐场景 → 方案 A（一条连接用一个 Worker 长时间持留最稳）
  Worker 频繁进出 → 方案 B（池负责连接生命周期管理）
```

```
安全公式:  directMaxConnections = maxclients × 0.7 / 服务实例数
          例: maxclients=10000, 10 实例 → 10000×0.7/10 = 700，设 500 保险
```

### 6.10 生产监控基线

```bash
# ========== Redis 侧告警 ==========

connected_clients > maxclients × 0.7  →  P1 告警：连接数逼近上限
connected_clients > maxclients × 0.9  →  P0 告警：即将拒绝连接

# rejected_connections 关键：监控 delta（增长速率），而非只看累计值
rejected_connections 累计值 > 0  →  仅说明"曾经发生过拒绝"（历史问题）
rejected_connections delta > 0   →  P0 告警：当前正在拒绝连接（紧急）

# blocked_clients 持续上升 →  大量客户端在执行阻塞命令（BLPOP/XREAD BLOCK），非连接排队

# ========== 客户端侧告警（DIRECT+Semaphore） ==========

semaphoreUsed / semaphoreMax > 0.8     →  连接许可逼近上限
semaphoreQueueLength > 0               →  有 Worker 在排队等许可（监控趋势，持续增长需扩容）
semaphoreAvgWaitTime > 阈值            →  用户感知延迟正在上升
connectFail > 0                        →  有 Worker 等候许超时 = 客户端限流生效中

# ========== 客户端侧告警（POOL） ==========

poolNumActive / poolMaxTotal > 0.8     →  连接池即将耗尽
poolNumWaiters > 0                     →  有请求在排队等连接
connectFail > 0                        →  连接创建失败

# ========== 系统层监控 ==========

文件描述符使用率 > 80% (ulimit -n)     →  连接数可能触及 OS 限制
ephemeral port 使用率 > 60%            →  客户端端口可能耗尽
TCP ESTABLISHED 连接数异常增长          →  连接泄漏风险
网络丢包 / CPU 饱和                     →  非 Redis 侧瓶颈

# 压测时需要同时收集: netstat -an | grep ESTABLISHED | wc -l、
# ulimit -n、ifconfig 丢包计数、top/htop
```

### 6.12 连接池预热（Pre-warm）

#### 6.12.1 问题：连接池的"首请求惩罚"

```
commons-pool2 的连接池默认是懒加载：
  创建 Pool → 池空（0 条连接）→ 第一个 Worker borrow → 触发 Supplier → TCP 握手 → 拿到连接
                                                     ↑
                                               第一个请求要多花 1~5ms

虽然握手很快（毫秒级），但在高并发场景下：
  - 第 0ms: 500 个 Worker 同时涌进来，全触发 Supplier
  - 前几个成功握手，后面的排队等待
  - 导致压测起步阶段出现短暂延迟尖刺
```

#### 6.12.2 方案：异步预热

**通用方法（不依赖 `preparePool()` — 所有连接池库都可用）：**

```java
// 并非所有客户端库都有 preparePool() API。
// 通用做法：后台线程循环 borrow()/return() 若干次以建立 minIdle 连接。

CompletableFuture.runAsync(() -> {
    try {
        for (int i = 0; i < poolMinIdle; i++) {
            // borrow → 触发 Supplier（TCP 握手 + 认证）→ return
            connectionPool.returnObject(connectionPool.borrowObject());
        }
        log.info("POOL 预热完成（通用 borrow/return 循环）: {} 条连接", connectionPool.getNumIdle());
    } catch (Exception e) {
        log.warn("POOL 预热失败（不影响后续 borrow，Worker 将走 lazy init）: {}", e.getMessage());
    }
});

// 若库支持 preparePool()（如 commons-pool2），可直接使用：
// connectionPool.preparePool();
```

**预热必须容错**：
- 异步预热不应阻塞服务启动
- 预热失败应记录日志/指标但不抛异常 → Worker 仍可通过 lazy 借用正常获取连接
- 建议增加重试/退避策略（如预热失败后等 5s 重试一次）

```
时间线：

  同步预热                                异步预热
  ────────                               ────────
  createPool()                           createPool()
    │                                      │
    ├─ preparePool() ← 卡住等握手           ├─ runAsync(preparePool) → 立刻返回
    │    ├─ 握手1                          │
    │    ├─ 握手2                          ├─ start() ← 立刻开始
    │    └─ 握手3   耗时 ~5ms              │
    │                                      │   后台线程在握手...
    ├─ start()                              │
                                             ▼
                                           预热完成前 Worker 走 lazy borrow
                                           预热完成后 Worker 直接拿，零握手

  启动慢 5ms，零风险                       启动快 + 握手照做
```

#### 6.12.3 不预热 vs 同步预热 vs 异步预热

| | 不预热 | 同步预热 | 异步预热（推荐） |
|:--|:--|:--|:--|
| **启动速度** | ⚡ 最快 | 🐌 多等握手时间 | ⚡ 最快 |
| **首个请求延迟** | ❌ 多 1~5ms（握手） | ✅ 零握手 | ⚡ 大概率零握手* |
| **预热失败影响** | 无 | ❌ 启动失败 | ✅ 不影响，Worker 走 lazy |
| **代码复杂度** | 最简单 | 简单 | 多一行 `runAsync` |

> *如果预热线程跑得比第一个请求慢，第一个请求仍走 lazy borrow，不会报错。

#### 6.12.4 预热适用所有中间件

```
定律：只要涉及连接池，就值得预热。

  MySQL/PG    → HikariCP 自带 initialization-fail-timeout
  Redis       → ConnectionPoolSupport + preparePool()
  Kafka       → 启动后立即 send 空消息，或用 adminClient.describeCluster()
  RabbitMQ    → 启动后预创建 channel / 声明队列
  MongoDB     → driver 自带连接池预热（minPoolSize）
  HTTP 客户端 → PoolingHttpClientConnectionManager + 启动后发一次请求
  gRPC        → ManagedChannel.getState(true) 等待就绪

根因：
  TCP 握手 + TLS 协商 + 中间件认证，加起来 10ms~200ms
  预热把它从"第一个用户请求"挪到"后台启动时"
  用户无感，连接就绪。
```

---

### 6.13 场景 6 总结：生产落地 checklist

#### 6.13.1 问题根源

```
问题: 微服务拆分后，每个 Worker 线程独占/新建 Redis 连接 →
     连接数随并发线性增长 → 触发 maxclients → rejected_connections → 业务失败。

根因:
  - 无客户端侧连接数限制
  - 无连接复用（每次 Worker 启动都建新连接）
  - 连接创建/保留无上限
```

#### 6.13.2 三种方案对比

| | DIRECT（裸） | POOL | **DIRECT + Semaphore** ⭐ |
|:--|:--|:--|:--|
| **吞吐** | ~140k cmd/s | ~58k cmd/s | **~155k cmd/s** |
| **连接数** | = 并发数（无上限） | ≤ poolMaxTotal | ≤ semaphoreMax |
| **Redis 保护** | ❌ 无 | ✅ 池硬上限 | ✅ 客户端硬上限 |
| **故障模式** | 撞 maxclients → 全炸 | 池满排队/超时 | 许可耗尽 → 客户端拒绝 |
| **连接复用** | ❌ 每次新建 | ✅ 池内复用 | ⚡ Semaphore 许可控制（建议搭配长连接/池复用）|
| **Pub/Sub** | ✅ 天然 | ✅ 需专用连接 | ✅ 天然 |
| **推荐场景** | 低并发已知安全 | 多服务共享 Redis | 单租户高吞吐 + 安全兜底 |

#### 6.13.3 poolMaxTotal 安全公式

```
poolMaxTotal_per_instance ≤ floor((maxclients - reserved_for_ops) / instance_count / safetyFactor)

  reserved_for_ops: 哨兵、监控、运维脚本预留（建议 50~200，视环境调整）
  safetyFactor: 1.1~1.3

示例:
  maxclients=10000, instance_count=10, reserved_for_ops=200, safetyFactor=1.2
  → floor((10000 - 200) / 10 / 1.2) = floor(9800 / 10 / 1.2) = floor(816.7) = 650

  directMaxConnections（DIRECT+Semaphore）同理：
  directMaxConnections ≤ floor((maxclients - reserved_for_ops) / instance_count / safetyFactor)
```

#### 6.13.4 监控告警基线

```
Redis 侧:
  connected_clients > maxclients × 0.7  →  P1 警告
  connected_clients > maxclients × 0.9  →  P0 紧急
  rejected_connections delta > 0        →  P0 紧急（当前正在拒绝连接！）

客户端侧:
  poolActive/poolMaxTotal > 0.8         →  池即将耗尽
  semaphoreUsed/semaphoreMax > 0.8      →  许可即将耗尽
  poolNumWaiters / semaphoreQueueLength持续增长 → 立即扩容

系统层:
  ulimit -n 使用率 > 80%                →  OS 文件描述符瓶颈
  ephemeral port 使用率 > 60%          →  客户端端口可能耗尽
```

#### 6.13.5 核心建议（一条条可执行）

```
1. 不要让 Worker 无限制 connect(): 客户端侧必须有硬上限（Pool 或 Semaphore）
2. 命令型调用优先使用连接池（pool），Pub/Sub 用专用独立长连接或单独 pool
3. 高吞吐短命命令 + 需要 Pub/Sub → DIRECT + Semaphore（或 Semaphore + 池组合）
4. poolMaxTotal 按公式计算：务必预留运维/监控连接名额
5. 连接池异步预热：不阻塞启动，预热失败优雅降级（不抛异常）
6. 监控 rejected_connections 的 delta（增长速率），而非只看累计值
7. 压测时同时收集系统层指标（ulimit、ephemeral ports、TCP 连接数、网络丢包）
8. Semaphore 公平模式（fairness=true）避免饥饿，但略降吞吐——根据场景选择
9. 以实际 INFO clients 返回的 maxclients 为准（不同发行版/镜像可能不同）
10. blocked_clients 表示阻塞命令等待中的客户端（BLPOP/XREAD），非被拒绝的连接
```

#### 6.13.6 一句话结论

> **DIRECT+Semaphore 以仅 10 条连接打满 ~155k cmd/s 吞吐 + Redis 零拒绝 + Pub/Sub 天然支持，是单租户高吞吐场景的最优方案。无论选哪种方案，都必须把 maxclients / reserved_for_ops / instance_count 纳入配置和监控策略。**

```
