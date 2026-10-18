---
title: "Redis 单体工具类使用文档"
summary: "面向单节点单机 Redis（无主从、无哨兵、无集群）的全量操作封装。所有方法统一返回 RedisResult<T>，业务层通过 isSuccess() 判断结果，getData()…"
date: 2026-05-31
author: "小番茄程序猿"
tags:
  - Redis
category: deep
series: "redis"
slug: "Redis单体工具类使用文档"
reading_time: 20
featured: false
draft: false
---
# Redis 单体工具类使用文档

面向**单节点单机 Redis**（无主从、无哨兵、无集群）的全量操作封装。所有方法统一返回
`RedisResult<T>`，业务层通过 `isSuccess()` 判断结果，`getData()` 取数据。

> **当前项目架构**：工具类保持单体语义不变，但 `RedisConfig` 已扩展为 **3 分片模式**——
> 批处理消费者 (`UserSetBatchConsumer`) 通过 `redisShard0/1/2` 三个独立 `ReactiveStringRedisTemplate`
> 按 `vu % 3` 哈希路由到 3 个 Redis 实例（6379/6380/6381）。各分片仍为单节点单机，
> 本章工具类适用于任意单个分片的直接操作。

## 一、总体设计

| 层 | 说明 |
|----|------|
| `config/RedisConfig` | 单体连接配置，Lettuce 连接池由 `application.yml` 自动装配；自定义序列化 |
| `common` | `RedisResult` 统一结果 / `RedisResultCode` 结果码 / `RedisConstants` 阈值 / `RedisAssert` 参数校验 / `RedisSafetyException` 安全异常 |
| `core/AbstractRedisSupport` | 统一执行模板：耗时统计 + 异常分类拦截 + 结果封装 |
| `core/Redis*Util` | 10 大功能模块工具类 |

### 统一返回结果 `RedisResult<T>`

```java
result.isSuccess();   // 是否成功
result.getData();     // 数据
result.getCode();     // 结果码：0 成功 / A001 参数错 / B001 连接失败 / B002 超时 / B003 命令错 / C001 安全拦截
result.getMessage();  // 描述
result.getCostMillis(); // 命令耗时(ms)
```

### 异常分类

| Code | 含义 | 触发场景 |
|------|------|----------|
| `A001` | 参数校验失败 | key 为空、过期时间为负 |
| `B001` | 连接失败 | Redis 不可达、连接池耗尽 |
| `B002` | 执行超时 | 命令超过读超时 |
| `B003` | 命令执行错误 | 类型不匹配、语法错误 |
| `C001` | 安全拦截 | 分页超上限等 |

## 二、连接配置（application.yml）

```yaml
spring:
  redis:
    host: ${REDIS_HOST:localhost}
    port: ${REDIS_PORT:6379}
    password: ${REDIS_PASSWORD:}
    database: 0
    timeout: 3000ms          # 读写超时
    lettuce:
      pool:
        max-active: 8         # 已从 16 收敛至 8（Tier 2 优化）
        max-idle: 4           # 已从 8 收敛至 4
        min-idle: 2
        max-wait: 3000ms
```

## 三、模块使用示例

### 模块1：通用 Key（RedisKeyUtil）

```java
keyUtil.setEx("k", "v", 60);            // SETEX 60 秒
keyUtil.pSetEx("k", "v", 1500);         // PSETEX 1500 毫秒
keyUtil.exists("k");                     // EXISTS
keyUtil.existsBatch(Arrays.asList("a","b"));
keyUtil.unlink("k");                     // UNLINK 异步删除（推荐）
keyUtil.del("k");                        // DEL 同步删除
keyUtil.expire("k", 60);                 // EXPIRE
keyUtil.ttl("k"); keyUtil.pTtl("k");     // TTL / PTTL
keyUtil.persist("k");                    // 移除过期
keyUtil.renameNx("old", "new");          // RENAMENX
keyUtil.type("k");                        // TYPE
keyUtil.memoryUsage("k");                // 查大 key 内存（Lua 实现，见第六节第 4 点）
keyUtil.scan("user:*", 100);            // SCAN 分页遍历（替代 keys *）
```

- **生产注意**：删除大 key 优先 `unlink`；遍历只能用 `scan`。
- **禁用场景**：❌ `keys *` 全库扫描（已屏蔽，只提供 scan）。

### 模块2：String（RedisStringUtil）

```java
stringUtil.set("k", "v");
stringUtil.get("k");
stringUtil.mSet(map); stringUtil.mGet(keys);
stringUtil.append("k", "abc");
stringUtil.getRange("k", 0, 3); stringUtil.setRange("k", "x", 2);
stringUtil.strLen("k");
stringUtil.incr("k"); stringUtil.incrBy("k", 10);
stringUtil.incrByFloat("k", 1.5); stringUtil.decr("k");
stringUtil.setNxEx("lock:order", uuid, 30);  // 单体分布式锁加锁
```

- **生产注意**：`incrByFloat` 有精度问题，金额慎用；锁 value 用唯一标识，解锁走 Lua。
- **序列化一致性**：本模块走 `StringRedisTemplate`（原始字符串）。与主 `RedisTemplate`（JSON）写入的 key **不可混用**——JSON 会落库带引号的串，导致 `append`/`strLen`/`getRange` 长度/内容偏差（详见第六节第 1 点）。
- **禁用场景**：❌ 用单体锁做强一致跨服务互斥（Redis 宕机锁失效）。

### 模块3：List（RedisListUtil）

```java
listUtil.lPush("k","a"); listUtil.rPush("k","b");
listUtil.lPushBatch("k","a","b"); listUtil.rPushBatch("k","c","d");
listUtil.lPop("k"); listUtil.rPop("k");
listUtil.blPop("q", 5); listUtil.brPop("q", 5);   // 阻塞队列
listUtil.rPopLPush("src","dst");                   // 可靠队列
listUtil.lPage("k", 0, 100);                        // 分页查询（推荐）
listUtil.lIndex("k", 0); listUtil.lLen("k");
listUtil.lSet("k",0,"x"); listUtil.lRem("k",0,"a"); listUtil.lTrim("k",0,99);
```

- **生产注意**：阻塞弹出占用连接，超时别设太长；对外用 `lPage` 分页。
- **禁用场景**：❌ `lRange(key, 0, -1)` 全量拉取大列表。

### 模块4：Hash（RedisHashUtil）

```java
hashUtil.hSet("user:1","name","tom");
hashUtil.hMSet("user:1", map);
hashUtil.hGet("user:1","name");
hashUtil.hMGet("user:1", Arrays.asList("name","age"));  // 推荐
hashUtil.hGetAll("user:1");                              // 大 hash 告警
hashUtil.hKeys/hVals/hLen/hExists(...);
hashUtil.hIncrBy("user:1","age",1); hashUtil.hIncrByFloat(...);
hashUtil.hDel("user:1","name");
```

- **生产注意**：字段数超阈值 `hGetAll` 打告警，优先 `hMGet`。
- **禁用场景**：⚠️ 对超大 hash 频繁 `hGetAll`。

### 模块5：Set（RedisSetUtil）

```java
setUtil.sAdd("k","a","b"); setUtil.sRem("k","a");
setUtil.sPop("k"); setUtil.sRandMember("k", 3);
setUtil.sMembers("k");     // 大 set 告警
setUtil.sCard("k"); setUtil.sIsMember("k","a");
setUtil.sInter/sUnion/sDiff("k1","k2");
setUtil.sInterStore/sUnionStore/sDiffStore("dst","k1","k2");
```

- **生产注意**：集合运算耗时超阈值告警；大集合运算优先 Store 版本落地。
- **禁用场景**：⚠️ 超大集合直接 `sMembers` 全量返回。

### 模块6：ZSet（RedisZSetUtil）

```java
zSetUtil.zAdd("rank","tom",99); zSetUtil.zIncrBy("rank","tom",1);
zSetUtil.zRevRange("rank",0,9);                 // Top10 排行榜
zSetUtil.zRevRangeWithScores("rank",0,9);
zSetUtil.zRangeByScore("rank",60,100);
zSetUtil.zRank/zRevRank/zScore/zCard(...);
zSetUtil.zRem/zRemRangeByRank/zRemRangeByScore(...);
```

- **典型场景**：排行榜、延迟队列（score 存执行时间戳）。

### 模块7：高级结构（RedisAdvancedUtil）

```java
// Bitmap 签到
advancedUtil.setBit("sign:1001", dayOfYear, true);
advancedUtil.bitCount("sign:1001");
// HyperLogLog UV
advancedUtil.pfAdd("uv:20260718","u1","u2");
advancedUtil.pfCount("uv:20260718");
// Geo 附近的人
advancedUtil.geoAdd("pos", 116.4, 39.9, "beijing");
advancedUtil.geoRadius("pos", 116.4, 39.9, 5);   // 5km 内
// Stream 消息队列
advancedUtil.xAdd("stream", content);
advancedUtil.xReadGroup("stream","group","c1", 10);
advancedUtil.xAck("stream","group", id);
```

- **生产注意**：HLL 有 ~0.81% 误差；Geo 半径过大有性能开销。

### 模块8：事务（RedisTransactionUtil）

```java
// MULTI/EXEC
txUtil.executeInTransaction(ops -> {
    ops.opsForValue().set("a","1");
    ops.opsForValue().set("b","2");
});
// WATCH 乐观锁（CAS）
txUtil.executeWithOptimisticLock(List.of("stock"), ops ->
    ops.opsForValue().decrement("stock"));
```

- **生产注意**：单体事务无回滚，运行期错误不回滚；WATCH 被改动则 EXEC 放弃，需业务重试。

### 模块9：Lua 脚本（RedisScriptUtil）

```java
scriptUtil.eval("return 1+1", Long.class, List.of());
// 解锁（判断+删除原子）
scriptUtil.eval(RedisScriptUtil.UNLOCK_LUA, Long.class, List.of(lockKey), uuid);
String sha = scriptUtil.scriptLoad("return 1").getData();
scriptUtil.evalSha(sha, ReturnType.INTEGER, List.of(), List.of());
scriptUtil.scriptFlush();
```

- **生产注意**：脚本执行受服务端 `lua-time-limit` 约束，工具额外做耗时告警；勿写死循环/重 IO。

### 模块10：只读运维（RedisOpsUtil）

```java
opsUtil.infoMemory();        // INFO memory
opsUtil.infoPersistence();   // RDB/AOF 状态
opsUtil.slowlogGet(10); opsUtil.slowlogLen();
opsUtil.clientList();
opsUtil.memoryPurge();       // 碎片回收（仅 jemalloc）
```

- **禁用场景**：❌ 未提供 `save`/`flushall`/`flushdb`/`bgsave`/`bgrewriteaof`/`client kill` 等高危写运维入口。

## 四、生产安全规范汇总

1. **删除**：优先 `unlink` 异步删除，`del` 仅小 key。
2. **遍历**：只用 `scan`，禁用 `keys *`、`monitor`。
3. **大集合**：`hGetAll`/`smembers` 有阈值告警，优先 `hMGet` 与分页/scan。
4. **参数校验**：key 空、过期负数、分页越界自动拦截返回 `A001/C001`。
5. **异常统一**：连接失败/超时/命令错分别返回 `B001/B002/B003`，不抛异常打断业务。
6. **危险命令**：运维模块仅只读，写运维指令一律不暴露。
7. **纯单体**：不含集群槽位、哈希标签、哨兵切换逻辑。

## 五、运行与测试

```bash
# 启动单体 Redis（使用本机 redis:7 镜像）
docker compose up -d

# 运行全部单元测试（需 Redis 已启动）
mvn test

# 启动应用
mvn spring-boot:run
```

## 六、实现要点与已知坑（踩坑记录）

以下为开发/测试过程中已定位并修复的关键问题，后续改动相关代码时务必留意。

### 1. String 模块使用 `StringRedisTemplate`（原始字符串序列化）

`RedisStringUtil` 的 `set`/`get`/`append`/`strLen`/`getRange`/`setRange`/`mGet` 等全部走 **`StringRedisTemplate`**（字符串序列化），而不是主 `RedisTemplate`（JSON 序列化）。

- 若用主 `RedisTemplate`（JSON）写入 `"abc"`，实际落库为带引号的 `"abc"`（5 字节）；再用 `StringRedisUtil.append("def")` 会变成 8 字节，且 `strLen`/`getRange` 作用在 JSON 串上，结果不符合预期。
- **结论**：同一个 key 的字符串读写，要么全用 `RedisStringUtil`，要么全用 JSON 模板，**不可混用**，否则出现引号/长度偏差。

### 2. 事务读写需保持同一序列化模板

`RedisTransactionUtil` 基于主 `RedisTemplate`（JSON）执行 `ops.opsForValue().set(...)`，写入值是带类型包装的 JSON（如 `"1"`）。校验结果时也必须用同一 JSON 模板读取（`redisTemplate.opsForValue().get(k)`），不能用 `StringRedisUtil.get`（会读到原始 JSON 串，断言失败）。

### 3. 原生命令须取真实 Lettuce 连接

`spring-data-redis 2.7` 的 `RedisServerCommands` 未暴露 `memoryUsage` / `slowlogLen` / `slowlogGet` / `memoryPurge`（这些在 3.x 才加入），因此走了 Lettuce 原生命令。

- `redisTemplate.execute(...)` 回调里拿到的连接是 **Spring 事务代理对象**（如 `$Proxy119`），无法直接强转为 `LettuceConnection`。
- `LettuceConnection.getNativeConnection()` 返回的是 `RedisAsyncCommandsImpl`，需再经 `getStatefulConnection()` 才能拿到 `StatefulRedisConnection`。
- 已在 `AbstractRedisSupport` 统一封装 `nativeLettuceCommand(Function<RedisCommands<byte[],byte[]>, T>)` helper，slowlog 等原生命令统一走它，避免各模块重复踩坑。

### 4. `MEMORY USAGE` 改用 Lua 脚本绕过 Lettuce 缺陷

- Lettuce `6.1.10.RELEASE` 的 sync/async `memoryUsage(key)` 存在缺陷：对存在的 key 也返回 `null`（同为整数回复的 `exists` 却正常，确认是解码路径 bug），手动 `dispatch` + `IntegerOutput` 仍取不到值。
- 改用 Lua 脚本实现：`return redis.call('MEMORY', 'USAGE', KEYS[1])`，由 Spring 脚本执行器正确解码整数返回值。见 `RedisKeyUtil.MEMORY_USAGE_SCRIPT`。

### 5. 测试 JDK 版本一致性

- 项目用 **JDK 8** 编译运行。若 IntelliJ 用 JDK 17 作为 Project SDK 编译过测试类（class 版本 61），而运行 JVM 是 JDK 8（版本 52），Surefire 会报 `UnsupportedClassVersionError: class file version 61.0`。
- 排查命令：`mvn clean test`（clean 强制当前 JDK 全量重编，避开 IDE 残留 class）；或在 IDE 中把 Project SDK / 各测试运行配置的 JDK 统一设为 JDK 8。
- 当前测试套件：`mvn clean test` → **49 个用例全部通过**。
