# Redis 高并发缓存架构实战手册

> 实战总结：基于本项目的真实压测数据（场景 2/5/7/8-1/8.21/9.18/11）整理，涉及缓存穿透/击穿/雪崩、延迟任务、概率算法、持久化、集群、编码选型等核心主题。

---

## 1 缓存三大经典问题与解决方案

### 1.1 缓存穿透

**现象**：查询 Redis、数据库均不存在的数据，大量请求直达数据库，压垮 DB。

**解决方案**：

1. **空值缓存**：无效 Key 缓存空值，TTL 控制 1~5 分钟（短期兜底，避免永久占用内存）；
2. **布隆过滤器前置拦截（生产首选）**；
3. **接口参数校验、网关限流防刷**，拦截恶意请求。

#### 1.1.1 布隆过滤器生产完整规范

**1. 核心作用**

拦截数据库、Redis 均不存在的非法/无效 Key，大幅缓解缓存穿透，保护数据库压力。

**2. 存储位置（关键）**

生产两种架构按需选择：

- **本地布隆过滤器（Guava）**：位图存放 JVM 本地内存，无网络开销、查询极快；只存储有效 Key 哈希位图，不保存明文 Key，内存占用极低。
- **分布式布隆过滤器（Redisson BloomFilter）**：位图存储在 Redis，适合多实例集群、需要全局统一有效 Key 集合场景，存在少量网络开销。

**Cluster / 多实例部署要点**：

- **Redis Cluster 场景**：若采用 Redis 存储位图，需确保 RedisBloom 模块在**每个节点已一致部署**，并考虑槽分片（单个大 Key 位图会落到单槽）。生产上常把布隆**按时间/业务维度分片**（如 `bf:sku:2026`），避免单槽/单 Key 过大，也便于按周期滚动重建。
- **Guava 本地布隆**：适合单实例/对延迟敏感场景，但**多实例部署时各进程位图会短暂不一致**——需配合定期全量同步、或通过消息总线（Kafka/MQ）广播新增元素，以降低不一致窗口。
- **初始化容量**：明确按**历史峰值 × 冗余（2~4×）**预估，并确定预估周期（如 1 年、业务高峰）。对填充率设置监控与告警（阈值参考 60%/75%/90%），超出即触发重建/扩容，避免超载后误判率急剧上升。

**3. 核心特性**

- 无假阴性：判定不存在 → 一定不存在，可以直接拦截。**前提**：标准布隆在未删除元素、实现正确且无位翻转/损坏时保证无假阴性；若使用计数布隆或错误删除/实现缺陷，可能出现例外（计数器溢出等异常结果）；
- 存在假阳性：判定存在 → 有可能不存在，仍需要放行查询 Redis/DB 兜底；
- 原生不支持精准删除：多个 Key 共用 bit 标记，删除单个 Key 会污染其他数据。

**4. 生产痛点应对**

- 新增业务数据：同步调用 `add()` 写入布隆过滤器；
- 删除业务数据：无法即时删除，采用**定时全量重建**方案；
- 一致性：定时重建保证过滤器集合与 DB 有效数据对齐；
- 误判率：合理预估容量与哈希函数个数，常规业务控制误判率 0.01%～0.1%。

**容量预估标准公式（生产必算）**：

- 位数组大小 m（**bit 单位**）：`m = -(n × ln p) / (ln 2)²`（n=预估最大元素数，p=目标误判率）；
- 哈希函数个数 k：`k = (m/n) × ln 2 ≈ 0.7 × (m/n)`；
- 例：n=1 亿、p=0.01% → m≈958MB、k≈7；**实际内存 ≈ m/8 字节**（bit→byte）；
- **容量必须按历史峰值 × 冗余（建议 2~4 倍）预估**，并配套监控与自动告警（填充率阈值 60%/75%/90%），避免超载；超出容量后误判率急剧上升，且无扩容手段（需重建）。

**5. 架构取舍**

- 追求极致性能、单体应用：优先本地 Guava 布隆；
- 分布式多实例要求全局统一视图：选用 Redis 分布式布隆；
- ❌ 禁止使用 Redis Set 存储全部有效 Key 替代布隆，海量 Key 会造成严重内存开销与查询衰减。

**6. 布隆过滤器优缺点总结**

✅ **优点**

1. 内存占用极低，仅保存 bit 位图，亿级数据仅需百 MB 级别；
2. 查询 O(1)，本地布隆无网络 IO，性能优异；
3. 可以可靠拦截不存在的非法请求，是防穿透主流方案；
4. 不存在假阴性，拦截逻辑安全，不会误杀正常请求；
5. 海量数据下性能稳定，适合 ID 类离散 Key 场景。

❌ **缺点 & 生产约束**

1. 存在假阳性误判，无法彻底消除，必须保留 DB 兜底查询；
2. 不支持就地删除元素；
3. 数据存在滞后，DB 删除数据无法同步剔除；
4. 创建前必须预估最大容量，数据超出阈值后误判率急剧上升；
5. 本地布隆多实例部署时，各进程过滤器数据存在短暂不一致。

#### 1.1.2 布隆过滤器无法删除元素的生产解决方案

标准布隆过滤器基于 bit 位图实现，不支持单个元素删除。DB 物理删除数据后，过滤器无法同步剔除，会导致该无效 key 持续放行查询，缓存穿透防护失效。生产可选方案：

1. **优先业务逻辑删除**：业务不执行 DELETE，使用 `is_deleted` 逻辑标记，有效主键集合稳定，无需从布隆移除数据，架构最简单。
2. **定时全量重建（主流方案）**：定时任务拉取数据库全部有效主键，构建全新布隆过滤器，原子替换旧实例；允许存在一段不一致窗口，适合删除频率较低业务。
3. **计数布隆过滤器 CountingBloomFilter**：使用多位计数器替代单 bit，支持 add/delete 实时操作。**注意**：需要为每个位维护计数器，计数器位宽（如 4-bit/8-bit）决定最大计数，存在**溢出风险**；内存占用随位宽线性上升。若删除非常频繁且数据量大，应评估计数器位宽与溢出处理策略，或考虑 CuckooFilter。
4. **布隆 + 墓碑 ZSet**：额外维护一个存放已删除 ID 的墓碑集合；布隆判定存在后，再校验是否在墓碑列表中，拦截已删除数据。**代价**：读路径需二次查询墓碑（增加一次读开销），且墓碑集合（ZSet/List/Set）会随时间膨胀——**必须定期压缩与清理**（TTL 或批量清理），并对墓碑成长设置监控与清理策略。

> 🔴 **重要红线**：布隆过滤器存在假阳性，任何方案下都不能省略 Redis、数据库兜底查询。

### 1.2 Keyspace Notifications（Redis 键空间通知）

**1. 核心概念**

Redis 原生基于 Pub/Sub 实现的键事件通知。监听 Key 创建、更新、删除、过期等生命周期事件，主动推送消息给订阅端，无需业务轮询探测 Key 状态。

> ⚠️ 默认关闭：`notify-keyspace-events ""`，开启会消耗主线程 CPU，非必要不启用。

**2. 基础配置**

仅监听过期事件（最常用最小配置）：

```
# 开启 E(键事件) + x(过期)，订阅 __keyevent@<db>__:expired
notify-keyspace-events Ex
```

标识说明：

- `E`：键事件通知（keyevent，事件视角）——channel 名为 `__keyevent@<db>__:<event>`
- `K`：键空间通知（keyspace，Key 视角）——channel 名为 `__keyspace@<db>__:<key>`
- `x`：过期事件（expired）

> 说明：若需要接收某 key 的过期事件，用 `Ex` 并订阅 `__keyevent@0__:expired`（`0` 为 db 号）。若想要 **keyspace（按 key 名）视角**（接收 `__keyspace@...__:<key>`），需加 `K` 标志（如 `Kx` 或 `KEx`），但会带来更高主线程开销，需评估。

> ⚠️ 生产准则：按需开启，不要无脑全开所有事件。常用最小配置为 `notify-keyspace-events Ex`。

**3. 适用场景（仅适合弱可靠异步辅助场景）**

1. **缓存过期异步预热**：监听 Key 过期事件，异步回源重建缓存，降低热点 Key 大面积过期击穿风险；仅作为优化手段，不能完全杜绝缓存击穿，仍需要互斥锁、本地缓存兜底。
2. **Key 运维审计与内存监控**：采集 Key 新增、删除、过期指标，统计过期速率、冷热 Key 分布、大 Key 变更，用于内存水位分析、故障排查。
3. **临时资源联动清理**：会话 Key、验证码、临时令牌过期后，异步清理下游碎片数据，实现资源自动回收。
4. **分布式锁释放感知（纯优化）**：监听锁 Key 删除/过期事件，减少业务空轮询；**严禁依靠该机制实现可靠锁等待逻辑**。

**4. ❌ 禁止使用场景（生产红线）**

1. **精准定时任务**：过期事件推送时机不等于 TTL 到期时刻，受惰性删除、定时抽样删除影响，延迟不可控。
2. **可靠业务消息、流程驱动**：底层 Pub/Sub 无持久化、无堆积、无 ACK、无重试，客户端断线期间事件直接丢失。
3. **海量 Key 高并发过期实例**：持续大量事件分发抢占主线程 CPU，抬高 P99 延迟，引发实例抖动。
4. **Redis Cluster 全局监听**：事件仅在 Key 所属槽节点产生，无法单客户端统一订阅全集群事件，架构复杂。**若要全量监听**，需在每个主节点上部署订阅端，或用独立 Proxy/Sidecar 把各节点事件**汇聚到消息总线**（Kafka 等），或在应用层通过客户端连到每个主节点分别订阅。

**5. 核心缺陷**

- 消息不保证送达，存在丢失；
- 事件触发延迟不可预测；
- 高负载场景损耗 Redis 主线程；
- 只适合弱一致、可容忍消息丢失的辅助业务。

**6. 落地规范**

1. 使用最小权限事件配置，尽量只开启 `Ex`；
2. 订阅回调内部逻辑投递独立线程池异步执行，禁止阻塞订阅线程；
3. 核心 Redis 实例尽量不开启，可使用独立 Redis 实例承载事件监听；
4. 业务代码必须容忍事件丢失、延迟，不能强依赖事件。

**7. 架构定位区分**

- 布隆过滤器：请求入口前置拦截，解决缓存穿透；
- Keyspace 通知：Key 生命周期后置回调，用于异步预热、运维审计、资源清理；

> 二者互补，不可相互替代。

### 1.3 缓存击穿

**现象**：热点 Key 过期瞬间，大量并发请求同时穿透到数据库。

**方案**：

1. 互斥锁（分布式锁）：同一时刻只放行一个请求回源；
2. 热点 Key 永不过期（逻辑过期方案）；
3. 本地缓存叠加分布式缓存，削峰流量；
4. 提前主动刷新热点缓存。

**互斥锁安全实践**：

- **推荐原子方案**：`SET key value NX PX ttl` + **随机唯一 value** + **Lua 原子解锁**（判断 value 匹配再 DEL，防误删他人锁），并实现幂等/超时回退（锁失效后仍要回源，避免雪崩）。
- **慎用 RedLock**：需理解其安全边界（依赖时钟/网络条件），不盲目采用；锁持有期间业务超时需有回退策略。
- **结合本地单 flight / 请求合并**：高 QPS 下用应用内 singleflight 把并发回源合并为 1 个，其他请求等待或复用旧值（逻辑过期），减少对分布式锁的依赖、降低延迟。

### 1.4 缓存雪崩

**现象**：大量 Key 集中同时过期，大量请求压垮数据库。

**方案**：

1. TTL 增加随机偏移量，打散过期时间；
2. 分层缓存、多级缓存；
3. Redis 高可用部署（主从/哨兵/Cluster），防止实例宕机雪崩；
4. 接口限流、熔断、降级；
5. 定时主动批量刷新缓存，避免集中失效；
6. **主动预热**：对已知热点数据错峰预热（冷/温/热分层缓存），避免瞬时集中回源；
7. **降级策略**：Redis 故障时返回**缓存降级视图**（本地二级缓存/静态兜底页面），保护下游 DB。

### 1.5 缓存一致性策略

**核心策略：Cache-Aside（旁路缓存，应用主动维护）**：

- **读**：先查缓存，未命中回源 DB 并写缓存；
- **写**：先更新 DB，随后删除缓存（或先写 DB 再删缓存），避免并发脏读；
- 必要时使用**延时双删**或**消息队列**保证最终一致（详见第 9 章）。

**write-through / write-back 区别与适用**：

| 策略 | 强一致 | 写延迟 | 风险 |
|:--|:--:|:--:|:--|
| **write-through**（写穿透）| 高 | 高（每次同步写 DB）| 低 |
| **write-back**（写回）| 低 | 低（先写缓存，异步落 DB）| 高（缓存丢则数据丢）|

- **write-through**：适合强一致但写频率不高的场景；
- **write-back**：写延迟低但风险高，仅在可容忍缓存丢失、有补偿机制的场景使用。

**双删与延时双删要点**：首次删缓存 → 更新 DB → **延时（几百 ms）再删一次**（抵消"回源写回旧值"的竞态窗口），并保证删除顺序与补偿重试。

> **推荐实践**：对强一致数据**不缓存**或采用**短 TTL + 严格写入策略**，宁可多打 DB 也不容忍脏读。

### 1.6 本地缓存选型（Caffeine / Guava / Ehcache）

| 维度 | Caffeine | Guava | Ehcache |
|:--|:--:|:--:|:--:|
| 并发策略 | 最优（Window TinyLfu）| 良好 | 一般 |
| 淘汰算法 | W-TinyLfu | LRU/LFU | LRU/TTL |
| 性能 | 最优 | 好 | 一般 |
| 适用 | 生产优先 | 轻量场景 | 需磁盘/持久化 |

**结论**：**Caffeine 优于 Guava**（更优的并发策略、Window TinyLfu 淘汰算法），生产优先 Caffeine，本地缓存配合 LRU/LFU 与容量/负载监控。

**本地缓存与分布式缓存一致性**：本地缓存仅作为**短期缓冲**，过期/失效需考虑通知（Keyspace 通知）或消息、版本号策略，避免长时间脏读。

### 1.7 请求合并 / 单 flight 防击穿实践

- 在应用层使用 **singleflight**（Go）或 **request coalescing**（Java）将并发回源请求**合并为 1 个**；
- 其他并发请求**等待同一个 in-flight 结果**，或直接返回旧值（逻辑过期/本地缓存）；
- 效果：极大降低 DB 压力，配合分布式锁兜底，是防击穿的高效补充手段。

### 1.8 分布式锁实践注意

- 用 `SET key value NX PX ttl` + **随机 value** + **Lua 原子释放**（判断 value 匹配再 DEL，防误删他人锁）；
- 为锁加上 **holder id 与 TTL**，锁持有者完成后用 Lua 判断 value 再释放；
- **RedLock 在分片环境的安全取决于时钟与网络条件，不应盲目使用**；
- 锁持有期间业务超时需有回退策略（幂等/重试/降级）。

---

## 2 Redis 原生结构实战：ZSet 实现全局延迟任务队列

### 2.1 核心原理

利用 **ZSet（Sorted Set）自动按 score 有序排序** 的特性：

- `score`：**任务执行时间戳（毫秒）**
- `member`：**全局唯一任务 ID**

任务按照执行时间从小到大排序，到期任务排在队列最前方。
典型场景：订单超时关单、优惠券超时回收、定时消息提醒、会话超时清理。

### 2.2 基础使用流程

#### 2.2.1 生产者：提交延迟任务

```redis
ZADD delay:task:queue 1786412300000 "order_9527"
```

✅ **生产标准优化方案**：member 只存放唯一 taskId，完整任务信息存入 Hash，减少 ZSet 负载，支持任务主动取消：

```redis
HSET delay:task:info order_9527 '{"type":"close_order","orderId":"9527"}'
ZADD delay:task:queue 1786412300000 "order_9527"
```

#### 2.2.2 消费者轮询拉取到期任务

> ⚠️ **禁止先查询再删除**，非原子操作，分布式环境引发重复消费。

##### 方案 1：Lua 脚本原子批量获取 + 删除（通用推荐）

```lua
-- KEYS[1] ZSet队列名，ARGV[1] 当前时间戳，ARGV[2] 单次拉取数量（建议 100~1000）
local items = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1], 'LIMIT', 0, ARGV[2])
if #items > 0 then
    redis.call('ZREM', KEYS[1], unpack(items))
end
return items
```

原子捞出到期任务并从 ZSet 移除，抢到任务的消费者独占处理。

> ⚠️ **安全性**：`unpack(items)` 在 items 很大时会超出 Redis Lua 返回/命令参数限制并引发性能问题。**必须对单批数量设上限（建议 100~1000）**，并在需要处理更多时**分块循环**（先 LIMIT 拉一批 → 删除 → 再拉下一批），避免单次 `ZRANGEBYSCORE` 返回海量数据占用主线程内存/CPU。

**Lua 原子迁移到 processing 示例（伪代码）**：

```lua
-- KEYS[1]=queue, KEYS[2]=processing, ARGV[1]=now, ARGV[2]=batchSize, ARGV[3]=owner
local items = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1], 'LIMIT', 0, ARGV[2])
for i = 1, #items do
    redis.call('ZREM', KEYS[1], items[i])
    redis.call('HSET', KEYS[2], items[i], cjson.encode({claimAt = ARGV[1], owner = ARGV[3], retries = 0}))
end
return items
```

> 生产请对 batchSize 做上限并分块删除，避免单次 unpack 过大。

##### 方案 2：ZPOPMIN（Redis 5.0+，适合低并发）

```redis
ZPOPMIN delay:task:queue 1
```

取出 score 最小任务，判断时间戳：未到期重新放回 ZSet，线程休眠。
缺点：大量未到期任务造成频繁空轮询，CPU 消耗高。

> ⚠️ **ZPOPMIN 是破坏性弹出**（取出即从 ZSet 移除），未到期任务需 ZADD 重新放回，放回时机/竞态处理较繁琐。**高并发场景**下大量未到期任务被弹出后放回，会引发频繁写入抖动；且弹出后再判断 score ≤ now 的"检查-放回"存在竞态。**通用作法仍是用 ZRANGEBYSCORE + Lua 原子移除或迁移**（只读查询 + 原子删除），只在"到期任务占绝对多数、几乎无空轮询"时才适合 ZPOPMIN。

#### 2.2.3 标准消费流程

1. 定时执行 Lua 脚本批量获取到期 taskId；
2. 通过 taskId 从 Hash 读取任务详情；
3. 异步执行业务逻辑；
4. 执行成功：删除 Hash 内任务数据；
5. 执行失败：根据重试策略重新计算延迟时间，放回队列；
6. 达到最大重试次数，转入死信队列。

### 2.3 分布式环境核心问题与解决方案

#### 2.3.1 惊群效应（多实例同时轮询争抢任务）

- Lua 脚本原子抢占，天然互斥；
- 消费者轮询间隔增加随机抖动（0.5s~1s），避免同一时刻集中请求 Redis；
- 高并发场景可增加分布式锁控制扫描权限。

#### 2.3.2 任务丢失风险（取出任务后进程宕机）

✅ **可靠三层架构（生产落地标准）**

1. `delay:task:queue`：ZSet 待调度延迟任务
2. `delay:task:processing`：Hash，存放【处理中任务 + 认领信息】
3. `delay:task:dead`：List 死信队列

**执行流程**：

1. Lua 捞出到期任务**不直接删除**，迁移至 processing 集合；
2. 消费者认领任务，写入 processing 记录认领时间；
3. 业务执行完成删除 processing 记录；
4. 独立守护线程定期扫描 processing，认领超时未完成任务回迁 ZSet 重试。

**processing 实现细节（防丢/防重复）**：

- **记录字段**：应包含**认领者 ID、认领时间戳、重试次数**（`{owner, claimAt, retries}`）。
- **回迁原子性**：守护线程回迁时需判断 `claimTime + processing_ttl < now`，且**用 Lua 脚本原子检查 processing 记录未被新认领**后再 ZADD 回队列，避免"超时判定"与"重新认领"竞态。
- **幂等**：业务处理必须幂等（建议幂等 key / 幂等 token），即使同一任务被回迁重试多次，也不能重复扣款/关单等副作用。

#### 2.3.3 大 Key 风险

海量任务堆积导致单个 ZSet 过大，`ZRANGEBYSCORE` 查询变慢。
解决：**按日期分片 ZSet**：

```
delay:task:queue:20260811
delay:task:queue:20260812
```

#### 2.3.4 任务主动取消

```redis
ZREM delay:task:queue order_9527
HDEL delay:task:info order_9527
```

### 2.4 优缺点总结

✅ **优点**

1. 仅依赖 Redis 原生数据结构，无需额外中间件；
2. 延迟精度支持秒级/毫秒级；
3. 支持动态新增、主动撤销任务；
4. 相比数据库轮询，数据库压力极低。

❌ **缺点**

1. 需要后台线程持续轮询，存在空轮询消耗；
2. 投递模型为**至少一次消费**，业务必须实现幂等；
3. Redis 未开启 AOF 持久化，宕机会丢失任务。**生产建议 RDB + AOF(everysec)**，并为 AOF 打开 rewrite 机制与慢写监控；需权衡恢复时间与丢失窗口（everysec 最多丢 1 秒 vs always 高写入时性能下降）。若对任务丢失极敏感，需在业务层做持久化副本（数据库/消息队列），或 DB+Redis 双写 + 补偿机制；
4. 不适合十万级以上超大吞吐延迟任务；
5. Redis Cluster 场景下，单个 ZSet 所有数据固定在同一个槽，无法分片。

### 2.5 适用场景 & 不适用场景

✅ **适合**：中小型延迟任务——订单超时关单、短信延迟提醒、会话超时清理、短时间延迟调度。

❌ **不适合**：

1. 金融级高可靠任务；
2. 百万级海量延迟消息；
3. 要求严格毫秒级精准触发；
4. 集群大规模调度场景（优先 RocketMQ 延迟消息）。

### 2.6 与 Keyspace Notifications 对比

- Keyspace 通知：事件易丢失、延迟不可控，只能用作缓存预热等辅助场景，**严禁实现业务定时任务**；
- ZSet 延迟队列：主动轮询调度，可控性强，是 Redis 实现延迟任务主流方案。

### 2.7 生产落地规范

1. member 使用全局唯一任务 ID，任务载荷外置 Hash 存储；
2. 使用 Lua 脚本保证查询与迁移/删除原子性，防止重复消费；
3. 引入 processing 处理中集合，解决进程宕机任务丢失；
4. 轮询间隔设置合理，增加随机抖动防惊群；
5. Redis 开启 AOF 持久化，降低任务丢失风险；
6. 消费逻辑强制幂等；配置重试策略与死信队列；
7. 任务量大时按时间分片 ZSet，规避大 Key；
8. 增加数据库定时巡检作为兜底补偿。

---

## 3 概率型近似算法组件（HLL / BloomFilter / CuckooFilter / CMS / Top-K / T-Digest）

> **通用前提**：全部属于**概率近似算法**，牺牲计算精度换取极致低内存占用；仅适合允许误差的业务统计、过滤场景，不可用于计费、交易等强精准场景。
>
> **分类**：
> - 成员存在判定：BloomFilter、CuckooFilter
> - 数据流聚合统计：HLL、CMS、Top-K、T-Digest
>
> **⚠️ 模块依赖（重点）**：除 HLL 外，Bloom/Cuckoo/CMS/Top-K/T-Digest 在 Redis 上通常通过 **RedisBloom 模块**提供；若不能安装模块，应在**应用端**（Guava/Caffeine/专门库）实现，或考虑外部服务（Kafka + Flink）替代。集群环境引入 RedisBloom 需在所有节点一致安装并注意模块版本/许可/编译。

### 3.1 HyperLogLog（HLL）—— 估算基数（独立去重数量）

Redis 原生命令：`PFADD、PFCOUNT、PFMERGE`

#### 3.1.1 核心能力

估算集合内不重复元素总量（基数/UV），**不存储原始数据**；单个 HLL **默认占用约 12KB（取决于精度参数 p，Redis 默认）**，数据量增长内存几乎不变；标准误差约 2%。如需更高精度可按需调整精度参数，但会增加内存。

#### 3.1.2 适用场景

✅ **适合**

1. 页面 UV、活动 UV、渠道独立访客统计；
2. 内容每日独立播放用户去重统计；
3. 多条流量维度 UV 合并计算（PFMERGE 合并多个 HLL）。

❌ **不适合**

1. 需要精确 UV 数值；
2. 需要获取用户 ID 明细；
3. 需要删除元素（原生 HLL 不支持删除）。

#### 3.1.3 架构坑点

1. 不要按单个用户/单个商品创建独立 HLL Key，会造成 Key 爆炸；
2. 适合按天、活动、频道等粗粒度聚合；
3. 结果存在误差，报表展示需要标注近似值。

### 3.2 BloomFilter 布隆过滤器 —— 成员存在判定

#### 3.2.1 核心能力

判断元素：**一定不存在 / 可能存在**。
无假阴性、存在假阳性；标准版本**不支持删除元素**。

#### 3.2.2 适用场景

✅ **适合**

1. 缓存穿透前置拦截（核心场景）；
2. URL 爬虫去重、黑白名单校验；
3. 商品 ID、账号白名单快速过滤。

❌ **不适合**

1. 需要频繁删除元素；
2. 业务无法容忍假阳性（必须保留下游兜底查询）；
3. 实际元素数量远超初始化容量，误判率飙升。

### 3.3 CuckooFilter 布谷鸟过滤器

#### 3.3.1 核心能力

同样做成员存在判定，无假阴性、存在假阳性；最大特性：**原生支持元素删除**；基于双哈希桶 + 驱逐置换实现。

#### 3.3.2 适用场景

✅ **适合**

1. 需要动态增删的黑名单：用户封禁/解封、商品上下架；
2. 不想使用内存开销翻倍的计数布隆过滤器场景。

❌ **不适合 / 注意**

1. 负载因子上限低于布隆过滤器，填充率过高性能暴跌；
2. Redis 无原生支持，依赖 RedisBloom 模块，运维成本更高；
3. **高负载/高填充率时可能插入失败（需要扩容或重建）**——生产上需添加监控（插入失败率），并预先规划重建/扩容策略。

#### 3.3.3 BF vs CuckooFilter 选型结论

- 只新增、极少删除 → **BloomFilter**（成熟、内存利用率高）；
- 频繁增删元素 → **CuckooFilter**。

> 很多 Redis 集群为了减少模块依赖，依然选择「标准 BF + 定时重建」方案替代布谷鸟过滤器。

### 3.4 Count-Min Sketch（CMS）最小计数草图

#### 3.4.1 核心能力

估算数据流中单个元素出现频次；查询计数 ≥ 真实值（存在高估冲突误差）；内存占用极低。

#### 3.4.2 适用场景

✅ **适合**

1. 海量流量下粗略统计 IP、用户、接口访问频次；
2. 风控初步识别高频爬虫、恶意访问；
3. 作为 Top-K 前置筛选器，过滤海量长尾元素。

❌ **不适合**

1. 需要精确计数（不能替代 String 计数器）；
2. 无法消除哈希冲突带来的计数高估。

### 3.5 Top-K（HeavyKeeper）—— 实时最热 TOP 元素

#### 3.5.1 核心能力

持续对流式数据统计，动态维护出现频率最高的 K 个元素与近似频次。RedisBloom 提供实现。

#### 3.5.2 适用场景

✅ **适合**

1. 实时热点商品、直播间 TOP 榜单；
2. 识别热点 IP、高频爬虫；
3. 自动发现 Redis 热点 Key；
4. 热搜词初步统计。

❌ **不适合**

1. 需要绝对精确、完整有序榜单；
2. 仅关心长尾数据，无热点统计需求。

#### 3.5.3 典型架构链路

网关流量采集 → CMS 全量粗略计数 → Top-K 筛选热点候选；
仅对筛选出的热点建立精确计数器，极大节省内存。

### 3.6 T-Digest —— 近似分布统计（分位数 P50/P95/P99）

#### 3.6.1 核心能力

不存储全部原始数据，聚合数值分布；快速计算中位数、P95、P99、最大最小值。

#### 3.6.2 适用场景

✅ **适合**

1. 接口响应耗时实时监控，统计 P99 延迟；
2. 订单金额、充值金额分布分析；
3. 消息消费耗时、压测延时指标聚合。

❌ **不适合**

1. 需要保存全部原始明细数据；
2. 需要精准单点数值。

### 3.7 概率算法横向对比汇总表

| 算法 | 核心用途 | 是否支持删除 | 误差特征 | Redis 原生支持 |
|------|------|:--:|------|------|
| HLL | 估算 UV/基数（去重总数）| ❌ | ±2% | ✅ PF 原生命令 |
| BloomFilter | 判断元素是否存在 | ❌ 标准版 | 假阳性 | ❌ 依赖 Redisson/RedisBloom |
| CuckooFilter | 判断元素是否存在 | ✅ | 假阳性 | ❌ RedisBloom 模块 |
| Count-Min Sketch | 估算元素访问频次 | ✅ | 计数高估 | ❌ RedisBloom 模块 |
| Top-K (HeavyKeeper) | 找出热度最高 K 个元素 | ✅ | 频次近似 | ❌ RedisBloom 模块 |
| T-Digest | 计算分位数 P50/P95/P99 | ✅ | 分布近似 | ❌ RedisBloom 模块 |

### 3.8 架构师通用选型原则

1. 统计独立去重数量（UV）→ **HyperLogLog**
2. 判断元素是否存在
   - 只增少删 → **BloomFilter**
   - 频繁增删 → **CuckooFilter**
3. 粗略统计海量元素访问频次 → **Count-Min Sketch**
4. 实时抓取最热 TOP 榜单 → **Top-K (HeavyKeeper)**
5. 延时、金额等数值分位数监控（P99）→ **T-Digest**

### 3.9 生产落地红线

1. 所有概率算法输出均为近似值，禁止用于计费、资金交易等强精确业务；
2. BloomFilter/CuckooFilter 存在假阳性，判定"存在"必须保留下游兜底查询；
3. Redis 原生仅支持 HLL，其余组件大多依赖 RedisBloom 扩展模块；集群环境引入模块会提升运维复杂度，简单业务慎重引入；
4. 不允许安装 Redis 模块时，优先在应用本地内存实现（Guava 布隆等），不要强行依赖 Redis。

### 3.10 概率数据结构 vs 精确存储实测对比（场景 9.18）

| 方案 | 内存占用 | 精度 | 适用 |
|------|:--:|:--:|------|
| **精确 Set（SADD）** | 随元素数线性增长（高）| 100% | 需精确去重/成员查询 |
| **HyperLogLog（PFADD）** | 固定 12KB/Key | ±2% | 大基数 UV 估算 |
| **BloomFilter** | bit 位图（低）| 假阳性 | 存在性判定（防穿透）|
| **Count-Min Sketch** | 极低 | 计数高估 | 频次统计 |

**核心取舍**：精确 Set 数据量越大内存线性膨胀且遍历越慢；HLL 固定内存但只给近似 UV；Bloom 只回答"是否存在"。**UV 估算用 HLL、防穿透用 Bloom、频次粗筛用 CMS**，都是牺牲精度换内存/性能的典型权衡——禁止用于计费、交易、需要明细的场景。

---

## 4 Redis 持久化机制

### 4.1 RDB（快照持久化）

**原理**：按照配置策略 fork 子进程，将全量内存数据持久化到二进制 `.rdb` 文件。

**优点**：

- 文件紧凑，恢复速度快；
- fork 子进程，主线程短暂阻塞；
- 适合冷备份。

**缺点**：

- 两次快照之间存在数据丢失风险；
- fork 大内存进程会产生 STW 阻塞；
- 不适合要求极高数据可靠性场景。

### 4.2 AOF（日志持久化）

**原理**：记录每条写命令追加到 aof 文件。

**三种刷盘策略**：

- `always`：每条命令 fsync，最安全，性能最差；
- `everysec`：每秒刷盘，默认方案，最多丢失 1 秒数据；
- `no`：交由操作系统控制，丢失数据不可控。**仅在对极致吞吐且可容忍较大丢失的场景考虑**——系统崩溃时可能丢失大量数据，**不推荐在业务数据关键的生产环境使用**。

**AOF 重写**：压缩日志，消除冗余命令，减少文件体积；生产应开启 rewrite 机制并对 rewrite 耗时/频率设置慢写监控。

**优点**：数据丢失粒度小；
**缺点**：文件体积更大，重启恢复慢。

### 4.3 生产推荐组合

`RDB + AOF everysec`，兼顾备份与数据安全。

**实测量化（场景 2，50 万大 Hash 批量写入 + 256MB 内存限制）**：

| 持久化模式 | 子进程存活 | COW 大小 | 内存压力 |
|:--|:--:|:--:|:--:|
| RDB | ~2s | **~245MB** | 高 |
| AOF percentage=100 | ~2s | **~66MB**（最小）| 最低 |
| AOF percentage=500 | ~3s | ~264MB | 最高 |

**关键结论**：

1. **COW 大小 = 父进程在子进程存活期间写入触发的 copy-on-write 字节数**，与写入速率和 fork 持续时间强相关；
2. **OOM 估算公式**：所需物理内存 ≈ `used_memory_rss + last_cow_size + 10~20%` 裕度，保守按 `used_memory_rss × 1.5~2`；
3. **根除 COW 风险**：主节点关闭持久化 + 副本做持久化/备份 + 副本错峰 BGSAVE；
4. 生产必须关闭 THP（见 10.1），否则 fork 大内存进程 COW 成倍放大。

---

## 5 主从复制、哨兵、Redis Cluster 集群

### 5.1 主从复制

**作用**：主库负责写操作并同步到从库，从库主要用于读扩展与备份（写入仍由主库承担）。

**复制流程**：全量同步（RDB 传输 / PSYNC fallback）→ 增量复制（复制积压缓冲区 + 流式复制）。

**需关注**：复制延迟、复制积压大小、partial resync 成功率与 replication offset，避免因从库滞后读到陈旧数据或主从切换时数据丢失。

**风险点**：复制风暴、主从延迟、从库只读默认配置。

> ⚠️ 注意 **replication backlog**（大小影响 partial resync 能力）与 `repl-diskless-sync`、`repl-backlog-ttl` 等配置；监控 **replication offset** 与复制延迟（`master_repl_offset`、`slave_repl_offset` 差值）。

### 5.2 哨兵（Sentinel）

Sentinel 为 Redis 提供**监控、通知与自动故障转移**。生产务必配置足够 sentinel 节点（**常为奇数**），并调整 `quorum`、`down-after-milliseconds`、`failover-timeout`、`parallel-syncs` 等参数（影响容灾行为与自动切主）。

**运维巡检命令**：

```
SENTINEL masters
SENTINEL sentinels <master-name>
SENTINEL get-master-addr-by-name <master-name>
```

**局限**：哨兵**无法横向扩展写能力**，仅用于高可用与自动切主；需合理设置 quorum 并监控 sentinel 可用性，选主策略与运维流程配合以避免脑裂。适合中小规模架构。

### 5.3 Redis Cluster（分片集群）

**核心机制**：

- 16384 个槽位，数据分片存放，通过 **gossip 协议**维护节点状态；
- 客户端遇 **MOVED/ASK** 重定向后自动重试；
- 跨槽多 key 操作会触发 **CROSSSLOT 错误**（必须用 `{hashtag}` 强制同槽，或让客户端拆分到多节点）；
- 键空间通知无法全局订阅；
- **槽迁移/重分片需谨慎**（`CLUSTER SETSLOT`、`MIGRATE`、reshard 工具），迁移期间注意请求重定向成本与短时不一致。

**Cluster 水平扩展说明**：集群只在 key 均匀分布到多个主节点时才体现写吞吐扩展优势。进行 re-shard/slot migration 时使用 `CLUSTER SETSLOT + MIGRATE` 或 `redis-cli --cluster reshard` 工具。

**运维命令速查**：

```
CLUSTER INFO
CLUSTER NODES
CLUSTER SLOTS
CLUSTER REPLICATE <node-id>
CLUSTER FAILOVER
CLUSTER SETSLOT <slot> <IMPORTING|MIGRATING|NODE>
```

**CROSSSLOT 实测关键结论（场景 7，3 主 3 从）**：

- 现代 Cluster 客户端（如 **Lettuce/Redisson Cluster**）会**按 slot 自动分组并拆分命令**以避免 CROSSSLOT 错误（SETEX ~170k/s、HGET ~390k/s）；
- **若绕过智能客户端，直接向单节点发送跨槽 multi-key 命令**，会触发 CROSSSLOT 错误或性能极低（0.4 cmd/s）；
- 因此跨槽问题是"绕过智能客户端 / 直连单通道"才触发，**用官方集群客户端即可自动规避**；
- 多 key 运算（MSET/MGET/ZUNIONSTORE）跨槽需用 `{hashtag}` 强制同槽，或客户端自动拆分；
- **注意**：实验数值依赖具体客户端实现与测试场景，结论应结合测试环境说明。

---

## 6 Redis 集合底层编码详解

### 6.0 String 底层编码：int / embstr / raw

Redis String 存在三种底层实现，由 value 自动决定：

| 编码 | 触发条件 | 内存布局 |
|:--:|------|------|
| **int** | value 是 64 位有符号整数 | 直接存 8 字节 `long`，内联在 RedisObject 的 value 字段，零分配 |
| **embstr** | 字符串且长度 ≤ 44 字节 | RedisObject + sds 头 + 数据 **1 次 malloc 连续分配** |
| **raw** | 字符串长度 > 44 字节 | RedisObject 和 sds **2 次 malloc 独立分配**，内存分离 |

> 注意：Redis 7.x 中 `OBJ_ENCODING_EMBSTR_SIZE_LIMIT = 44` 字节（旧版 ziplist 场景是 39）。embstr 是一种对短字符串的**紧凑单次 malloc 布局**（适合短字符串）；当对 embstr 做扩展写入（如 APPEND）时，Redis 会根据需要转换为 raw（可能 realloc）以支持变长操作。

**实测数据（场景 11，c=16, batch=100）**：

| 操作 | int | embstr | raw | 结论 |
|:--|:--:|:--:|:--:|:--|
| INCR | **532,654** | — | — | int 零分配，P99 最低 |
| SET | 415K | **448,159** | 357,566 | embstr vs raw **+25.3%** |
| GET | 429K | **457,400** | 400,466 | embstr vs raw **+14.2%** |
| SET 1KB | — | — | 218,997 | 比 200B **-38.8%**（memcpy 线性增长）|
| **APPEND** | — | 409K（max **287ms**）| **410K**（max **39ms**）| **raw 预分配摊销 O(1)**，max 延迟差 7.3 倍 |

**生产选型（按 value 用途）**：

| 场景 | 推荐编码 | 原因 |
|------|:--:|------|
| 计数器/自增/频控 | **int** | 零 malloc，532K ops/s 最快 |
| 短文本/短状态/Token | **embstr** | 单次分配 cache 友好，SET/GET 明显优于 raw |
| JSON/长文本/日志 | **raw** | 唯一选择，注意 memcpy 与 slowlog |
| **增量拼接构建大字符串** | **raw 预分配** | 先 `SET` 初始 raw 预分配，再反复 `APPEND` → 摊销 O(1) |
| 对短值反复 APPEND | ❌ 避开 | int/embstr 每次重分配，max 延迟 287ms |

### 6.1 Hash 底层编码：listpack VS hashtable（Redis 7.0+）

Redis Hash 存在两种底层实现：**listpack（紧凑编码）、hashtable（dict 哈希表）**。

配置阈值（示例值）：

```
hash-max-listpack-entries 128
hash-max-listpack-value 64
```

规则：字段数量 ≤ 阈值 **并且** 所有 field、value 长度 ≤ 阈值字节时使用 listpack；任一条件不满足自动转为 hashtable；转换**单向不可逆**。

> ⚠️ **默认阈值依 Redis 版本/发行包而异**。生产环境请用以下命令以**当前实例实际配置为准**：
> ```
> CONFIG GET hash-max-listpack-entries
> CONFIG GET hash-max-listpack-value
> ```
> 典型 Redis 7+ 默认较小（如 128，非 512），务必以 `CONFIG GET` 验证并监控编码转换频率。若生产想多用 listpack 可上调，但上限不宜超 2000（listpack 是 O(N) memmove）。

#### 6.1.1 listpack

连续内存紧凑存储，交替存放 field-value，无指针元数据，内存占用低。

**读写特性**：查找、更新、删除需要线性遍历 O(N)；修改可能触发内存拷贝。

- ✅ 适用：字段少、值短小、大量小 Hash、内存敏感、经常一次性读取全部字段；
- ❌ 不适用：字段增多后单点读写性能持续下降。

#### 6.1.2 hashtable（dict）

标准哈希表，平均读写 O(1)；需要存储哈希桶、指针，内存开销更高。

✅ **hashtable 性能优于 listpack 典型场景**：

1. Hash 内部 field 数量较多（数百个）；
2. 业务以随机单点读写为主（HGET/HSET/HINCRBY），很少读取全部字段；
3. field/value 存在较长字符串，超出阈值；
4. 高并发 QPS，需要稳定可控的延迟；
5. 频繁零散更新、删除字段。

**实测验证（场景 11，c=16, batch=50）**：

- 小 Hash（50 field）listpack 碾压 hashtable，HSET 快 20+ 倍；
- 但**临界反超**：公平对比（127f listpack vs 128f hashtable）下 hashtable 反超 **+21%**（HGET）、+22%（末尾 field）——listpack 是 O(N) 顺序扫描，N 接近阈值时扫描成本反超 dict 的 O(1)；
- 因此"刻意限容保 listpack"不是银弹：field 逼近 128 时 hashtable 反而更稳。

> 🔴 **hashtable 的红线：严禁对大 Hash 做 HGETALL**。500 field 的 hashtable 做 HGETALL 是 O(N) 全量读取，实测阻塞主线程 4.4 秒、16 条 slowlog（场景 5）。同理，Set 的 hashtable 做 SMEMBERS 慢 30 倍+（14K vs 476K ops/s，场景 11）。**大 hashtable 必须用 HGET/HMGET 按需取，或 HSCAN 分页。**

**HSCAN 使用注意**：`count` 只是 hint（不保证精确返回条数），返回可能重复且**不保证顺序**——需做去重 + 游标结束检测；**避免在 Lua 脚本中做长循环 HSCAN**（阻塞主线程）。若需全量消费，建议后台异步分批导出，或从业务层分页读取并降峰。

#### 6.1.3 生产规范

1. 小规模短字段对象优先利用 listpack 节省内存；
2. 多字段、高并发随机读写对象，hashtable 性能更稳定；
3. 避免 Hash 长期处于阈值附近频繁触发编码转换；
4. 升级为 hashtable 后，即便字段减少，也不会退回 listpack。

### 6.2 四类型内部编码选型实测结论（场景 11，c=16）

> 所有内部编码均**由数据自动决定，无法显式指定**；只能靠 value/member 的类型、数量、长度"凑"出目标编码，且转换**单向不可逆**。

| 类型 | 编码 | 实测关键结论 | 选型建议 |
|:--:|:--|:--|:--|
| **String** | int / embstr / raw | INCR(int) 532K 最快；embstr 比 raw SET +25%、GET +14%；raw 的 `sdsMakeRoomFor` 预分配使 APPEND 摊销 O(1)（max 39ms vs embstr 287ms）| 计数→int；短文本→embstr；长文/可变拼接→raw |
| **Hash** | listpack / hashtable | 小 Hash listpack 碾压；128f 临界 hashtable 反超 +21%；**严禁大 hashtable HGETALL**（500f 阻塞 4.4s）| field≤64 用 listpack；≥128 直接用 hashtable（O(1)），禁 HGETALL |
| **ZSet** | listpack / skiplist | **ZADD 上 skiplist 反而 +11%**（排序定位优势）；ZSCORE/ZRANGE 接近；128 member 未触发 skiplist（需 >128）| 大排行榜/高并发 ZADD 用 skiplist；小排行榜 listpack |
| **Set** | intset / listpack / hashtable | **intset 全面碾压**（512 整数 +22% vs hashtable 513 整数）；**SMEMBERS hashtable 慢 30 倍+**（14K vs 476K）| 纯整数用 intset（别存成字符串）；禁大 hashtable 全量读（SMEMBERS/SRANDMEMBER）|

**四类型通用红线**：

1. **编码不可强求**：小数据自动落入紧凑编码，无需干预；
2. **真正要防的是大结构的"全量读取"**（HGETALL/SMEMBERS/SRANDMEMBER/大 value 拼接）——数量级差距，改用 HSCAN/SSCAN 分页或分片；
3. 紧凑编码（listpack/intset/embstr）在临界点会因 O(N) 扫描反噬，但大 hashtable/skiplist 的 O(1)/O(logN) 更稳——"刻意保小"不是银弹；
4. **128/512 阈值边界不可靠**：实测 128 短 member 未触发转换（需 129+），生产建议留足余量（≤127 保 listpack、≤511 保 intset）。

### 6.3 ZSet 底层编码：listpack VS skiplist

Redis ZSet 存在两种底层实现，由 **member 数量** 和 **member 字符串大小** 决定：

| 编码 | 触发条件 | 内存布局 |
|:--:|------|------|
| **listpack** | member 数 ≤ `zset-max-listpack-entries`（默认 128）**且** 每 member ≤ `zset-max-listpack-value`（默认 64B）| 所有 member+score 存于一个**连续内存块** |
| **skiplist** | member 数 > 128 或 member > 64B | 跳表 + dict，每 member 独立节点，O(logN) |

> 与 Hash 不同，ZSet 的**插入/查找天然需要排序定位**，这是 skiplist 的优势所在。

**实测数据（场景 11，c=16, batch=50）**：

| 操作 | listpack (50m) | skiplist (500m长名) | 结论 |
|:--|:--:|:--:|:--|
| ZADD | 126,073 | **139,528** | **skiplist +11%** |
| ZSCORE | 300,565 | 299,066 | 接近 |
| ZRANGE | 188,806 | 188,115 | 接近 |
| ZINCRBY | 112,057 | **116,243** | skiplist +4% |

**为什么 ZADD 上 skiplist 反而更快（+11%）**：ZADD 需按 score 定位插入点，skiplist 的 O(log 500)≈9 次跳表跳跃 < listpack 的 O(50) 线性扫描。**ZADD 的排序定位是 skiplist 的强项**。

**生产选型**：

| 场景 | 推荐编码 | 原因 |
|------|:--:|------|
| 小排行榜（member ≤64）| listpack | 紧凑 cache 友好 |
| **大排行榜（member ≥128）** | **skiplist** | O(logN) 稳定，ZADD 更快 +11% |
| 高并发 ZADD 写入 | **skiplist** | 排序定位优势 |
| 全服大排行榜（百万用户）| skiplist | 唯一选择 |

> ⚠️ 128 短 member 实测未触发 skiplist（仍是 listpack），转换需 >128。生产建议 member 数明显 <128 才可靠保持 listpack；接近 128 时 ZADD 反而 skiplist 更快，不要刻意限容。

> 💡 `zset-max-listpack-entries` 与 `zset-max-listpack-value` 可在配置中调整；生产应监控成员数分布，**接近阈值时考虑预先分片或迁移**以避免频繁编码转换。

### 6.4 Set 底层编码：intset / listpack / hashtable

Redis Set 存在三种底层实现，由 **member 类型**、**数量** 和 **字符串大小** 决定：

| 编码 | 触发条件 | 内存布局 |
|:--:|------|------|
| **intset** | 全整数 且 数量 ≤ `set-max-intset-entries`（默认 512）| 有序紧凑整数数组，无 dictEntry，内存最省 |
| **listpack** | 字符串 且 ≤ `set-max-listpack-entries`（默认 128）且 ≤ `set-max-listpack-value`（默认 64B）| 连续内存块 |
| **hashtable** | 整数 > 512，或字符串 > 128 / >64B | dict 哈希表 |

**转换链（均单向不可逆）**：`intset →(加字符串)→ listpack →(>128/>64B)→ hashtable`；`intset →(整数>512)→ hashtable`。

**实测数据（场景 11，c=16, batch=50）**：

| 操作 | intset (50整数) | listpack (50字符串) | hashtable (500长) | int vs ht |
|:--|:--:|:--:|:--:|:--:|
| SADD | 364,821 | 346,772 | 311,825 | +17% |
| SISMEMBER | 379,007 | 345,711 | 329,709 | +15% |
| SREM | 448,859 | 449,792 | 307,076 | +46% |
| SRANDMEMBER | 522,726 | 530,864 | 258,756 | +102% |
| **SMEMBERS** | 476,344 | 481,726 | **14,719** | **+3136%** |

**公平对比**：intset 512整数 **+22.1%** 碾压 hashtable 513整数（395,056 vs 323,631）。

**生产选型**：

| 场景 | 推荐编码 | 原因 |
|------|:--:|------|
| 纯整数集合（≤512）| **intset** | 二分查找，全面碾压 |
| 短字符串集合（≤127）| listpack | 紧凑高效，接近 intset |
| 大集合 / 长字符串 | hashtable | 唯一选择 |
| **高频 SMEMBERS 全量读** | **避开 hashtable** | 慢 30 倍+（14K vs 476K）|
| 高频 SRANDMEMBER | intset/listpack | 比 hashtable 快 1 倍 |

> 🔴 **核心红线**：**整数别存成字符串**（否则失去 intset 优势）；**避免大 hashtable 全量读取**（SMEMBERS/SRANDMEMBER）——用 SSCAN 分页或分片。整数集合 513 个即触发 hashtable（跨越 512 阈值），整数保留 ≤512 才可靠保 intset。

> 💡 用 `OBJECT ENCODING <key>` 检查真实编码；`OBJECT FREQ` / `MEMORY USAGE` 可用于容量与 hotspot 诊断。

---

## 7 热点 Key & 大 Key 治理

### 7.1 热点 Key

**问题**：大量请求集中访问同一个 Key，分片 CPU 打满、流量倾斜。

**解决方案**：

1. 本地缓存多级拦截；
2. Key 分片（hotkey_01、hotkey_02……）；
3. 读写分离，从库分担读流量；
4. 请求入口限流。

> ⚠️ **从库读一致性**：从库读能扩展读吞吐，但存在**复制延迟风险**。对强一致性场景不宜依赖从库读；若必须从从库读，应有延迟容忍策略，或采用 **read-after-write 路由**（写后一段时间内读写都路由到主库，直到确认写生效）。另外推荐 **request coalescing（singleflight）**和本地缓存作为优先手段。

**实测量化（场景 8-1，16 并发 × 100 batch 全部 INCR 同一 key）**：

- **热点 Key 是 Cluster 单点瓶颈**：`slot = CRC16(key) % 16384` 固定映射，高频 INCR 全部路由到同一 master，水平扩展不分散同 key 负载；
- **单点写入放大复制压力**：replication backlog 被覆盖 → psync 失效 → 触发全量同步 → BGSAVE/COW 爆发；
- 五种模式实测：SINGLE_KEY 直连 ~307K cmd/s；**LOCAL_BUFFERED 本地 Atomic 自增 + 定期 INCRBY 刷写达 ~35,000,000/s**；COMPUTED 本地计算 + 批量写回 ~95,000,000/s（Redis 每秒仅 1 次写）；
- **结论**：需要强一致/实时去重 → 用 Lua/WAIT/单点原子（牺牲吞吐）；否则优先"计算在应用层，汇总写回 Redis"（LOCAL_BUFFERED/COMPUTED），本地吞吐千万级/s，且不受网络 RTT 影响（RTT>2ms 应切本地方案）。

### 7.2 大 Key

**危害**：集群迁移卡顿、网络带宽打满、DEL 阻塞主线程、RDB/AOF 文件膨胀。

**治理方案**：

1. 拆分大型集合；
2. 采用渐进式分批删除，禁止直接 DEL 超大集合；
3. 控制单个容器内元素数量；
4. 定时巡检扫描识别大 Key。

---

## 8 Redis 性能优化方向

### 8.1 Pipeline

批量打包多条命令，减少网络往返次数。
**限制**：单次不要组装上万条命令，防止缓冲区溢出；**不具备事务保障**。

### 8.2 事务与 Lua 脚本

- **MULTI/EXEC**：将命令排队并在 EXEC 时批量执行，**不支持回滚**；但它**不提供 MVCC 或可重复读语义**，在并发写场景下需用 **WATCH** 做乐观锁；
- **WATCH + MULTI/EXEC**：这才是**乐观锁**。WATCH 监控 key，事务执行前校验 key 是否被改动，若被改则 EXEC 返回 nil 需重试；
- **Lua 脚本**：服务端原子执行（天然串行，无锁冲突），减少网络交互；脚本应**短小**避免长时间阻塞主线程；Cluster 模式要求脚本内所有 Key 落在同一个槽（否则 CROSSSLOT）；
- 实测（场景 8.21）：能用原生命令绝不用 EVAL；需要原子性时用"小 EVAL + batch≤100"的 NESTED 模式；**严禁单个大 EVAL 内 for 大循环**（MERGED 模式 P99 随 batch 超线性恶化 13→28→55ms，造成全局阻塞）。

### 8.3 连接池优化

合理配置连接池最大、最小连接数；避免频繁创建销毁连接，监控连接泄漏。

**连接池大小估算**：

- 单实例并发上限 ≈ `maxclients - 保留连接数（OS/监控/运维）`；
- 每个微服务实例连接池上限 ≈ `floor((maxclients - reserved) / instance_count / safetyFactor)`（safetyFactor 建议 1.5~2）；
- **maxclients 调优**：给 OS 与监控工具保留连接（如 reserved=50~100），避免业务池占满 maxclients 后监控/运维连不上。

**策略**：优先 **DIRECT + Semaphore**（裸直连吞吐 + 连接数硬上限，见场景 6/11.5）；监控 `connected_clients`、`rejected_connections`（delta）与连接池 `poolActive/poolIdle/poolWaiters`。

---

## 9 缓存设计通用规范

1. 统一 Key 命名规范，增加业务前缀隔离；
2. 所有缓存设置 TTL，杜绝永不过期 Key 持续占用内存；
3. 禁止缓存手机号、身份证等敏感明文；
4. 缓存与 DB 更新标准策略：先更新数据库，再删除缓存；
5. 高并发更新场景，使用版本号、分布式锁保障数据一致性；
6. 配置降级、熔断策略，Redis 故障时保护下游数据库。

### 9.1 缓存一致性完整方案（"先更新 DB 再删缓存"的落地与兜底）

**核心策略：Cache-Aside 旁路缓存**——读：先查缓存，未命中回源 DB 并写回缓存；写：先更新 DB，再删除缓存（延迟到下次读时重建）。删除缓存而非更新缓存，可避免并发写覆盖导致的数据错乱。

**为什么"先更新 DB 再删缓存"仍会脏读**：

- 线程 A 更新 DB → 线程 B 读旧缓存 → 线程 A 删缓存 → 期间 B 读到旧数据；
- 或线程 A 更新 DB，线程 C 在 A 删缓存前回源写回旧值 → 缓存永久脏。

**兜底强化方案（按可靠性递进）**：

1. **延迟双删**：更新 DB 后删除缓存，**间隔几百 ms 再删一次**（抵消"回源写回旧值"的竞态窗口）。删除失败需重试。
2. **删除失败重试机制**：删除缓存失败不能吞掉，投递到 MQ/本地重试表异步补偿重删。
3. **binlog 增量订阅（生产强推）**：应用旁挂 Canal/Debezium 订阅 DB binlog，捕获更新事件后**由订阅方删除/重建缓存**，业务代码零侵入、与业务事务解耦、天然补偿"先更新 DB 后删缓存"的失败窗口。
4. **TTL 兜底**：所有缓存设置 TTL，即使缓存写脏，最长 TTL 后也会过期自愈，是最后防线。
5. **版本号/时间戳校验**：缓存值携带版本，读时校验版本落后则视为 miss 回源，避免旧值覆盖新值。

> 读多写少场景优先 Cache-Aside；写频繁场景考虑"只更新 DB + binlog 订阅刷新缓存"，或降低缓存力度。高一致业务宁可多打 DB 也不容忍脏读。

**最佳实践依场景而定**：强一致场景建议**写入 DB 并同步发布事件**（在同一事务/事务日志后），由订阅方异步更新/删除缓存（binlog + Canal/Debezium）。无论哪种方案，**业务需设计幂等回源与重试补偿**。

### 9.2 安全与合规

**不要缓存明文敏感数据**：

- **禁止缓存**身份证号、银行卡号、密码、Token 明文、手机号等 PII（个人敏感信息）明文；
- 若必须缓存，需**加密或脱敏**（如哈希/掩码），并严格 TTL 与访问控制；
- 对敏感 Key 设置**更严格的访问权限与审计**，避免被误读/泄露。

---

## 10 线上运维调优要点

### 10.1 Transparent Huge Page（THP 透明大页）优化

**1. 概念说明**

Linux 内核机制，自动将 4KB 内存小页合并为 2MB 大页，优化顺序访问程序；对 Redis 存在严重负面影响。

**2. 对 Redis 的致命影响**

Redis `fork()` 创建子进程执行 RDB、AOF 重写依赖写时复制（COW）。开启 THP 后，只要修改大页内任意一小块数据，内核需要复制整个 2MB 页面；引发内存暴涨、主线程延迟毛刺、主从同步超时、持久化缓慢。

**3. 三种模式**

- `always`：永久启用；
- `madvise`：程序主动申请才启用；
- `never`：完全关闭（生产标准）。

**4. 运维操作**

```bash
# 查看状态
cat /sys/kernel/mm/transparent_hugepage/enabled
# 临时关闭（立即生效，重启失效）
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag
```

**永久关闭（持久化）**——否则重启后失效：

- **方式 A：systemd service**（推荐）：创建 `/etc/systemd/system/disable-thp.service`，在 `ExecStart` 中执行 `echo never > .../enabled && echo never > .../defrag`，`WantedBy=multi-user.target`。
- **方式 B：GRUB**：在 `/etc/default/grub` 的 `GRUB_CMDLINE_LINUX` 加 `transparent_hugepage=never`，执行 `update-grub` 并重启。

> ⚠️ 规范：生产 Redis 服务器**永久关闭 THP**，并把该设置**固化进运维/初始化脚本与配置管理**（Ansible/Puppet/基础设施模板），防止新节点遗漏；修改完成后重启 Redis 进程。

**5. 核心结论**

Redis 官方强制规范：**所有生产实例必须禁用透明大页 THP**。

### 10.2 通用运维调优清单

1. 合理配置 tcp-backlog、client timeout；
2. 从节点可**选择**关闭持久化以降低主机 IO 压力，但**仅在有独立备份策略、至少一台副本保留持久化或能接受副本丢失的前提下**才允许；推荐**副本错峰执行 BGSAVE/AOF rewrite**（不同时间窗口）；
3. 合理调整 hz 参数，控制过期任务执行频率；
4. 核心监控指标：内存、CPU、网络、缓存命中率、慢查询、P99 延迟；
5. 持续采集慢查询日志，定位阻塞主线程的命令；
6. 线上禁止使用 KEYS 命令，遍历集合使用 SCAN。

### 10.3 缓存层监控与告警指标

**Cache 层核心指标**：

| 指标 | 说明 | 告警建议 |
|:--|:--|:--|
| hit ratio（命中率）| 近 1min/5min/1h | 低于阈值（如 80%）→ 告警 |
| qps / miss qps | 缓存请求/未命中速率 | miss 突增 → 排查穿透 |
| avg latency | 平均延迟 | 超 SLO → 告警 |
| 空值命中率 | 空值缓存占比 | 过高 → 空值污染 |
| **布隆误判率** | 布隆命中后 DB 未命中次数估算 | 超阈值 → 检查容量/重建 |
| **布隆填充率** | bit 填充程度 | > 70% → 告警扩容/重建 |
| HLL key 数量 | HLL 实例数 | Key 爆炸预警 |
| ZSet 长度 / processing 超时率 | 延迟任务队列健康度 | processing 回迁率 > X → 告警 |
| AOF rewrite 频率 / RDB bgsave 时间 | 持久化健康 | bgsave 慢 / COW 大 → 告警 |

> 告警示例：hit ratio 低于阈值、processing 回迁率 > X、布隆填充率 > 70%、COW 大小异常增长。

**INFO 关键指标（可直接采集）**：

- 内存：`used_memory`、`used_memory_rss`、`mem_fragmentation_ratio`；
- 网络：`instantaneous_input_kbps`、`instantaneous_output_kbps`；
- 吞吐：`instantaneous_ops_per_sec`、`keyspace_hits` / `keyspace_misses`；
- 复制：`sync_full`、`sync_partial_ok`、`master_repl_offset`、`slave_repl_offset`、`repl_backlog_active`、`repl_backlog_first_byte_offset`；
- 持久化：`aof_rewrite_in_progress`、`rdb_bgsave_in_progress`、`last_fork_usec`；
- 阻塞：`blocked_clients`、`blocked_time`；
- 慢查询：`slowlog` 长度与条目。

**验证/核验命令**：

```
# 查看 listpack/hash 配置
CONFIG GET hash-max-listpack-entries
CONFIG GET hash-max-listpack-value
# 检查 key 编码
OBJECT ENCODING <key>
OBJECT IDLETIME <key>
# 检查复制/延迟
INFO replication
redis-cli --stat
# 检查 Cluster 状态
CLUSTER INFO
CLUSTER NODES
```

---

## 11 全场景压测实证结论集锦（场景 1-10 核心结论汇总）

> 本章汇总 `docs/` 下场景 1-10 各压测文档的核心实证结论，与前面各章的架构理论相互印证。
>
> ⚠️ **环境依赖声明**：所有压测数据基于特定硬件/网络/配置及客户端实现（本环境 Redis 7 单体/集群、Lettuce 等），**实际生产需按本环境复现并回归测试**。**关键指标应以相对差异（倍数/百分比）而非绝对值为参考**——绝对吞吐/延迟受硬件、网络、客户端库版本、Redis 配置强烈影响，具体数值仅供参考。

### 11.1 Pipeline 压测优化（场景 1）

**核心问题**：多 Worker Pipeline 持续写入，逐步排查并修复 7 个问题。

**实测数据（SETEX 基线，batch=500, c=8, keySpace=5000 万）**：

| 指标 | 修复前 | 修复后 |
|:--|:--:|:--:|
| 吞吐 | 435k（假数据/覆盖写）| **~287k**（真实新写入成本）|
| P99 | 20ms | 5ms |

**7 大问题与结论**：

1. **Redis CPU 100%**：硬件天花板，单机新 key 写入吞吐约 200-300k cmd/s，要突破需水平扩展。
2. **Pipeline 并发过载反噬**：conc 8→16 吞吐反降 **-37%**（746k→468k）、延迟 5→20.7ms。修复：`Semaphore` 反压限制同时 flush 数，**务必 try/finally 防令牌泄漏**。修复后吞吐 468k→375k、延迟 20.7→11ms（牺牲吞吐换稳定）。
3. **SecureRandom 并发争用**：高并发下成为瓶颈，改 `ThreadLocalRandom`（注意：加密场景仍需 SecureRandom）。
4. **key 空间太小退化为覆盖写**：keySpace 不足 → 覆盖写掩盖真实写入成本，需足够大 key 空间。
5. **AOF 刷盘影响**：仅开 AOF 时触发。
6. **Pipeline 超时配置**：配 1s 超时需真正生效。
7. **优雅关闭**：两阶段关闭（优雅等待 + 超时强断），防令牌泄露。

**混合命令退化（c=8）**：SET 基线 332,825 → 2 种（SETEX+HSET）257,856（-22.5%）→ 3 种（+ZADD）240,706（-27.7%）。**退化由"昂贵命令占比"（ZADD 跳表 O(logN)）决定，非种类数**。

**ZSet 分片专项（c=64）**：Raw ZADD 单 key 168,851 vs 16 分片 177,727（仅 +8.3%）。**结论：ZSet 分片在高吞吐写入下无效**（0.16s 突破 listpack 阈值转 skiplist，路由开销成纯负担）。

**生产最优**：Raw ZADD 实时写 + 异步定时 ZREMRANGEBYRANK 裁剪。

### 11.2 短期 TTL 海量过期 key（场景 3）

**核心问题**：大量秒级 TTL key 同时到期，active-expire 采样式扫描抢占主线程，P99 抖动。

**三种武器实测对比**：

| 指标 | ① 基线（固定TTL）| ② Redis 层（hz=50,effort=2,lazyfree）| ③ 应用层（TTL+rand）|
|:--|:--|:--|:--|
| **P99** | **51~159ms** | **5.6~17ms** | 6.7~34ms |
| **max** | **82~262ms** | **8~41ms** | 12~123ms |
| 吞吐 | 37k~236k/s | **384k~471k/s** | 187k~432k/s |

**核心结论**：

1. **P99 尖刺根因**：active-expire 在主线程采样式扫描 + 同步释放内存。
2. `hz` 控制扫描频率（密度），`effort` 控制单次扫描深度（阻塞），`lazyfree-lazy-expire` 只异步化内存回收，**不消除扫描开销**。
3. **决策树**：能改配置 → 开 lazyfree + 短 TTL 用 hz=50/effort=2；能改应用 → TTL 随机打散（P99: 159→34ms）；都不能 → 横向拆分或接受 P99>50ms。
4. **实测最优组合**：`hz=50 + effort=2 + lazyfree=yes`，P99 从 159ms→16.96ms（↓9.3 倍），吞吐翻倍。
5. 长 TTL 周期批量写入 → 质数/互质 TTL 打破 LCM 汇集。

### 11.3 分布式信号量（场景 4）

**核心问题**：单服务最优 8 pipeline 槽位，但 12 个微服务同时写 = 96 个管道，超出 Redis 承受能力。用 Redis 全局信号量让 Writer 竞争有限槽位。

**实测结论**：

1. **8 个全局槽位是性价比最优值**（cmd/s≈320k, P99≈4.6ms, retries=0）。
2. **实现要点**：① 只在成功占有槽位时释放（不能无条件 DECR）；② 用 token+TTL 防泄漏；③ 用 Lua 保证原子性；④ 避免 tight spin-wait，用随机退避。
3. **retries=0 不等于无竞争**，吞吐才是真正的健康指标。
4. 风险评估三轴：吞吐（cmd/s）/ 延迟（P99）/ 公平性（retries/starvation）。

### 11.4 HGETALL vs HSCAN（场景 5）

**核心问题**：大 Hash（数千 field）HGETALL 是 O(N) 阻塞操作，写请求 P99 恶化。

**实测结论**：

1. **HGETALL 在大 Hash 上 O(N) 阻塞主线程**，写请求排队、P99 从 <1ms 恶化到 >50ms。
2. **HSCAN 增量不阻塞**，但返回不保证快照一致，`count` 只是 hint。
3. **三级阈值策略（RedisHashUtil）**：`HLEN < 500` 直接 HGETALL；`500~5000` warn 走 HSCAN；`≥5000` 拒绝。**HSCAN count=200 为经验最优值**。
4. **数据建模优先**：单 key field 数 ≤ 1000（保守 500），从源头避免大 Hash。
5. **禁止在 Lua 中做大循环或 HGETALL/SMEMBERS 大 key 操作**；Redis 7+ 可用 EVAL_RO 发只读副本。

### 11.5 maxclients 边界探测（场景 6）

**核心问题**：多微服务多线程 Pipeline，连接数线性增长撞向 `maxclients`，新连接被拒。

**实测数据（SETEX Pipeline, batch=20）**：

| 模式 | 吞吐 | 连接数 | 吞吐/连接 | Redis 压力 |
|:--|:--|:--|:--|:--|
| DIRECT 裸 | ~140k | =并发数（无上限）| 388 | ❌ 高 |
| POOL | ~58k | ≤poolMax | 4,143 | ✅ 低 |
| **DIRECT+Semaphore** | **~155k** | ≤semaphoreMax（实测 5-10）| **15,500** | ✅ 极低 |

**核心结论**：

1. **DIRECT 模式 = 连接无上限**，高并发必撞 maxclients，连接被拒无优雅降级。
2. **POOL = 连接池硬上限**，天然熔断器，但吞吐被池机制限制。
3. **DIRECT+Semaphore = 裸 DIRECT 吞吐 + POOL 安全性**，只用 10 条连接打满 ~155k cmd/s，最优方案。
4. poolMaxTotal 配置公式：`floor((maxclients - reserved) / instance_count / safetyFactor)`。
5. 监控 `rejected_connections` 的 δ（增长速率），delta>0 说明正在拒绝 → 紧急告警。

### 11.6 Pipeline RTT 敏感度与 WAIT（场景 8.15-8.21）

**核心问题**：网络延迟对五种写入模式的冲击量化，以及同步/异步 WAIT 对吞吐的影响。

**RTT 敏感度结论（场景 8.15）**：

1. `SINGLE_KEY` / `DISTRIBUTED` / `SHARDED` 吞吐 ∝ 1/RTT（RTT 从 1ms→10ms 吞吐掉 ~90%）。
2. `LOCAL_BUFFERED` / `COMPUTED` 本地主循环，吞吐几乎与 RTT 无关。
3. **量化判据**：RTT ≥ 5ms 且写压占主路径 → 优先本地缓冲/COMPUTED；RTT < 1ms 且需强一致 → 同步 WAIT 可接受。

**同步 vs 异步 WAIT（场景 8.17/8.18）**：

- **同步 WAIT**：每批 flush 后等待副本确认，吞吐大幅退化（batch=100 时 -47%）。
- **异步 WAIT（Fire-and-Forget）**：只登记 pendingWaits，周期 drain，**根除 WAIT 吞吐退化**。
- 核心公式：`throughput ≈ (concurrency × batchSize) / (batchLatency + WAIT_RTT/N)`，N→∞ → 无 WAIT 基线。
- 监控：WAIT 成功率 < 99% 告警，持续 < 95% 自动降级。

**EVAL 嵌套执行（场景 8.21，3×3 实测）**：

| batchSize | 模式 | ops/s | sideP99 |
|:--:|:--|:--:|:--:|
| 100 | NESTED | 191,044 | 18.99ms |
| 100 | **MERGED** | 4,660 | 12.75ms |
| 100 | RAW | 455,483 | 14.95ms |
| 1000 | MERGED | 600 | **55.37ms** ※ |

※ 单个大 EVAL 独占主线程 1000 次循环的伤害 > 1000 个小 EVAL 排队。

**结论**：RAW 原生命令最优（吞吐 455-588K）；NESTED EVAL 可行（batch≤100 最稳，~200K）；**MERGED EVAL 危险**（P99 随 batch 超线性增长，batch>100 反超 NESTED）。生产优先级：能不用 EVAL 就不用 → 原子性用 NESTED 且 batch≤100 → 避免 MERGED 大循环。

### 11.7 集合运算高并发（场景 9）

**核心问题**：Set 并/交/差是 CPU 密集型，大集合高并发下阻塞主线程。

**实测数据（环境相关）**：

| 集合大小 N | 吞吐 | 说明 |
|:--:|:--:|:--|
| 500 | 30,499 | |
| **1,000** ⭐ | **38,031** | 甜蜜点 |
| 5,000 | 18,050 | 拐点（-52%）|
| 10,000 | 5,184 | 主线程 CPU 饱和 |

**核心结论**：

1. **SINTER 以最小输入集合为遍历基准**（O(S×(k-1))），SUNION 为 O(sum Ni)，SDIFF 遍历第一个集合并排除。
2. **参数顺序影响成本**——把小集合放前面。
3. **SMOVE 是单命令原子方案**；Pipeline 减少 RTT 但不保证多命令原子性。
4. SSCAN 遍历必须去重 + max rounds/超时；非快照隔离。
5. **手动取模跨分片 SINTER 不报错但静默错误**（缺失 key 视为空集）——必须 hashtag 或校验。
6. 单 Redis 拆小 key 不减少 SINTER 遍历总量。
7. **大集合用 STORE 变体预计算**（SINTERSTORE 写一次换多次读），仅一次性用 SINTER。

### 11.8 哨兵 vs 集群（场景 10）

**核心问题**：为写入阶段做架构选型，对比稳态吞吐与故障韧性。

**稳态写吞吐（单 worker 单 slot）**：

| 指标 | 哨兵 | 集群 |
|:--|:--:|:--:|
| cmdPerSec | ~369,300 | ~354,900 |
| p99 | 15.970ms | 18.233ms |

> 集群与哨兵吞吐几乎相同（集群略低 ~4%）。因 key 落在同一固定 slot（单主分片），本质仍是单节点在写。**单热 key/单 worker 分布写时，集群和哨兵都受单节点限制，无吞吐优势。**

**故障韧性（kill master）**：

| 阶段 | 观察窗口 | 重试 | 哨兵 failRate | 集群 failRate |
|:--|:--:|:--:|:--:|:--:|
| 修复前 | 15s | 无 | 0.105% | 0.099% |
| **修复后** | **35s** | **指数退避重试** | **0.000%** | **0.000%** |

**核心结论**：

1. **稳态写入上集群对单 worker/单热 key 无吞吐优势**（≈相同），写路径都是单主分片单线程。
2. **集群的吞吐优势只在"多 key 均匀分布到多主分片"时体现**。
3. **故障韧性**：应用层重试 + 足够观察窗口（35s），哨兵和集群 kill master 都做到 failRate=0（零丢失）。
4. **选型建议**：数据量小/简单高可用 → 哨兵；数据量大/需水平扩展/多 key 高吞吐 → 集群（前提 key 能均匀分布）。
5. **脑裂防护**：`min-replicas-to-write=1` 即可（与 replica 总数无关）。
6. **RPO 兜底**：数据库水位线对账是唯一数学上能保证最终一致性的手段。
