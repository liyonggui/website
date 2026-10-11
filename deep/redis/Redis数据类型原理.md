---
title: "Redis 数据类型原理"
summary: "从内存模型 → 内部编码 → 压缩策略 → 选型决策，系统化分析 Redis 数据存储的全链路原理。 > > 注：本文所有字节数与阈值均为\"典型 64-bit Linux + je…"
date: 2026-06-03
author: "小番茄程序猿"
tags:
  - Redis
  - 源码
category: deep
series: "redis"
slug: "Redis数据类型原理"
reading_time: 155
featured: false
draft: false
---
# Redis 数据类型原理

> 从内存模型 → 内部编码 → 压缩策略 → 选型决策，系统化分析 Redis 数据存储的全链路原理。
>
> **注**：本文所有字节数与阈值均为"典型 64-bit Linux + jemalloc 构建"的经验值；在 32-bit、不同 allocator（tcmalloc/malloc）或特殊编译选项下会有差异。

### 精简正确版总结（先读这里）

Redis 在运行时对每条数据使用 redisObject（robj(~16B) on 64-bit glibc+jemalloc）+ 实际存储结构（SDS / listpack / intset / quicklist / skiplist+dict 等），并在性能与内存效率之间动态切换内部编码。绝大多数小对象的内存开销来自元数据（robj、dictEntry(~24B)、可变 SDS header 与分配器碎片），因此**"结构级优化"**（例如把多个属性放进一个 Hash listpack 或使用 intset）比对单条 value 进行通用压缩在众多场景下收益更高。对大文本/JSON（>1KB）或冷数据，客户端/应用层压缩（建议 zstd level=1，且在 value 前附加 magic+version+algo 元信息）是合理的折中；但不要对小值（<200B）、高频读或需要 Redis 原子操作的数据做压缩，因为会丧失原生操作能力并引入解压延迟。持久化（RDB/AOF）在 fork 时会触发 COW 导致短期内存峰值（可直接监控 `rdb_last_cow_size` / `aof_last_cow_size`），容量规划必须为此预留 headroom。编码转换（如 listpack→hashtable、intset→hashtable）会在触发点产生明显的 CPU/内存开销且**不可自动回退**（只有删除重建或导出重建才能恢复紧凑编码），生产应通过分片/冷热拆分/自动分桶与定时巡检等组合策略避免单 key 退化。

---

## 一、Redis 内存开销全景

### 1.1 一条数据存进去到底占多少

一个 SETEX 写入的完整内存结构：

```
┌──────────────────────────────────────────────────────────────────┐
│  dictEntry (~24B)       ── key 为 SDS（可变 header + 数据，非 robj）│
│  redisObject (~16B)     ── value 容器（ptr 指向 value SDS/内部结构）│
│  expires dictEntry(~24B)── 指向同一 key SDS（复用指针，不复制）    │
│  分配器对齐/碎片（~10-20%）                                       │
├──────────────────────────────────────────────────────────────────┤
│  存储 "hello" (5B value) → 实际内存 ≈ 50~90B + 碎片（元数据主导） │
│  存储 100KB 的 JSON        → 实际内存 ≈ 100KB + ~50~90B + 碎片    │
└──────────────────────────────────────────────────────────────────┘
```

> 主 dict 的 key 使用 SDS 字符串（`dictEntry->key` 指向 sds），value 以 redisObject 为容器；**key 不额外占用 robj（~16B）**。

> **expires dict 说明**：expires 字典仅存 `key → 过期时间戳（整数）`，key 指针通常复用主 dict 的 SDS，**不重复分配字符串内容**，仅增加一个 dictEntry 的结构开销。
>
> ⚠️ **caveat**：expires dict 的 key 复用主 dict 的 sds 指针（不复制字符串），但**某些旧版本或特殊操作（如 RENAME 临时复制）可能出现短暂不同指针**，应以源码/版本为准。expires dict 本身多出一个 dictEntry（约 24B）和过期时间戳（存在 `dictEntry->v` 中存储的整数），因此带来额外元数据开销。

### 1.2 三种核心内存结构

#### redisObject — 一切数据的容器

```c
typedef struct redisObject {
    unsigned type:4;      // 数据类型：string/list/hash/set/zset
    unsigned encoding:4;  // 内部编码：int/embstr/raw/ziplist/...
    unsigned lru:24;      // LRU 时钟（用于淘汰）
    int refcount;         // 引用计数（共享对象用）
    void *ptr;            // 指向实际数据的指针
} robj;
```

redisObject（robj）是 Redis 存放 value 的通用容器（含 type/encoding/refcount/ptr 等字段）。**主字典 dict 的 key 在常规场景下以 SDS（sds）字符串直接存储**（`dictEntry->key` 指向 sds），而**不是 robj**——因此计算单条记录元数据时**不应默认为 key 再加一个 robj**（不额外占用 robj 的 ~16B）。value 的内存开销受其内部编码（int/embstr/raw）强烈影响：int 编码避免 SDS 分配，embstr 将 robj 与短 SDS 合并到一次 allocator 分配以减少 alloc 次数与碎片，raw 则为两次独立分配。典型 64-bit glibc+jemalloc 构建下 robj ≈16B（32 位系统更小）。

#### SDS — 动态字符串

```c
struct sdshdr {
    int len;        // 已用长度
    int free;       // 剩余空闲空间（预分配优化）
    char buf[];     // 实际数据（柔性数组）
};
```

SDS header **不是固定的**——Redis 3.2+ 引入了多种 SDS 变体（sdshdr5/8/16/32/64），根据字符串长度自动选择最短 header。其中 **sdshdr5 把长度编码在 flags 字节中**（适用于非常短的字符串，无独立的 len/free 字段），其他 sdshdrX（8/16/32/64）才有 len/free 字段。

> 💡 sdshdr5 用 flags 的**低 5 位编码长度**，因此用于非常短的字符串（**通常可表示长度上限为 31 字节左右**）；更长字符串将使用 sdshdr8/16/32/64 以保存 len/free 字段。**SDS header 具体大小与 Redis 版本有关**——实现会根据字符串长度选择最小能表示的 header 类型。

因此不存在固定的"9B header"——这是很多文档的常见误区，实际 header 大小随字符串长度和变体而变化：短字符串用 sdshdr5（~1B），中等长度用 sdshdr8（~3B），长字符串用 sdshdr16/32/64。

#### dictEntry — 哈希表条目

```c
typedef struct dictEntry {
    void *key;               // 指向 key 的 SDS
    union { void *val; uint64_t u64; int64_t s64; } v;
    struct dictEntry *next;  // 链表解决哈希冲突
} dictEntry;
```

**典型 64-bit 构建下约 24B**（3 个指针 × 8B，`(~24B on 64-bit glibc+jemalloc)`）。32 位系统下约 12B。**具体值依赖于编译器与编译选项**（结构体对齐、是否含额外字段）；把"约"和平台依赖写清楚即可，不要当作跨平台硬性常量。

### 1.3 元数据开销占比

```
典型元数据组成（不含 key/value 实际字节）：
  + main dictEntry (~24B on 64-bit)
  + value 的 redisObject (~16B)
  + SDS headers（key 与 value 的 header，各可变；短字符串通常为 sdshdr5/8 等）
  + （若有 TTL）expires dictEntry (~24B)
  + allocator rounding/对齐与碎片（10-20%）
  ⇒ 不含正文的元数据在 64-bit 环境下通常落在 ~50–90B（无 TTL）或 ~80–120B（有 TTL）范围
```

> ⚠️ **注意**：常规场景下 key 使用 SDS 存储而非 robj，因此**不要把 key 计为额外一个 robj（~16B）**；元数据区间会随 value 是否走 embstr（单次 alloc）或 raw（多次 alloc）、allocator 的分配粒度与对齐（rounding/class）而变化。
>
> **示例**（64-bit jemalloc，key='hello' 5B，value='hello' 5B，value 走 embstr）：大致元数据（含 dictEntry、robj、SDS header）可能在 ~80–120B 间；若 value 为 raw（两次 alloc）或 allocator rounding 不同，该数字会上涨。

| value 大小 | 元数据占比 | 优化方向 |
|:--|:--|:--|
| 小（<100B） | ~50-70% | **结构优化**（合并为 Hash listpack）比压缩 value 更有效 |
| 中（100B-1KB） | ~20-50% | 结构与压缩并重 |
| 大（>1KB） | <10% | **value 压缩**收益显著，元数据优化收益递减 |

---

## 二、内部编码机制

Redis **不是"存入什么就原样存储什么"**。每种数据类型有 2-3 套内部编码，在小数据量下自动切换到更紧凑的存储方式。

### 2.1 String — 三种内部编码

| 内部编码 | 触发条件 | 内存结构 | 关键开销 |
|:--|:--|:--|:--|
| **int** | value 是 64 位有符号整数范围内的整数字符串 | 整数值编码为指针形式存放在 robj->ptr 中，**省去 SDS 堆分配** | 无 SDS，免去一次 heap alloc |
| **embstr** | value ≤ 44 字节（典型阈值） | robj + SDS 一次连续分配，cache 友好 | 单次 alloc，省去二次分配和碎片 |
| **raw** | value > 44 字节 | robj 和 SDS 两次独立分配，支持动态扩展 | 两次 alloc |

> **澄清"int 编码 8B"的常见误解**：int 编码将整数值以指针/整数形式编码到 robj->ptr，从而避免 SDS 的堆分配；但 redisObject（robj，本体 ≈16B on 64-bit glibc+jemalloc）与 dictEntry(~24B) 等元数据仍然存在，因此节省的是 SDS/alloc 开销而非整条记录只占 8B。
>
> 💡 **节省量化**：value 走 int 编码时**省去一次 SDS 堆分配**（节省一次 malloc header + sdshdr + 小对象分配对齐/rounding）。具体节省量取决于 value 原本用 embstr 还是 raw：**相较 raw 节省更大**（省去两次 alloc 中的一次及指针追踪）；**相较 embstr 节省则主要是少一次分配和一次指针**（embstr 本就是单次 alloc）。

```bash
# 验证
SET int_key 100            →  object encoding int_key   # "int"——绕过了 SDS 分配
SET short_key "hello"      →  object encoding short_key  # "embstr"——一次分配
SET long_key "xx...3KB"    →  object encoding long_key   # "raw"——两次分配
```

#### 关键机制：44 字节阈值（区分源码常量 vs allocator 决定）

```
Redis 源码层面：embstr 触发阈值为 44 字节（EMBSTR_SIZE_LIMIT = 44）。
即字符串长度 ≤ 44 时选择 embstr 编码。

经验推导（为何是 44）：
  = 64B（常见 jemalloc 小尺寸 class）
  - robj(~16B) - sdshdr(~3B header) - null(1B) = 44B

原理：embstr 能带来"单次分配"的实惠，很大程度上依赖底层分配器
（例如 jemalloc）的小尺寸 class（常见为 64B）——把 robj + SDS 打包到
一次 allocator 分配中，减少 alloc 次数与碎片。
```

> ⚠️ **两层含义要分清**：① **44 是 Redis 源码中 embstr 的固定阈值**（`EMBSTR_SIZE_LIMIT=44`），不是 Redis 根据 allocator 动态决定的；② jemalloc 的**小尺寸 class（如 64B）**决定了为何把 robj+SDS 放单次 alloc 能落在同一 size class，从而减少 alloc/碎片。**不同 allocator/版本会影响最终内存对齐情况**，但 embstr 的阈值在 Redis 源码中是固定的（可通过改源码或未来版本调整）。这是经验值，非随 allocator 变化的硬编码动态值。

### 2.2 Hash — listpack vs hashtable

Hash 内部有两套编码，按条件自动切换：

| 编码 | 存储结构 | 内存模式 | 适用条件 |
|:--|:--|:--|:--|
| **listpack**（Redis 7+）/ **ziplist**（<7） | 连续内存块，field-value 紧密排列 | 极致紧凑，无指针开销 | field 数 ≤ 512 且 每个元素 ≤ 64B |
| **hashtable** | 标准 dict（桶 + 链表） | 每个 field-value 独立分配，指针多 | 任意大小 |

**切换阈值**：

```
hash-max-listpack-entries 512    # field 数量阈值
hash-max-listpack-value   64     # 单个 field 或 value 大小阈值（字节）
```

#### 内存对比（100 field，field=8B + value=50B）

```
listpack：
  连续内存 ~100 × (8+50+少量元数据) ≈ 5.8KB
  + 外层 dictEntry ~ 70B
  → 总计 ≈ 6KB

hashtable：
  100 个 dictEntry       → 100 × 24B      = 2.4KB
  100 个 field SDS       → 100 × (~3+8)B  = 1.1KB (header 可变，小 field 用 sdshdr8)
  100 个 value SDS       → 100 × (~3+50)B = 5.3KB
  hash table 桶数组      → 128 桶 × 8B    = 1KB
  → 总计 ≈ 9.8KB

内存节省：~45%
```

#### listpack 退化为 hashtable 的触发条件

当发生下列任一操作时，listpack 自动转换为 hashtable：

- 新增 field 使总数 > 512
- 新增 field 或 value 长度 > 64 字节
- 某个 field 的 value 被更新为 > 64 字节

**转换不可自动回退**——一旦转为 hashtable，Redis 不会自动降级回 listpack。只有通过以下手段才能回到紧凑编码：① 删除 key 后重新创建；② 导出数据后重建（如 `DUMP`/`RESTORE`）；③ 使用 `DEBUG HTSTATS` 或 `OBJECT ENCODING` 检测后再手动触发重建（参见策略 F）。

> **编码升级的 CPU/内存代价**：listpack → hashtable 的转换不是零成本的——它需要**遍历所有 field-value 并逐一插入 hashtable（O(N) reinsert）**，期间可能触发多次内存分配与可能的 rehash，在大量 field 写入的瞬时可能产生**明显的 CPU 峰值与内存短时放大（peak memory）**。生产环境必须在**非峰值**测试转换代价并做监控/告警（见三-C 生产测试建议）。回退手段见**策略 F**（检测→重建，应作为必须配套而非可选兜底）。

### 2.3 ZSet — listpack vs skiplist

| 编码 | 触发条件 | 结构 | 内存 |
|:--|:--|:--|:--|
| **listpack** | member 数 ≤ 128 且 每个 member + score ≤ 64B | 连续内存，score+member 成对排列 | 紧凑 |
| **skiplist + dict** | 超出阈值 | 跳表存顺序 + dict 存 O(1) 查找 | 双份索引 × 内存 |

```
zset-max-listpack-entries 128
zset-max-listpack-value   64
```

**为什么 skiplist 需要额外 dict？**

- skiplist（跳表）：支撑 ZRANGE / ZRANK 等范围查询，O(log N) 按 member 查找
- dict：支撑 ZSCORE 单点查询，O(1) 查找
- 两张结构同时维护，内存开销 ≈ 单结构的 2 倍

### 2.4 Set — intset / listpack / hashtable

Set 是编码种类最多的类型：

| 编码 | 触发条件 | 结构 | 内存 |
|:--|:--|:--|:--|
| **intset** | 所有元素都是整数 且 元素数 ≤ 512 | 连续整数数组，按升序排列 | **极致紧凑** |
| **listpack** | 元素含字符串 且 元素数 ≤ 128 且 每个元素 ≤ 64B | 连续内存，适合小字符串集合 | 紧凑 |
| **hashtable** | 超出阈值 | 标准字典 | 一般 |

```
set-max-intset-entries       512
set-max-listpack-entries     128
set-max-listpack-value       64
```

#### intset 的巨大优势

```
存储 100 个 int64 整数：

intset：
  连续内存 8B × 100 + header(8B) = 808B
  → 约 0.8KB

hashtable：
  100 个 dictEntry + 100 个 SDS(数据) + hash table 桶
  → 约 5KB

内存节省：~84%
```

**intset → hashtable 升级触发**：一旦添加一个非整数元素，或元素数超过 512。此升级会触发内存重新分配（从连续整数数组转为 hashtable），在高并发路径上需谨慎。升级后不可自动回退（需删除 key 后重新插入纯整数方可恢复 intset）。

### 2.5 List — quicklist（linked listpack）

Redis 3.2 起 List 只有一种编码：**quicklist**。

```
quicklist = 双向链表，每个节点是一个 listpack

┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  listpack   │──▶│  listpack   │──▶│  listpack   │
│  (N个元素)   │◀──│  (N个元素)   │◀──│  (N个元素)   │
└─────────────┘   └─────────────┘   └─────────────┘
```

| 参数 | 含义 | 默认 | 调优方向 |
|:--|:--|:--|:--|
| `list-max-listpack-size` | 每个节点最多存多少个元素 / 最大字节数 | -2（8KB） | 增大 → 省内存但增加操作延迟 |
| `list-compress-depth` | 两端之外多少节点不压缩 | 0（不压缩） | 1+ → 省内存，适合队列场景 |

> 💡 **负值语义**：`list-max-listpack-size` 可以是正数（表示元素数）或负数（表示字节目标）。例如 `-2` 在默认 redis.conf 中通常对应约 **8KB**（见所使用 Redis 版本的注释）。**不同 Redis 版本对此的默认意义及数值可能不同**——请以当前 Redis 版本的 redis.conf 注释为准，并在生产前做压测验证。

**典型场景优化**：

```
消息队列（只有一个方向读，如 LPUSH + RPOP）：
  list-compress-depth 1  # 中间节点全部压缩，两端保留一个不压缩

双向队列（两端都可能操作）：
  list-compress-depth 2  # 两端各留 2 个解压节点

随机访问（LINDEX）：
  list-compress-depth 0  # 全部解压
```

### 2.6 内部编码汇总

| 数据类型 | 紧凑编码 | 标准编码 | 触发条件 | 内存节省量级 |
|:--|:--|:--|:--|:--|
| **String** | int / embstr | raw | 整数 / ≤44B（典型） | int: 免 SDS 分配, embstr: 少 1 次 alloc |
| **Hash** | listpack | hashtable | field≤512 & 每项≤64B | **40-50%** |
| **ZSet** | listpack | skiplist+dict | member≤128 & 每项≤64B | **30-40%** |
| **Set** | intset / listpack | hashtable | 纯整数≤512 / 字符串≤128 | intset: **80%+** |
| **List** | quicklist（始终） | — | 参数调优决定压缩深度 | 压缩中间节点可省 40% |

---

## 三、Hash 结构优化的深度分析

Hash 是 Redis 中**内存优化弹性最大**的类型，值得单独展开。

### 3.1 为什么要用 Hash 代替逐条 String？

```
场景：存储用户信息（10 个字段，每个 value 约 30B）

方案 A：逐条 String SET
  10 条 × (key 元数据 90B + value 元数据 30B)
  10 条 × 120B = 1.2KB（纯元数据）
  + value body 300B
  ≈ 1.5KB / 用户 × 100 万用户 = 1.5GB（仅元数据）

方案 B：Hash listpack 编码
  1 个外层 dictEntry 90B
  10 对 field-value 连续存储 ~ 10 × (30+30+少量标记) = 650B
  ≈ 0.74KB / 用户 × 100 万用户 = 0.74GB

内存节省：~50%
```

### 3.2 为什么 listpack 不能无限制容纳大 Hash？

```
设计权衡：

listpack（紧凑）
  ├── 优点：连续内存，零指针，无碎片，内存效率最高
  └── 代价：HGET 需 O(N) 线性扫描整个 listpack（无哈希索引）
            修改中间元素需整体内存搬移（O(N) memmove）
    
hashtable（灵活）
  ├── 优点：HGET O(1) 哈希查找，单 field 修改 O(1)
  └── 代价：大量独立分配，指针多，内存效率低
```

**512 这个阈值是 Redis 社区经过反复 benchmark 的 trade-off**——在这个数量级，O(N) 扫描 512 个元素仍然远快于一次网络往返，但内存节省却可达到 50%。

### 3.3 生产环境 Hash 调优策略

```bash
# 保守（省内存优先）
hash-max-listpack-entries 1024   # 提高阈值，更多 hash 走 listpack
hash-max-listpack-value   128    # 允许更大的 value 仍保持 listpack

# 性能优先（避免频繁编码转换）
hash-max-listpack-entries 256    # 降低阈值，更快退化为 hashtable
hash-max-listpack-value   48     # 更保守的 size 限制
```

**动态监控**：

```bash
# 查看当前实例有多少 hash key 是 listpack 编码，多少是 hashtable
redis-cli --bigkeys

# 查看具体 key 的编码
redis-cli OBJECT ENCODING user:12345
```

---

## 四、Hash field 数量控制：避免退化为 hashtable 的策略

> **核心目标**：让 Hash 尽可能保持在 **listpack 紧凑编码**，避免触发 `hash-max-listpack-entries` 阈值后转为 hashtable（内存膨胀 ~2×）。

### 4.1 问题量化：退化前后的差距到底多大

| 编码 | 100 field | 500 field | 1000 field |
|:--|:--|:--|:--|
| **listpack** | ~50KB | ~250KB | —（超出 512，不存在） |
| **hashtable** | — | — | ~550KB+ |
| 倍数 | — | — | listpack 省 **45-55%** |

**退化发生在第 513 个 field 写入的那一瞬间**——从那一刻起整个 Hash 变为 hashtable，且**不可自动回退**。这是单靠调大阈值无法根本解决的问题。

### 4.2 策略全景

```
                      ┌── 策略 A：调大阈值（最简单）
                      │
   控制 field 数量 ──┼── 策略 B：垂直分片——按 key 哈希分桶（最通用）
                      │
                      ├── 策略 C：时间分区（适合时序缓存）
                      │
                      ├── 策略 D：冷热拆分（适合用户画像/对象属性）
                      │
                      ├── 策略 E：应用层计数器 + 自动分桶（对业务透明）
                      │
                      └── 策略 F：主动检测 + 强制重建（兜底策略）
```

---

### 策略 A：调大 `hash-max-listpack-entries`（最简单但有限）

```conf
# redis.conf
hash-max-listpack-entries 2000   # 默认 512，温和上调
hash-max-listpack-value 128      # 默认 64B，适当放大
```

```bash
# 运行时动态调整（不重启）
CONFIG SET hash-max-listpack-entries 2000
```

| 优点 | 缺点 |
|:--|:--|
| 零代码改动 | listpack 越大，HSET/HGET 的 O(n) 遍历开销越大 |
| 即刻生效 | 上限在 **2000** 左右为经验上限；超过后 memmove 成本显著。视场景不建议任意放大，**必须压测**确认 listpack 的 O(N) 扫描是否成为瓶颈 |
| 适合 field 数在 500~2000 的温和增长 | **不解决根本问题**——持续增长迟早突破 |

---

### 策略 B：垂直分片——按 key 哈希分桶（最通用，推荐）

**思想**：把一个大 Hash 拆成 N 个物理小 Hash，用哈希路由。

```
原始：HSET user:profile field_1 v1 ... field_2000 v2000

拆分后：
  user:profile:0  ── field_1  ... field_256
  user:profile:1  ── field_257 ... field_512
  ...
  user:profile:7  ── field_1792 ... field_2000
```

```java
public class HashShardingUtils {

    private static final int DEFAULT_SHARD_COUNT = 8;
    private static final int TARGET_FIELDS_PER_SHARD = 400;  // 留 ~22% 余量

    /**
     * 计算分片数：保证每片平均 field 数不超过 TARGET_FIELDS_PER_SHARD
     */
    public static int shardCount(int totalFields) {
        return Math.max(1, (totalFields + TARGET_FIELDS_PER_SHARD - 1)
                / TARGET_FIELDS_PER_SHARD);
    }

    /**
     * 根据 field 名计算所属分片（CRC16 与 Redis Cluster hash slot 一致）
     */
    public static int shardIndex(String fieldName, int shardCount) {
        int crc = fieldName.hashCode() & 0xFFFF;
        return Math.abs(crc % shardCount);
    }

    /**
     * 生成分片 key
     */
    public static String shardKey(String baseKey, int shardIndex) {
        return baseKey + ":" + shardIndex;
    }

    // 写：单个 field
    public void hset(String baseKey, String field, String value) {
        int totalFields = getOrEstimateTotalFields(baseKey);
        int count = shardCount(totalFields);
        int idx = shardIndex(field, count);
        redis.hset(shardKey(baseKey, idx), field, value);
    }

    // 读：单个 field
    public String hget(String baseKey, String field) {
        int count = shardCount(getOrEstimateTotalFields(baseKey));
        int idx = shardIndex(field, count);
        return redis.hget(shardKey(baseKey, idx), field);
    }

    // 读：全部 field（并发聚合）
    public Map<String, String> hgetAll(String baseKey) {
        int count = shardCount(getOrEstimateTotalFields(baseKey));
        Map<String, String> result = new HashMap<>();
        for (int i = 0; i < count; i++) {
            result.putAll(redis.hgetAll(shardKey(baseKey, i)));  // 可 Pipeline 并发
        }
        return result;
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| **field 数无限扩展**，每片始终 < 400 | `HGETALL` 需要多次网络往返（可 Pipeline 并发缓解） |
| 每片在 listpack 编码内，内存最优 | 需在应用层维护路由逻辑 |
| 分片数可动态调整 | 分片数变化时需迁移数据 |

**分片数选择公式**：
```
shardCount = max(1, ⌈totalFields / 400⌉)

400 = hash-max-listpack-entries(512) × 0.78
留 ~22% 余量，避免偶发 field 尖峰触发退化
```

---

### 策略 C：时间分区（适合时序缓存）

**场景**：以时间维度写入 field，每个时间窗口一个 Hash。

```
原始：HSET stats:api:call_count 2026-07-27-14:30 1234
                               2026-07-27-14:31 2345
                               ...（每天 1440 分钟 × 30 天 = 43200 field）→ 必然退化

拆分后：
  stats:api:2026-07-27  14:30 1234    ← 一个 Hash 存一天的分钟级数据（1440 field）
  stats:api:2026-07-28  14:30 2234    ← 若 1440 仍超阈值则拆半天（720 field）
```

```java
public class TimeBasedHash {

    /**
     * 根据写入频率自动估算时间窗口大小
     */
    public static String timeShardKey(String baseKey, long timestampMs,
                                       int maxFieldsPerShard) {
        long windowSizeMs = (long) maxFieldsPerShard * 60 * 1000L;  // 假设每分钟 1 field
        long epoch = timestampMs / windowSizeMs;
        return baseKey + ":" + epoch;
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 天然适合时序数据，可按窗口 TTL 自动过期 | 仅适合时序类数据 |
| 近期数据在 listpack，老数据淘汰 | 跨窗口聚合查询需遍历多个 key |

---

### 策略 D：冷热拆分（适合用户画像、商品属性）

**思想**：高频访问的 field 放一个 Hash（保持 listpack），低频 field 放另一个（可接受 hashtable）。

```
原始（2000 field，全部在一个 Hash）→ hashtable，浪费 2× 内存

拆分：
  user:profile:hot   ← 前 50 个高频 field（昵称、头像、等级……）
                       → 保持在 listpack，极省内存 + 单次 HGETALL 快

  user:profile:cold  ← 后 1950 个低频 field（注册时间、最后登录 IP……）
                       → 可能退化 hashtable，但访问频率低，影响小
```

```java
public class ColdHotHash {
    private static final Set<String> HOT_FIELDS = Set.of(
        "nickname", "avatar", "level", "vip_expire", "balance"
    );

    public void hset(String baseKey, String field, String value) {
        String key = HOT_FIELDS.contains(field)
            ? baseKey + ":hot"
            : baseKey + ":cold";
        redis.hset(key, field, value);
    }

    // 80% 的 HGET 命中 hot key，走 listpack 快速路径
    public String hget(String baseKey, String field) {
        String key = HOT_FIELDS.contains(field)
            ? baseKey + ":hot"
            : baseKey + ":cold";
        return redis.hget(key, field);
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 热数据查询极快（listpack O(1) 指针偏移） | 需提前定义 hot field 集合 |
| 冷数据退化 hashtable 可接受，整体内存仍省很多 | 冷热比例失衡则效果打折扣 |

---

### 策略 E：应用层计数器 + 自动分桶（对业务透明）

**思想**：在应用层维护每个逻辑 key 的 field 计数，接近阈值时自动创建新分片。

```java
public class AutoShardingHash {
    private static final int MAX_FIELDS_PER_SHARD = 400;
    private static final String COUNT_KEY_PREFIX = "meta:hash:count:";
    // 本地计数器批量化，减少 Redis INCR 次数
    private static final int SYNC_THRESHOLD = 100;
    private final AtomicLong localCounter = new AtomicLong(0);

    public void hset(String baseKey, String field, String value) {
        // 1. 本地聚合计数
        long local = localCounter.incrementAndGet();
        if (local % SYNC_THRESHOLD == 0) {
            redis.incrBy(COUNT_KEY_PREFIX + baseKey, SYNC_THRESHOLD);
            redis.expire(COUNT_KEY_PREFIX + baseKey, 3600);
        }

        // 2. 计算分片
        long totalFields = getTotalFields(baseKey);
        int shardIdx = (int) ((totalFields - 1) / MAX_FIELDS_PER_SHARD);
        String shardKey = baseKey + ":s" + shardIdx;

        // 3. 写入
        redis.hset(shardKey, field, value);
    }

    public Map<String, String> hgetAll(String baseKey) {
        long total = getTotalFields(baseKey);
        int shardCount = (int) ((total + MAX_FIELDS_PER_SHARD - 1)
                / MAX_FIELDS_PER_SHARD);
        List<Map<String, String>> results = new ArrayList<>();
        for (int i = 0; i < shardCount; i++) {
            results.add(redis.hgetAll(baseKey + ":s" + i));  // 可 Pipeline 并发
        }
        return merge(results);
    }

    private long getTotalFields(String baseKey) {
        return redis.incrBy(COUNT_KEY_PREFIX + baseKey, 0);  // 读当前值
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 业务层完全透明 | `HSET` 每次多一次本地计数 + 定期 INCR |
| 自动触发分片，无需预估 | `HDEL` 需额外索引查分片位置（或接受遍历） |
| 不会意外退化 | 本地计数器在进程重启时会丢失少量计数 |

---

### 策略 F：主动检测 + 强制重建（兜底策略）

当意外退化发生时，用定时任务检测并重建。

```java
public class HashEncodingGuard {

    /**
     * 定时扫描并修复退化的 Hash（每 10 分钟）
     */
    @Scheduled(fixedDelay = 600_000)
    public void repairEnlargedHashes() {
        for (String key : monitoredHashKeys) {
            String encoding = redis.objectEncoding(key);
            if ("hashtable".equals(encoding)) {
                long fieldCount = redis.hlen(key);
                if (fieldCount <= 500) {
                    // field 数已降回阈值以下但编码仍未变 → 手动重建恢复 listpack
                    rebuildAsListpack(key);
                }
                // fieldCount > 500 则走策略 B/C/D 主动分片
            }
        }
    }

    private void rebuildAsListpack(String oldKey) {
        String tmpKey = oldKey + ":tmp:" + System.currentTimeMillis();
        String versionKey = oldKey + ":version";       // 乐观版本号

        // 为防止 HGETALL 一次性拉取超大集合阻塞主线程，
        // 生产建议使用 HSCAN 分批迭代（COUNT 500）
        long cursor = 0;
        List<Map.Entry<String, String>> allEntries = new ArrayList<>();
        do {
            ScanResult<Map.Entry<String, String>> scanResult =
                redis.hscan(oldKey, cursor, HSCAN_COUNT_500);
            allEntries.addAll(scanResult.getResult());
            cursor = scanResult.getCursor();
            if (scanResult.isFinished()) break;
        } while (cursor != 0);

        // 分批写入 tmp key（避免单次 HSET 大量 field 立即触发退化）
        for (int i = 0; i < allEntries.size(); i += 400) {
            int end = Math.min(i + 400, allEntries.size());
            Map<String, String> batch = new HashMap<>();
            for (int j = i; j < end; j++) {
                Map.Entry<String, String> e = allEntries.get(j);
                batch.put(e.getKey(), e.getValue());
            }
            redis.hmset(tmpKey, batch);  // Redis 4.0+ 建�用 HSET 多参数替代已弃用的 HMSET
        }

        // ⚠ 一致性方案（三选一）：
        // 方案 1（简单但丢写）：业务空闲/低峰窗口直接用 RENAME
        //    redis.rename(tmpKey, oldKey);
        //
        // 方案 2（CAS 乐观锁）：用版本号 + WATCH 保证不丢写
        //    见下方 Lua 脚本实现
        //
        // 方案 3（最安全）：短暂暂停写入 + RENAME + 恢复写入
        //    适合对一致性要求严格但可接受短暂停写的场景

        // CAS 实现示例（方案 2——乐观版本号）
        String luaScript =
            "local v = redis.call('GET', KEYS[1]) or '0' " +
            "if v == ARGV[1] then " +           // 版本号未变
            "  redis.call('RENAME', KEYS[2], KEYS[3]) " +
            "  redis.call('INCR', KEYS[1]) " +  // 版本号 +1
            "  return 1 " +
            "else return 0 end";                // 版本号已变，重建期间有新写入
        Long result = redis.eval(luaScript,
            Arrays.asList(versionKey, tmpKey, oldKey),
            String.valueOf(currentVersion));
        if (result == 0) {
            // 重建期间有新写入 → 放弃本次重建，下次巡检再试
            redis.del(tmpKey);
        }
    }
}
```

> **一致性说明**：RENAME 本身是原子的，但 `HSCAN → HMSET` 之间存在**写入丢失窗口**——如果在 HSCAN 遍历结束后、RENAME 执行前，原始 key 上发生了新的 HSET/HDEL，这些变更不会被迁移到 tmp key 中，RENAME 后就会丢失。上面给出的三种方案覆盖了不同严格程度的场景：简单低峰窗口重建（可接受少量丢失）、CAS 乐观版本号（不容丢失）、暂停写入（最严格）。生产环境务必根据一致性需求选择合适的方案。此外，`HMSET` 在 Redis 4.0+ 已被标记为弃用（deprecated），建议改用 `HSET key field1 value1 field2 value2 ...` 多参数形式。

---

### 4.3 策略选型决策树

```
                      field 数会持续增长吗？
                               │
                  ┌────────是──┼──否────────────────────┐
                  │                                     │
            field 数上限可预估？                   策略 A：调大阈值即可
                  │                           (hash-max-listpack-entries=2000)
        ┌───是───┼───否──────────────────┐
        │                                 │
   上限多少？                       策略 E：应用层计数器 + 自动分桶
        │                           (完全透明，但多一次本地计数)
  ┌─────┼─────┐
  │           │
<2000    2000~10000+
  │           │
策略 A    是时序数据吗？
调大阈值    │
         ┌─是─┼─否──────────┐
         │                  │
     策略 C：          有明确的冷热
     时间分区            field 区分？
         │                  │
         │           ┌───是──┼───否──┐
         │           │                │
         │     策略 D：          策略 B：
         │     冷热拆分          垂直分片（哈希路由）
         │                      最通用，推荐
         │
         └─ 策略 F：定时任务兜底（所有策略都建议叠加）
```

### 4.4 组合策略（生产推荐）

**单靠一种策略不够，生产推荐的组合方案**：

```yaml
# 层级 1：Redis 服务端兜底
hash-max-listpack-entries: 2000     # 温和上调，作为安全网

# 层级 2：应用层分桶路由（策略 B）
sharding:
  target_fields_per_shard: 400      # 目标每片 field 数
  hash_algorithm: crc16             # 与 Cluster slot 一致

# 层级 3：定时巡检（策略 F）
guard:
  scan_interval_seconds: 600        # 每 10 分钟扫描
  auto_rebuild_threshold: 500       # field ≤500 且编码=hashtable 则自动重建
  alert_threshold: 1000             # 超过 1000 field 告警

# 层级 4：监控告警
alerts:
  - encoding == 'hashtable' → warning
  - field_count > 800 → 提前告警，避免到达 2000 才被动
```

```java
// 组合实现示例（写路径）
public void hsetSmart(String baseKey, String field, String value) {
    // Step 1：确定分片（策略 B）
    String shardKey = resolveShardKey(baseKey, field);

    // Step 2：写入
    redis.hset(shardKey, field, value);

    // Step 3：异步采样检查 field 数（5% 概率）
    if (ThreadLocalRandom.current().nextInt(100) < 5) {
        long fieldCount = redis.hlen(shardKey);
        if (fieldCount > 400 * 1.2) {
            alertManager.warn("Hash " + shardKey
                + " approaching limit: " + fieldCount);
        }
    }
}
```

### 4.5 常见反模式

| 反模式 | 为什么危险 | 正确做法 |
|:--|:--|:--|
| 把所有用户属性放入一个 Hash | 一个 key 几千 field → 必然退化 hashtable | 冷热拆分 / 垂直分片 |
| 用 field 名做 ID（`user:profile` → field=`id:10001`） | field 数无限增长 | 把 ID 放进 key：`user:10001:profile` |
| 设置 `hash-max-listpack-entries 0`（禁用 listpack） | **所有 Hash 都是 hashtable**，内存浪费 50% | 永远不要设 0 |
| 设置 `hash-max-listpack-entries 100000` | listpack 太大，HSET/DEL 是 O(n) memmove | 上限不超 2000 |
| field 数超阈值后重建但不暂停写入 | 写入丢失 | RENAME 原子替换 + 重建期间用版本号 |

### 4.6 极简总结（六策略速查）

| 策略 | 适用场景 | 核心原理 | 改动量 |
|:--|:--|:--|:--|
| **A：调大阈值** | field 数在 500~2000 的温和场景 | 放宽 listpack 触发条件 | 零代码 |
| **B：垂直分片** | ⭐ 最通用，field 数不可控 | 哈希路由，拆成 N 个小 Hash | 应用层加路由 |
| **C：时间分区** | 时序缓存（分钟/小时级统计） | 时间窗口为分片维度 | 中等 |
| **D：冷热拆分** | 有明显高频/低频 field 区分 | 热 field 在 listpack，冷 field 可退化 | 需定义 hot field 集合 |
| **E：自动分桶** | 业务完全透明 | 计数器触发分片 | 多一次本地计数 |
| **F：定时巡检** | **所有策略的兜底** | 检测退化 + 自动重建 | 低 |

> **生产推荐**：**B（垂直分片，目标 400 field/片）+ A（调大阈值为 2000 做兜底）+ F（10 分钟巡检自动修复）**。
>
> **注意**：2000 这个上限是经验值，依赖于 value 大小和 Redis CPU 性能。在生产上线前必须在相同硬件上做压测，确认 listpack 的 HSET/HGET/HGETALL 在目标 field 数下不会成为延迟瓶颈。

### 4.7 Redis Cluster 环境下的分片注意事项

当应用层分片（策略 B/C/D/E）运行在 Redis Cluster 环境时，需注意以下问题：

| 场景 | 风险 | 缓解措施 |
|:--|:--|:--|
| `HGETALL` 跨多个分片 key | 各分片 key 可能落在不同 slot → 不同节点 | Pipeline 并发请求可缓解，但非事务保证 |
| 多 key 操作（如 `MGET` 多个分片 key） | Cluster 禁止跨 slot 的多 key 命令 | 使用 hash tags `{...}` 将同一逻辑 Hash 的所有分片绑定到同一 slot |
| Lua 脚本操作多个分片 | 脚本中的 key 必须属于同一节点 | 用 hash tag 确保分片 key 在同一 slot |

**推荐 hash tag 方案**：

```
原始分片 key：user:profile:0, user:profile:1, user:profile:2
Cluster hash tag：user:profile:{shard}:0, user:profile:{shard}:1, user:profile:{shard}:2
                   → "{shard}" 部分被用于计算 slot，确保所有分片在同一个节点
```

```java
// Cluster 兼容的分片 key 生成
public static String clusterSafeShardKey(String baseKey, int shardIdx) {
    // 使用 {baseKey} hash tag，确保所有分片在同一 slot
    return "{" + baseKey + "}:" + shardIdx;
}
```

---

## 五、ZSet member 数量控制：避免退化为 skiplist 的策略

> **核心目标**：让 ZSet 尽可能保持在 **listpack 紧凑编码**，避免触发 `zset-max-listpack-entries`（默认 128）阈值后转为 `skiplist + dict`（内存通常明显增加，经验上常见 **1.8–2.5×**，且不可自动回退——具体倍数取决于 member 平均长度与实现细节）。

### 5.1 问题量化：退化前后的差距到底多大

> 注：以下示例数值基于 **64-bit glibc+jemalloc、member 平均长度 ≈16B、score 用 double 编码约 8–16B、listpack 编码限制**。这些是**估算值**，实际值取决于 member 长度、编码（listpack vs skiplist）及 allocator rounding，**请在生产环境压测核实**（相对级别：listpack 紧凑为 KB 级；skiplist+dict 在同样 member 条件下内存约 1.8–2.5×）。

| 编码 | 100 member | 200 member | 1000 member |
|:--|:--|:--|:--|
| **listpack** | ~12KB（估算） | —（超出 128，不存在） | — |
| **skiplist + dict** | — | ~40KB+（估算） | ~200KB+（估算） |
| 倍数 | — | 相对 listpack 约 **1.8–2.5×** | — |

**退化触发**：在默认配置（`zset-max-listpack-entries=128`、`zset-max-listpack-value=64`）且 member/score 长度未超过阈值的情形下，**写入第 129 个 member 将触发从 listpack→skiplist+dict 的升级**。实际触发是满足**任一阈值**（entries 或 value size）时发生，且阈值可通过 redis.conf/`CONFIG SET` 修改。

#### ZSet 退化比 Hash 更痛苦的原因

| 维度 | Hash（hashtable） | ZSet（skiplist + dict） |
|:--|:--|:--|
| 退化后的结构数 | 1 套 dict | **2 套结构**（skiplist + dict），同时维护 |
| 内存膨胀 | ~2×（估算） | ~2~2.5×（估算，取决于 member 长度） |
| 写入复杂度 | O(1) | O(log N) |
| 跨分片查询难度 | 低（HGETALL 聚合即可） | **高**（ZRANGE/ZRANK 需跨分片排序合并） |
| 恢复紧凑编码的难度 | 中等 | **高**（重建时需保持 score 顺序一致） |

**ZSet 退化后恢复的代价远大于 Hash**，因为 ZSet 的 member 是按 score 排序的，任何重建都必须重新计算顺序。**重建要点**：需要把 member + score 正确迁移到新的 listpack 形式——要保证顺序通常应使用 **ZRANGE WITHSCORES**（按 score 有序获得全部成员）或 **ZSCAN + 客户端排序**（ZSCAN 是无序迭代，需客户端排序）；重建期间需用一致性策略（暂停写、乐观 CAS 版本号、或低峰窗口 RENAME）避免丢失/冲突。与 Hash 相比，**ZSet 的排序语义使重建与回退代价更高且更容易出问题**。

### 5.2 策略全景

```
                      ┌── 策略 A：调大阈值（最简单）
                      │
   控制 member 数 ──┼── 策略 B：垂直分片——按 member 哈希分桶（最通用）
                      │
                      ├── 策略 C：时间分区（适合时序排行榜）
                      │
                      ├── 策略 D：Top-N + 归档（适合排行榜场景）
                      │
                      ├── 策略 E：应用层计数器 + 自动分桶（对业务透明）
                      │
                      └── 策略 F：主动检测 + 强制重建（兜底策略）
```

---

### 策略 A：调大 `zset-max-listpack-entries`（最简单但有限）

```conf
# redis.conf
zset-max-listpack-entries 500    # 默认 128，温和上调
zset-max-listpack-value 128      # 默认 64B，适当放大
```

```bash
# 运行时动态调整（不重启）
CONFIG SET zset-max-listpack-entries 500
```

| 优点 | 缺点 |
|:--|:--|
| 零代码改动 | listpack 越大，ZADD/ZRANGE 的 O(N) 线性扫描开销越大 |
| 即刻生效 | 建议上限 **500**（**经验值**）；member 的 listpack 没有哈希索引，ZSCORE/ZRANK 需全量扫描 |
| 适合 member 数在 128~500 的温和增长 | **不解决根本问题**——持续增长迟早突破 |

> ⚠️ **经验值，强烈建议在本地/生产等同硬件上压测验证**：阈值可能需要调整（若 member 较短可适当放大；若 member 较长建议更保守）。**不要把阈值设为极端值**——设 0 会禁用 listpack（所有 ZSet 都变 skiplist，内存大增）；设过大（如 5000）会使 listpack 过大，导致 O(N) 操作（ZRANK/ZSCORE/ZRANGE）变慢，务必压测。

> **ZSet listpack 的性能陷阱**：Hash listpack 的 HGET 虽然也是 O(N)，但至少 field 名直接存储，通过 memcmp 匹配。ZSet 的 listpack 中 member+score 成对排列，ZRANK（获取排名）需要扫描整个 listpack 累计 score 数量——500 member 时 ZRANK 可能已经显著慢于 skiplist 的 O(log N)。建议不要超过 500。

---

### 策略 B：垂直分片——按 member 哈希分桶（最通用，推荐）

**思想**：把一个 ZSet 拆成 N 个物理小 ZSet，用哈希路由。每个分片保持在 listpack 编码内。

```
原始：
  ZADD leaderboard m1 100  m2 200  ...  m1000 50000

拆分后：
  leaderboard:0  ── ZADD leaderboard:0 m_xxx 100 ...    (≤128 member)
  leaderboard:1  ── ZADD leaderboard:1 m_yyy 200 ...    (≤128 member)
  ...
  leaderboard:7  ── ZADD leaderboard:7 m_zzz 50000 ...  (≤128 member)
```

```java
public class ZSetShardingUtils {

    private static final int TARGET_MEMBERS_PER_SHARD = 100;  // ~22% 余量（128 × 0.78）

    public static int shardCount(int totalMembers) {
        return Math.max(1, (totalMembers + TARGET_MEMBERS_PER_SHARD - 1)
                / TARGET_MEMBERS_PER_SHARD);
    }

    public static int shardIndex(String member, int shardCount) {
        // ⚠️ 生产建议使用明确的哈希函数（CRC16 / MurmurHash3 / XXHash）以获得跨语言一致性；
        // 不要依赖 Java String.hashCode() 作为跨语言/跨进程的规范哈希函数。
        return Math.abs(member.hashCode() % shardCount);
    }

    public static String shardKey(String baseKey, int shardIdx) {
        return baseKey + ":s" + shardIdx;
    }

    // ==================== 写操作 ====================

    // ZADD（单 member）
    public void zadd(String baseKey, String member, double score) {
        int total = estimateTotalMembers(baseKey);
        int count = shardCount(total);
        int idx = shardIndex(member, count);
        redis.zadd(shardKey(baseKey, idx), score, member);
    }

    // ZREM
    public void zrem(String baseKey, String member) {
        int total = estimateTotalMembers(baseKey);
        int count = shardCount(total);
        int idx = shardIndex(member, count);
        redis.zrem(shardKey(baseKey, idx), member);
    }

    // ==================== 读操作 ====================

    /**
     * ZSCORE（单 member 查分）
     */
    public Double zscore(String baseKey, String member) {
        int total = estimateTotalMembers(baseKey);
        int count = shardCount(total);
        int idx = shardIndex(member, count);
        return redis.zscore(shardKey(baseKey, idx), member);
    }

    /**
     * ZRANK（全局排名）
     *   → 问题：分片后 member 的在分片中的排名 ≠ 全局排名
     *   → 解法：先在该 member 所在分片查 localRank，再加上所有 score 比它高的分片的总 member 数
     *   → 注意：ZRANK 返回 0-based 排名，ZCOUNT 需用 exclusive 区间，且要对每个分片各执行一次 ZCOUNT
     */
    public Long zrank(String baseKey, String member) {
        int total = estimateTotalMembers(baseKey);
        int count = shardCount(total);
        int myShard = shardIndex(member, count);
        double myScore = redis.zscore(shardKey(baseKey, myShard), member);
        if (myScore == null) return null;

        Long localRank = redis.zrank(shardKey(baseKey, myShard), member);  // 0-based
        // 遍历其他分片，统计 score > myScore 的 member 数
        long globalRank = localRank;
        for (int i = 0; i < count; i++) {
            if (i == myShard) continue;
            // 用 (myScore, +inf) EXCLUSIVE 区间，统计严格大于 myScore 的 member 数
            // 注意：不同客户端/版本对 score 区间的 inclusive/exclusive 语法略有差异，需确认
            globalRank += redis.zcount(shardKey(baseKey, i),
                    new ZRangeBounds<>(ScoreBoundary.exclusive(myScore), ScoreBoundary.unbounded()));
        }
        return globalRank;  // 0-based 全局排名；若需 1-based，调用方 +1
    }

    /**
     * ZRANGE [start, stop]（全局 Top-N）
     *   → 问题：分片后无法直接从单个分片拿 Top-N
     *   → 解法：从每个分片取 K（K = stop - start + 1）个候选，客户端做 K 路归并
     */
    public List<String> zrange(String baseKey, int start, int stop) {
        int total = estimateTotalMembers(baseKey);
        int count = shardCount(total);
        int k = stop - start + 1;          // 全局需要的最大数量
        int fetchPerShard = k;             // 每片至少取 K；可多取一些作为 buffer

        // 第一步：从每个分片取 top(fetchPerShard)，Pipeline 并发
        List<List<ScoredValue<String>>> allShards = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            String key = shardKey(baseKey, i);
            allShards.add(redis.zrangeWithScores(key, 0, fetchPerShard));
        }

        // 第二步：K 路归并（按 score 降序）
        PriorityQueue<ShardEntry> pq = new PriorityQueue<>(
            (a, b) -> Double.compare(b.score, a.score));  // 大顶堆

        int[] cursors = new int[count];
        for (int i = 0; i < count; i++) {
            if (cursors[i] < allShards.get(i).size()) {
                ScoredValue<String> sv = allShards.get(i).get(cursors[i]);
                pq.offer(new ShardEntry(sv.getValue(), sv.getScore(), i, cursors[i]));
                cursors[i]++;
            }
        }

        // 第三步：弹出 Top-N
        List<String> result = new ArrayList<>();
        int rank = 0;
        while (!pq.isEmpty() && rank <= stop) {
            ShardEntry entry = pq.poll();
            if (rank >= start) {
                result.add(entry.member);
            }
            rank++;
            // 从同一分片补下一个元素
            if (entry.cursor + 1 < allShards.get(entry.shardIdx).size()) {
                ScoredValue<String> next = allShards.get(entry.shardIdx).get(entry.cursor + 1);
                pq.offer(new ShardEntry(next.getValue(), next.getScore(),
                          entry.shardIdx, entry.cursor + 1));
            }
        }
        return result;
    }
}
```

> ⚠️ **实现全局 ZRANK 的要点**：先找出 member 所在分片并取 `localRank`（**0-based**）；再对其它分片执行 `ZCOUNT (myScore, +inf)`（**exclusive**，统计严格更大的元素）求和，最后合并并考虑 **0-based/1-based** 差别。此过程需要对每个分片各执行一次 ZCOUNT（O(log N)），总体成本为 **O(#shards × log N_per_shard)**，在高并发或大分片数时代价显著。

> ⚠️ **正确求全局 Top-K**：从每个分片只取 `stop+1` 个并不能保证得到全局 Top-(stop+1)。必须从每片取**至少 K（K = stop - start + 1）的候选元素**（可多取作为 buffer）再做 K 路归并。**更高效的做法**：每片先取较小 batch（如 K），归并后若结果不足 K，再向需要的分片按需拉取下一批（**惰性拉取**），减少不必要 IO。**若 shardCount 很大**，单次从每片拉取 K 会产生大量 IO，应使用惰性拉取或维护 Top-N 索引。

| 优点 | 缺点 |
|:--|:--|
| **member 数无限扩展**，每片始终 < 100 | ZRANGE/ZRANK 需要跨分片聚合（需客户端 K 路归并） |
| 每片在 listpack 编码内，内存最优 | ZRANK 的准确实现需要每片做 ZCOUNT（O(log N) per shard） |
| ZSCORE 直接哈希路由，O(1) 定位 | 分片数变化时需要迁移数据（member 需要 rehash 到新分片） |

> **ZSet 分片的核心代价**：排序操作（ZRANGE/ZRANK/ZREVRANGE）失去原子性，需要客户端做归并。如果业务强依赖全局排序的原子性，考虑策略 D（Top-N + 归档）或接纳 skiplist。

---

### 策略 C：时间分区（适合时序排行榜——如每小时/每天热门）

**场景**：以时间维度写入，每个时间窗口一个 ZSet。

```
原始：
  ZADD hot_articles 2026-07-27-14:30 1234 ...  (10000+ member → skiplist)

拆分后：
  ZADD hot_articles:2026-07-27-14  07-27-14:30 1234  （1 小时窗口，~几十 member）
  ZADD hot_articles:2026-07-27-15  07-27-15:00 5678
```

```java
public class TimeBasedZSet {

    /**
     * 按小时分桶的 ZSet key
     */
    public static String hourlyKey(String baseKey, long timestampMs) {
        long hourEpoch = timestampMs / (3600 * 1000L);
        return baseKey + ":h" + hourEpoch;
    }

    // ZADD
    public void zaddHourly(String baseKey, String member, double score) {
        String key = hourlyKey(baseKey, System.currentTimeMillis());
        redis.zadd(key, score, member);
        redis.expire(key, 86400 * 7);  // 保留 7 天
    }

    // 查询最近 N 小时的 Top-K（跨时间窗口聚合）
    public List<String> topKLastNHours(String baseKey, int nHours, int k) {
        long now = System.currentTimeMillis();
        long hourEpoch = now / (3600 * 1000L);

        // 从最近的 N 个窗口取 Top-K，然后归并
        List<List<ScoredValue<String>>> windowTops = new ArrayList<>();
        for (int i = 0; i < nHours; i++) {
            String key = baseKey + ":h" + (hourEpoch - i);
            windowTops.add(redis.zrevrangeWithScores(key, 0, k - 1));  // 每窗口取 topK
        }

        // K 路归并
        return mergeTopK(windowTops, k);
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 天然适合时序排行榜数据（热门/最新/实时） | 跨窗口 Top-K 查询仍需客户端归并 |
| 历史窗口按 TTL 自动过期，无需手动清理 | 聚合查询复杂度 O(窗口数 × log K) |

---

### 策略 D：Top-N + 归档（适合固定大小的排行榜）

**思想**：排行榜的核心需求是"只看 Top-N"（如前 100 名）。把**活跃 Top-N 保留在一个 ZSet**（listpack 编码，始终 ≤ 128），其余归档到另一个 ZSet（可接受 skiplist）。

```
leaderboard:top100   ← 前 100 名，始终 ≤ 128 member
                       → ZADD 后如果 member > 128，修剪到 Top-100
                       → 始终保持在 listpack 编码

leaderboard:archive  ← 第 101+ 名，归档用
                       → 低频访问，可接受 skiplist
                       → 定期 TTL 或迁移到冷存储
```

```java
public class TopNZSet {
    private static final int TOP_N = 100;        // 活跃榜保留前 100
    private static final int MAX_LISTPACK = 128;  // 留 28 个余量

    public void zadd(String baseKey, String member, double score) {
        String topKey = baseKey + ":top";
        String archiveKey = baseKey + ":archive";

        // 1. 写入 Top-N 榜
        redis.zadd(topKey, score, member);

        // 2. 如果 member 数超过 MAX_LISTPACK，修剪到 TOP_N
        long size = redis.zcard(topKey);
        if (size > MAX_LISTPACK) {
            // 被修剪掉的 member 移到归档榜
            Set<ScoredValue<String>> removed =
                redis.zrangeWithScores(topKey, TOP_N, -1);
            for (ScoredValue<String> sv : removed) {
                redis.zadd(archiveKey, sv.getScore(), sv.getValue());
            }
            // 执行修剪
            redis.zremrangebyrank(topKey, TOP_N, -1);
        }
    }

    // Top-N 查询走 listpack，极快
    public List<String> top100(String baseKey) {
        return redis.zrevrange(baseKey + ":top", 0, TOP_N - 1);
    }

    // 查询所有排名（含归档）→ 需跨两个 key 聚合
    public Long zrank(String baseKey, String member) {
        Long topRank = redis.zrank(baseKey + ":top", member);
        if (topRank != null) return topRank;

        // 在归档榜中，排名 = TOP_N + 在归档中的排名
        Long archiveRank = redis.zrank(baseKey + ":archive", member);
        return archiveRank == null ? null : TOP_N + archiveRank;
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 高频 Top-N 查询走 listpack，O(N) 扫描量极小（≤128） | 需要定义明确的 Top-N 阈值 |
| Top-N 始终在紧凑编码，核心路径零退化风险 | 归入归档的 member 查询排名需要跨 key 聚合 |
| 适合固定大小的排行榜（热榜 Top-100/200） | 不适合"全量排名"需求的场景 |

---

### 策略 E：应用层计数器 + 自动分桶（对业务透明）

**思想**：与 Hash 的策略 E 类似，在应用层维护 member 计数，接近阈值时自动创建新分片。

```java
public class AutoShardingZSet {
    private static final int MAX_MEMBERS_PER_SHARD = 100;
    private static final String COUNT_KEY_PREFIX = "meta:zset:count:";
    private final AtomicLong localCounter = new AtomicLong(0);
    private static final int SYNC_THRESHOLD = 50;

    public void zadd(String baseKey, String member, double score) {
        // 1. 本地聚合计数（减少 Redis INCR 次数）
        long local = localCounter.incrementAndGet();
        if (local % SYNC_THRESHOLD == 0) {
            redis.incrBy(COUNT_KEY_PREFIX + baseKey, SYNC_THRESHOLD);
            redis.expire(COUNT_KEY_PREFIX + baseKey, 3600);
        }

        // 2. 确定分片
        long total = redis.incrBy(COUNT_KEY_PREFIX + baseKey, 0);  // 读当前值
        int shardIdx = (int) ((total - 1) / MAX_MEMBERS_PER_SHARD);
        String shardKey = baseKey + ":s" + shardIdx;

        // 3. 写入
        redis.zadd(shardKey, score, member);
    }

    // ZRANGE Top-K：每片取 Top-K，归并
    public List<String> zrevrange(String baseKey, int start, int stop) {
        long total = redis.incrBy(COUNT_KEY_PREFIX + baseKey, 0);
        int count = (int) ((total + MAX_MEMBERS_PER_SHARD - 1) / MAX_MEMBERS_PER_SHARD);
        int k = stop + 1;

        List<List<ScoredValue<String>>> shardTops = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            shardTops.add(redis.zrevrangeWithScores(baseKey + ":s" + i, 0, k - 1));
        }
        return mergeTopK(shardTops, k).subList(start, Math.min(stop + 1, mergedSize));
    }
}
```

| 优点 | 缺点 |
|:--|:--|
| 业务层完全透明 | ZADD 多一次本地计数 |
| 自动触发分片，无需预估 member 总数 | ZRANGE/ZRANK 需客户端归并（同策略 B） |
| 不会意外退化 | 本地计数器在进程重启时可能丢失少量计数 |

---

### 策略 F：主动检测 + 强制重建（兜底策略）

当意外退化发生时，用定时任务检测并重建。

```java
public class ZSetEncodingGuard {

    @Scheduled(fixedDelay = 600_000)  // 每 10 分钟
    public void repairEnlargedZSets() {
        for (String key : monitoredZSetKeys) {
            String encoding = redis.objectEncoding(key);
            if ("skiplist".equals(encoding)) {
                long memberCount = redis.zcard(key);
                if (memberCount <= 200) {
                    // member 数已降回阈值以下但编码仍未变 → 重建恢复 listpack
                    rebuildAsListpack(key);
                }
                // memberCount > 200 则走策略 B/C/D 主动分片
            }
        }
    }

    private void rebuildAsListpack(String oldKey) {
        String tmpKey = oldKey + ":tmp:" + System.currentTimeMillis();

        // 若集合可一次性拉取且能接受排序：优先用 ZRANGE 0 -1 WITHSCORES（按 score 有序），直接批量 ZADD 到 tmpKey
        // 若集合非常大：用 ZSCAN 分批拉取，收集后在客户端按 score 排序再批量写入 tmpKey
        long cursor = 0;
        List<ScoredValue<String>> allMembers = new ArrayList<>();
        do {
            ScanResult<ScoredValue<String>> scanResult =
                redis.zscan(oldKey, cursor, ZSCAN_COUNT_500);  // ZSCAN 无序，需客户端排序
            allMembers.addAll(scanResult.getResult());
            cursor = scanResult.getCursor();
        } while (cursor != 0);

        // 按 score 排序后再批量 ZADD
        allMembers.sort(Comparator.comparingDouble(ScoredValue::getScore));

        // 批量 ZADD，务必传递服务器返回的 score 字符串/高精度文本，避免中间以低精度 double 转换丢失精度
        for (int i = 0; i < allMembers.size(); i += 100) {   // 每次 100~1000 减少 RTT
            int end = Math.min(i + 100, allMembers.size());
            Map<String, Double> batch = new LinkedHashMap<>();  // 保留插入顺序
            for (int j = i; j < end; j++) {
                ScoredValue<String> sv = allMembers.get(j);
                batch.put(sv.getValue(), sv.getScore());
            }
            redis.zadd(tmpKey, batch);  // 批量 ZADD，保持 score 原值
        }

        // 原子替换（同 Hash 策略 F，三种一致性方案可选）
        redis.rename(tmpKey, oldKey);
    }
}
```

> **ZSet 重建的特殊注意事项**：
> - **score 精度**：`ZRANGE WITHSCORES` 返回 score 的文本表达（客户端通常解析为 double）。若客户端以浮点数再序列化到字符串，理论上可能出现微小表示差异——**更稳妥的做法**是使用 `ZRANGE WITHSCORES` 获取服务器返回的 score 文本（或高精度字符串），并**直接将该文本作为参数传回 ZADD**（多数客户端接受字符串格式的 score），避免中间以低精度 double 转换。**ZSCAN 是无序的**，若用 ZSCAN 需客户端按 score 排序后再 ZADD；若可接受 `ZRANGE WITHSCORES`（按 score 有序）则更简单。ZSCAN 在高成员数时可分批避免一次性读取，但要注意顺序和重复问题（用 cursor 循环直到 0）。
> - **一致性问题**：`RENAME` 是原子的，但 `ZSCAN→ZADD` 与 `RENAME` 之间存在**写入丢失窗口**。若重建期间有 ZINCRBY（增量修改 score）或 ZADD，重建后的 tmpKey 会丢失这些写入。**要避免丢写**，选择：① 低峰窗口重建并接受少量丢写；② 乐观 CAS（版本号 + Lua 脚本）检测重建期间是否有并发写，有则放弃重试；③ 短暂停写后 RENAME。生产务必根据一致性需求选择方案（**同 Hash 策略 F 三种一致性方案**）。
> - **ZSet 重建比 Hash 慢**：skiplist 含有 double score，listpack 需要按 score 排序后成对存储，所以 ZSCAN + ZADD 的重建性能会在 member 数较大时下降。

---

### 5.3 策略选型决策树

```
                      member 数会持续增长吗？
                               │
                  ┌────────是──┼──否────────────────────┐
                  │                                     │
            member 数上限可预估？                   策略 A：调大阈值即可
                  │                           (zset-max-listpack-entries=500)
        ┌───是───┼───否──────────────────┐
        │                                 │
   上限多少？                       策略 E：应用层计数器 + 自动分桶
        │                           (完全透明，但 ZRANGE 需归并)
  ┌─────┼─────┐
  │           │
<500      500~10000+
  │           │
策略 A    需要全局排序的原子性？
调大阈值    │
         ┌─是──────────┼─否──────────┐
         │              │              │
   是固定大小         是时序数据？   策略 B：垂直分片
   排行榜吗？           │           (最通用，接受归并)
         │           ┌─是──┼─否──┐
    ┌─是─┼─否─┐      │          │
    │         │   策略 C：  策略 B：
策略 D： 接纳 skiplist  时间分区  垂直分片
Top-N+归档 (唯一选择)
    │
    └─ 策略 F：定时任务兜底（所有策略都建议叠加）
```

### 5.4 ZSet 分片后的操作兼容性矩阵

由于 ZSet 的排序特性，分片后不同操作的支持度差异很大：

| 操作 | 单 ZSet（未分片） | 分片后 | 实施方案 |
|:--|:--|:--|:--|
| **ZADD** | ✅ | ✅ | 哈希路由，O(1) 定位分片 |
| **ZSCORE** | ✅ | ✅ | 哈希路由，单分片查询 |
| **ZREM** | ✅ | ✅ | 哈希路由，单分片删除 |
| **ZCARD** | ✅ | ✅ | 各分片 `ZCARD` 求和 |
| **ZRANK / ZREVRANK** | ✅ | ⚠️ | 需跨分片 ZCOUNT 求和（见策略 B 代码） |
| **ZRANGE / ZREVRANGE** | ✅ | ⚠️ | 需客户端 K 路归并（见策略 B 代码） |
| **ZCOUNT** | ✅ | ⚠️ | 各分片 ZCOUNT 求和 |
| **ZINCRBY** | ✅ | ✅ | 哈希路由，同 ZADD |
| **ZUNIONSTORE / ZINTERSTORE** | ✅ | ❌ | **Cluster 中仅当所有输入 key 与输出 key 落到同一 slot 才可用**（可用 hash tag 实现）；分片后不同分片 key 通常分布在不同 slot，因此不可直接使用 |
| **BZPOPMIN / BZPOPMAX** | ✅ | ❌ | **阻塞命令仅在单个 key 上可靠**；跨分片无法实现相同语义（可轮询或应用层协调，但不是原子） |

> **红色线**：强依赖 `ZUNIONSTORE` / `ZINTERSTORE` / `BZPOP*` 这类多 key 原子操作的场景，**不要做分片**——分片后这些操作会完全失效。只调大阈值或接受 skiplist 是唯一选择。若确需在 Cluster 用 `ZUNIONSTORE`，所有参与 key 必须用 hash tag 强制同 slot（如 `{baseKey}:s0`、`{baseKey}:s1`）。

### 5.5 组合策略（生产推荐）

```yaml
# 层级 1：Redis 服务端兜底
zset-max-listpack-entries: 500      # 温和上调，作为安全网
zset-max-listpack-value: 128

# 层级 2：业务层选择最适合的策略
zset_strategy:
  # 排行榜 → 策略 D（Top-N + 归档）
  leaderboard:
    top_n: 100
    max_listpack_members: 128
  
  # 时序热门 → 策略 C（时间分区）
  hot_articles:
    partition_window: 3600           # 1 小时窗口
    
  # 通用 ZSet → 策略 B（垂直分片）
  general:
    target_members_per_shard: 100   # 经验值（128 × 0.78），需按实际 member 长度压测调整

# 层级 3：定时巡检（策略 F）
guard:
  scan_interval_seconds: 600        # 经验值
  auto_rebuild_threshold: 200       # 经验值：member ≤200 且编码=skiplist 则重建
  alert_threshold: 500              # 经验值：member > 500 告警

# 层级 4：监控告警
alerts:
  - encoding == 'skiplist' → warning
  - member_count > 300 → 提前告警
  - zrank/zrange_latency_p99 > 10ms → 检查是否退化
```

### 5.6 常见反模式

| 反模式 | 为什么危险 | 正确做法 |
|:--|:--|:--|
| 不分片直接把所有用户放进一个 ZSet 排行榜 | 百万 member → skiplist + dict，内存 200MB+ | Top-N + 归档 / 垂直分片 |
| 分片后试图用 `ZUNIONSTORE` 合并结果 | 分片的 key 不在同一 slot，直接报错 | 客户端归并（如 K 路归并 Top-K） |
| 分片后直接用 `ZRANK` 做全局排名 | `ZRANK` 只在单个分片内返回排名 | 需要跨分片 ZCOUNT 累计（见策略 B） |
| 设置 `zset-max-listpack-entries 0` | 所有 ZSet 都是 skiplist，内存浪费 50% | 永远不要设 0 |
| 设置 `zset-max-listpack-entries 5000` | listpack ZRANK 是 O(N) 扫描，5000 极慢 | 上限不超 500 |
| 重建 ZSet 时未保留 score 精度 | `ZRANGE` 取 score 后以低精度 double 再序列化，可能引入微差异 | 用 `ZRANGE WITHSCORES` 获取服务器返回的 score 文本并直接传回 `ZADD`（避免中间低精度转换）；或 `ZSCAN` 分批 + 客户端按 score 排序 |

### 5.7 极简总结（六策略速查）

| 策略 | 适用场景 | 核心原理 | 改动量 |
|:--|:--|:--|:--|
| **A：调大阈值** | member 数在 128~500 的温和场景 | 放宽 listpack 触发条件 | 零代码 |
| **B：垂直分片** | ⭐ 最通用，member 数不可控 | 哈希路由，拆成 N 个小 ZSet | 应用层加路由 |
| **C：时间分区** | 时序排行榜（每小时/每天热门） | 时间窗口为分片维度 | 中等 |
| **D：Top-N + 归档** | 固定大小排行榜（前 100/200 名） | 活跃 Top-N 在 listpack，其余归档 | 低 |
| **E：自动分桶** | 业务完全透明 | 计数器触发分片 | 多一次本地计数 |
| **F：定时巡检** | **所有策略的兜底** | 检测退化 + ZSCAN 重建 | 低 |

> **生产推荐**：**D（Top-N + 归档，适合排行榜）+ B（垂直分片，目标 100 member/片，适合通用场景）+ A（调大阈值为 500 做兜底）+ F（10 分钟巡检自动修复）**。

---

## 六、什么时候需要"客户端压缩为二进制再存"

### 6.1 明确应该做的场景

| 场景 | 理由 | 推荐算法 |
|:--|:--|:--|
| **大 JSON / XML（>1KB）存储为 String** | 字符串原始存储无任何压缩，JSON 有大量冗余空格和重复 key | zstd |
| **大文本 / 日志（>4KB）** | 文本熵低，压缩比极高（5-10×） | zstd |
| **二进制 blob（图片缩略图、小文件）** | 本来就紧凑，不需再压 | 保持原格式 |
| **跨语言序列化传输** | Protobuf / MessagePack 本身就是紧凑二进制 | Protobuf / MessagePack |

### 6.2 明确不该做的场景

| 场景 | 原因 |
|:--|:--|
| **小 value（<200B）** | 压缩后可能比原数据更大（压缩算法最小开销 ~20B），元数据占比高，省下的 body 微不足道 |
| **已用 Hash listpack 编码** | listpack 本身已是连续内存 + 紧凑存储，压缩 64B 的 field 收益极小 |
| **高频读场景** | 每次读都需解压，CPU 开销远超节省的网络带宽 |
| **需要 Redis 原子操作**（INCR、HSET field、ZADD） | 压缩后 Redis 无法理解结构，原子操作全部失效 |

### 6.3 压缩算法选型

```
压缩比：gzip ≈ zstd > lz4 > snappy
速度：  snappy > lz4 >> zstd(level 1) >> gzip
```

| 算法 | 压缩比（文本） | 压缩速度 | 解压速度 | Redis 场景推荐 |
|:--|:--|:--|:--|:--|
| **snappy** | 2-4× | 500 MB/s | 1800 MB/s | 不适合，压缩比太低 |
| **lz4** | 2-4× | 550 MB/s | 2200 MB/s | 高频读写、延迟敏感 |
| **zstd (level 1)** | 4-5× | 500 MB/s | 1400 MB/s | ⭐ **首选**：压缩比 + 速度平衡 |
| **zstd (level 6)** | 5-7× | 120 MB/s | 1200 MB/s | 冷数据、低频读 |
| **gzip (level 6)** | 5-8× | 50 MB/s | 250 MB/s | 仅冷数据，CPU 开销大 |

**zstd 是在"压缩比×速度"曲线上做到 Pareto 最优的已知算法**，2024-2025 年 Redis 场景的首选。

### 6.4 Java 实现示例

```java
// 依赖：com.github.luben:zstd-jni:1.5.6-5

// 写入：压缩后存入 Redis
byte[] compressed = Zstd.compress(jsonString.getBytes(StandardCharsets.UTF_8));
byte[] key = ("cache:user:" + userId).getBytes(StandardCharsets.UTF_8);
redis.set(key, compressed);

// 读取：取出后解压
byte[] compressed = redis.get(key);
if (compressed != null) {
    String json = new String(Zstd.decompress(compressed), StandardCharsets.UTF_8);
}
```

### 6.5 压缩代价

| 代价 | 影响 |
|:--|:--|
| **每次读需额外 CPU 解压** | 高频读写下"省的内存"可能被额外 CPU 反噬 |
| **无法修改压缩数据** | HSET / LPUSH / ZADD 无法作用于压缩后的 blob |
| **TTL 与 CAS 仍可用** | EXPIRE / SETNX 仍然有效，因为它们不依赖 value 内容 |
| **bigkeys 分析失效** | `redis-cli --bigkeys` 看到的是压缩后大小，不是真实数据量 |
| **调试困难** | 直接 `GET key` 返回的是乱码，需解压后才能阅读 |

### 6.6 压缩后必须附加元信息（magic + version + algo）

把二进制直接存入 Redis 会导致：
- 多语言/多客户端环境无法判断数据是否已压缩，用什么算法
- 生产故障时 `redis-cli` 直接调试困难
- 迁移/备份时数据语义不明

**推荐方案**：在 value 前附加固定长度的 header，或使用单独的 metadata key。

```java
// 方案 A：前 4 字节放 magic + version + algo
// magic(1B)=0xAA + algo(1B)=0x01(zstd)|0x02(gzip)|0x03(lz4) + version(1B)=0x01 + reserved(1B)=0x00
byte[] compressed = Zstd.compress(raw);
byte[] stored = new byte[4 + compressed.length];
stored[0] = (byte) 0xAA;   // magic
stored[1] = 0x01;           // algo: zstd
stored[2] = 0x01;           // version
stored[3] = 0x00;           // reserved
System.arraycopy(compressed, 0, stored, 4, compressed.length);
redis.set(key, stored);

// 读取时先解析 header
byte[] raw = redis.get(key);
if (raw != null && raw.length > 4 && raw[0] == (byte) 0xAA) {
    int algo = raw[1];
    byte[] data = Arrays.copyOfRange(raw, 4, raw.length);
    switch (algo) {
        case 1: return Zstd.decompress(data);
        case 2: return Gzip.decompress(data);
        // ...
    }
}
return raw;  // 无 magic，认为是未压缩数据（向后兼容）
```

```java
// 方案 B：并行 metadata key
// cache:user:123 → 压缩数据
// cache:user:123:meta → {"algo":"zstd","version":1,"originalSize":1024}
redis.set("cache:user:123", compressed);
redis.set("cache:user:123:meta", "{\"algo\":\"zstd\",\"version\":1,\"originalSize\":1024}");
redis.expire("cache:user:123", ttl);
redis.expire("cache:user:123:meta", ttl);
```

> **建议**：方案 A（value header）更适合大多数场景——单次 GET 即可获取所有信息，无额外 RTT；优于并行 meta key（方案 B 多一次 RTT）。并在客户端库中实现透明压缩层，做到**向后兼容**：无 magic 时视为未压缩数据，直接返回原始字节。

> **跨语言兼容性**：多语言客户端（Java/Go/Python/Node.js 等）需统一实现同一套 header 格式和解析逻辑。建议将 `encode/decode` 逻辑封装为共用库（或提供各语言 SDK），确保 magic byte + algo code + version 在不同平台含义一致。这是"前端压缩"方案中容易忽略的坑——Go 客户端压缩的数据，Python 客户端必须能解压。

---

## 七、概率数据结构：HyperLogLog / BloomFilter / Count-Min Sketch

以上所有策略都是基于 Redis 精确数据类型（String、Hash、Set、ZSet）的优化。当数据量达到**亿级**时，精确存储的内存成本无法承受，需要通过"用精度换内存"的概率数据结构来解决问题。

### 7.1 哪些是 Redis 自带的

| 数据结构 | 标准 Redis 自带？ | 最低版本 | 核心命令 |
|:--|:--|:--|:--|
| **HyperLogLog** | ✅ 是（内置） | Redis 2.8.9 | `PFADD`、`PFCOUNT`、`PFMERGE` |
| **BloomFilter** | ❌ 需要 Redis Stack / RedisBloom 模块 | Redis Stack 自带 | `BF.ADD`、`BF.EXISTS`、`BF.MADD` |
| **Count-Min Sketch** | ❌ 需要 Redis Stack / RedisBloom 模块 | Redis Stack 自带 | `CMS.INITBYDIM`、`CMS.INCRBY`、`CMS.QUERY` |

```bash
# 验证当前 Redis 是否支持
redis-cli MODULE LIST
# 空输出 → 只有 HyperLogLog 可用
# 包含 "bf"  → BloomFilter 可用
# 包含 "RedisBloom" → BloomFilter + CMS 都可用
```

### 7.2 HyperLogLog：去重统计，内存固定 12KB

#### 原理

HyperLogLog 是一种**近似去重计数**算法，无论统计多少亿个元素，内存固定 ≈ 12KB，标准误差 0.81%。

```
Set 存 1 亿 UV  → 数 GB~数十 GB（示例：取决于 member 长度与实现）
HLL 存 1 亿 UV  → ~12KB 内存
```

> ⚠️ **Set 内存示例说明**：如果把 1e8 个 user_id 作为正常 Redis Set 存储（考虑到 dictEntry、robj、SDS header、allocator rounding），内存常常会到**数 GB 量级**（示例可能为数十 GB 或更多，取决于 member 长度与实现）；相比之下 HLL 仅 12KB。**内存降低倍数（如 ~500,000×）基于典型原始存储假设，实际倍数取决于原始表示法、元素长度与元数据开销，请按实际场景用公式/压测确认。**

#### 命令

```bash
# 添加元素（多次添加同一个元素不膨胀）
PFADD uv:2026-07-28 user1 user2 user3 user1  # 重复 user1 只算一次

# 统计不重复元素数（近似值）
PFCOUNT uv:2026-07-28    # → 3（近似值，误差 ≤ 0.81%）

# 合并多天的 UV（求并集的去重计数）
PFMERGE uv:2026-07:week uv:2026-07-28 uv:2026-07-29 uv:2026-07-30
PFCOUNT uv:2026-07:week  # → 本周去重 UV
```

#### 适用场景

| 场景 | 命令 |
|:--|:--|
| **每日 UV/DAU 统计** | `PFADD daily:uv:2026-07-28 user_id` |
| **广告曝光去重人数** | `PFADD ad:123:impression:uv user_id` |
| **多维度合并 UV** | `PFMERGE` 合并多天/多页面 UV |
| **商品/文章展开次数** | `PFADD article:456:views user_id` |

#### 局限性

| 限制 | 影响 |
|:--|:--|
| **只能计数，不能查询具体元素** | 无法知道"user1 今天来过没"（需要 BloomFilter） |
| **无法删除元素** | 不支持 `PFREM` |
| **小数据量时可能不准** | <1000 元素时误差可能偏大，可用 Set 过渡或上层校正 |
| **标准误差 0.81%** | 对于精确账单/财务数据不可用 |

#### Java 示例

```java
// 标准 Redis（无需装任何模块，直接可用）
public class HyperLogLogService {
    
    // 记录 UV
    public void recordPageView(long userId, long pageId) {
        String key = "uv:page:" + pageId + ":" + LocalDate.now();
        redis.pfadd(key, String.valueOf(userId));
        redis.expire(key, 86400 * 30);  // 保留 30 天
    }
    
    // 查询当天 UV
    public long getTodayUV(long pageId) {
        String key = "uv:page:" + pageId + ":" + LocalDate.now();
        return redis.pfcount(key);
    }
    
    // 查询过去 7 天 UV（自动去重跨天用户）
    public long getWeeklyUV(long pageId) {
        String[] dailyKeys = new String[7];
        LocalDate today = LocalDate.now();
        for (int i = 0; i < 7; i++) {
            dailyKeys[i] = "uv:page:" + pageId + ":" + today.minusDays(i);
        }
        String mergeKey = "uv:page:" + pageId + ":week";
        redis.pfmerge(mergeKey, dailyKeys);
        redis.expire(mergeKey, 86400);  // 合并结果缓存 1 天
        return redis.pfcount(mergeKey);
    }
}
```

---

### 7.3 BloomFilter：判断"是否可能存在"

#### 原理

BloomFilter 回答的是：**"这个元素有没有出现过？"**（可能存在，或确定不存在）。

```
Set 存 10 亿手机号 → 数十 GB（示例，取决于 member 长度与实现）
BloomFilter 存 10 亿手机号（1% 误判率）→ 约 1.1–1.2 GB（非 500MB）
```

> **BloomFilter 内存估算公式**：位数 `m ≈ -n·ln(p)/(ln2)²`（bits）。对 n=1e9、p=0.01（1%）：`bits/elem ≈ 9.58 bits` → 总大小 ≈ 9.58e9 bits ≈ **~1.2e9 bytes ≈ 1.1–1.2 GB**（**不是 500MB**）。**误判率越低占用越多**，对内存的节省量高度依赖所选 p 与原始每元素占用。生产用公式或 RedisBloom 计算器评估并做压测。
>
> **内存降低倍数**（如 ~100×）基于典型原始存储假设，实际倍数取决于原始表示法、元素长度与元数据开销，请按实际场景确认。

**误判率解释**：
- `BF.EXISTS blacklist phone` → 0 = 确定不在黑名单 ✅
- `BF.EXISTS blacklist phone` → 1 = **可能**在（约 1% 误判率）

#### 安装方式

```bash
# 方式 1：Docker Redis Stack（推荐，一次装齐所有模块）
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest

# 方式 2：docker-compose 中指定镜像
# redis:
#   image: redis/redis-stack:latest

# 方式 3：标准 Redis + 手动加载 RedisBloom 模块
# redis.conf:
#   loadmodule /path/to/redisbloom.so
```

#### 命令

```bash
# 创建 BloomFilter（误判率 0.01 = 1%，容量 100 万）
BF.RESERVE blacklist 0.01 1000000

# 添加元素
BF.ADD blacklist 13812345678
BF.MADD blacklist 13812345678 13987654321 13611111111  # 批量添加

# 检查是否存在
BF.EXISTS blacklist 13812345678   # → 1（可能存在）
BF.EXISTS blacklist 13000000000   # → 0（确定不存在）

# 查看 BloomFilter 信息
BF.INFO blacklist
```

#### 适用场景

| 场景 | 命令 |
|:--|:--|
| **黑名单检查** | 手机号/设备 ID/信用卡号是否在黑名单库中 |
| **爬虫 URL 去重** | `BF.EXISTS crawled_urls url` → 0 则没爬过，可以抓取 |
| **缓存穿透保护** | 查询 DB 前先问 BF：确定不存在则直接返回空，不查 DB |
| **用户名/邮箱唯一性** | 注册时 `BF.EXISTS` → 0 则确定未注册 |
| **推荐去重** | 判断某个内容是否已推荐给该用户 |

#### 局限性

| 限制 | 影响 |
|:--|:--|
| **误判率（false positive）** | 可能把"不在的"误判为"在"（如 1%） |
| **无法删除元素** | 标准 BloomFilter 不支持 delete；RedisBloom 支持可伸缩/计数版本的 BloomFilter（能实现一定程度上的删除或扩容），**具体命令和参数请参考 RedisBloom 文档** |
| **信息不可见** | `BF.EXISTS` 返回布尔值，无法知道"误判时原始数据是谁" |
| **必须预估容量** | `BF.RESERVE` 的 capacity 参数影响内存和误判率；超出容量后误判率会上升 |
| **需要 Redis Stack** | 标准 Redis 不带，需额外安装模块 |

#### Java 示例

```java
// 需 Redis Stack（redis/redis-stack 镜像）
public class BloomFilterService {
    
    private static final double ERROR_RATE = 0.01;  // 1% 误判率
    private static final long CAPACITY = 10_000_000; // 1000 万容量
    
    // 初始化黑名单 BloomFilter
    @PostConstruct
    public void init() {
        try {
            // 先检查 key 是否存在，避免重启报错
            redis.bfReserve("blacklist:phones", ERROR_RATE, CAPACITY);
        } catch (Exception e) {
            // key 已存在，忽略
        }
    }
    
    // 加黑名单
    public void addToBlacklist(String phone) {
        redis.bfAdd("blacklist:phones", phone);
    }
    
    // 批量添加
    public void batchAdd(List<String> phones) {
        redis.bfMAdd("blacklist:phones", phones.toArray(new String[0]));
    }
    
    // 检查是否在黑名单
    public boolean isBlacklisted(String phone) {
        return redis.bfExists("blacklist:phones", phone);
    }
}
```

---

### 7.4 Count-Min Sketch：近似频率统计

#### 原理

CMS 统计**每个元素出现了多少次**，但结果是近似值（可能有高估，不会低估）。

```
ZSet 存 1 亿次的点击量排行榜 → 数 GB（示例，取决于原始表示）
CMS 存 1 亿次的点击量       → 取决于 width/depth（见下公式）
```

> **CMS 内存估算公式**：内存 ≈ `width × depth × counter_size`（bytes）。`width ≈ ceil(e/ε)` 控制误差 ε，`depth ≈ ceil(ln(1/δ))` 控制置信度 δ。**CMS 内存并不直接随元素总数 N 线性增长**，而是随目标误差/置信度决定。
>
> - 示例：width=2000, depth=5, counter_size=8B → 内存 ≈ 2000×5×8 = **~80 KB**；
> - 若需非常低误差（width 提到 1e6）：内存 ≈ 1e6×5×8 ≈ **~40 MB**。
>
> **为 1e8 次事件设计的 CMS 内存并非固定 50MB**，需按 ε/δ 计算 width/depth。给出绝对字节数前应先算这两个参数。

#### 命令

```bash
# 创建 CMS（宽 2000，深 5）—— 宽 × 深越大越精确，但内存越大
CMS.INITBYDIM click_counts 2000 5

# 增加计数
CMS.INCRBY click_counts user:1001 5      # 用户 1001 点击 +5
CMS.INCRBY click_counts user:1002 3      # 用户 1002 点击 +3

# 查询近似计数
CMS.QUERY click_counts user:1001          # → 约 5

# 查看 CMS 信息
CMS.INFO click_counts
```

#### 适用场景

| 场景 | 命令 |
|:--|:--|
| **文章点击量 Top-N** | 统计每篇文章的近似点击量，再用客户端排序取 Top-N |
| **API 调用频率统计** | 统计每个 IP/API Key 的调用次数统计 |
| **广告点击量** | 统计每个广告位的近似点击次数 |
| **热搜/趋势词** | 统计每个搜索词的近似搜索频率 |
| **流量画像** | 统计每个用户某个行为维度的近似次数 |

#### 局限性

| 限制 | 影响 |
|:--|:--|
| **只有高估（over-estimate）** | 实际点击 5 次，CMS 可能返回 6，但不会返回 4 |
| **无法列出所有元素** | 只知计数，不知"谁被统计过" |
| **不可删除** | 标准 CMS 不支持负增量安全回滚（删除会带来误差）；**若需要删除须谨慎**，某些实现/扩展可能支持减法或合并，或用 conservative update 技术改善误差 |
| **初始化需预估** | 宽/深选太小精度低，选太大浪费内存 |

#### Java 示例

```java
// 需 Redis Stack
public class CountMinSketchService {
    
    // 初始化 CMS：宽 2000，深 5
    @PostConstruct
    public void init() {
        try {
            redis.cmsInitByDim("article:clicks", 2000, 5);
        } catch (Exception e) {
            // 已存在，忽略
        }
    }
    
    // 记录点击
    public void recordClick(long articleId) {
        redis.cmsIncrBy("article:clicks", "article:" + articleId, 1);
    }
    
    // 查询点击量（近似值）
    public long getClickCount(long articleId) {
        return redis.cmsQuery("article:clicks", "article:" + articleId);
    }
}
```

---

### 7.5 不想装 RedisBloom 模块的替代方案

**HyperLogLog** 无需任何额外安装，标准 Redis 已内置。

**BloomFilter** 可以用 Lua + Bitmap 在标准 Redis 上自己实现（但精度和性能不如原生模块）：

```lua
-- 简陋版 BloomFilter（标准 Redis 可运行，不装任何模块）
-- 注意：示例中哈希值应由客户端计算并传入，而非在 Lua 内调用不存在的命令
-- KEYS[1] = bitmap key, ARGV[1..k] = 各哈希 bit 位置
local bitCount = 1000000  -- bitmap 大小（与客户端约定一致）
for i = 1, #ARGV do
    redis.call('SETBIT', KEYS[1], tonumber(ARGV[i]) % bitCount, 1)
end
return 1
```

```lua
-- Check 脚本（哈希位置由客户端传入）
local bitCount = 1000000
for i = 1, #ARGV do
    if redis.call('GETBIT', KEYS[1], tonumber(ARGV[i]) % bitCount) == 0 then
        return 0  -- 确定不存在
    end
end
return 1  -- 可能存在
```

> ⚠️ **重要**：示例中不能在 Lua 中直接调用 `redis.call('CRC16', ...)`——**Redis 本身不暴露 CRC16 命令**，Lua 环境也无法调用不存在的服务器命令。如要在纯 Redis + Lua 环境实现 BloomFilter，应在**客户端计算若干哈希**（MurmurHash/XXHash/SHA1 分段）并把对应 bit 位置传入 Redis 的 SETBIT/GETBIT；**更好的办法是直接使用 RedisBloom 模块**获得高性能成熟实现。

**Count-Min Sketch** 用标准 Redis **极难自己实现**（需要多维数组 + 取 min 操作），基本必须上 RedisBloom 模块。

### 7.6 三种概率结构的决策速查表

| 需求 | 用啥 | 自带？ | 内存量级（估算，需压测） | 精度 |
|:--|:--|:--|:--|:--|
| "今天来了多少不同用户" | **HyperLogLog** | ✅ | 12KB（固定） | 0.81% 误差 |
| "这个手机号在黑名单里吗" | **BloomFilter** | ❌ | ~1.1–1.2 GB / 10 亿条 (1%) | 1% 误判 |
| "每篇文章被点击了多少次" | **Count-Min Sketch** | ❌ | 取决于 width×depth（见公式） | 高估 ≤ 某个比率 |
| 需要精确结果 + 可承受内存 | **Set / ZSet** | ✅ | GB 级别 | 精确 |

> 以上内存为**估算值**，实际倍数取决于原始表示法、元素长度与元数据开销，请按实际场景确认。

```bash
# 快速决策
redis-cli MODULE LIST | grep bf
# 有输出 → BloomFilter + CMS 都可用
# 无输出 → 只有 HyperLogLog 可用
```

### 7.7 内存对比总览

```
精确存储 vs 概率存储（量级对比，估算值）

  Set             数十 GB（示例，取决于元素长度/实现）
  BloomFilter     1% 误判率、1e9 元素 → 约 1.1–1.2 GB
  HyperLogLog     12KB（固定，0.81% error）← 仅统计数量，不存具体元素
  Count-Min Sketch 取决于 width×depth（见公式）← 统计频率，不存具体元素

概率结构牺牲的是"精确"，换来的是内存降低（倍数取决于原始表示法，
元素长度与元数据开销；请按实际场景用公式/压测确认）。
```


### 7.8 Top-K：近似 Top-N 排行

> **⚠ 需要 Redis Stack / RedisBloom 模块**（`redis/redis-stack` 镜像自带）

#### 原理

Top-K 基于 **HeavyKeeper** 算法，用固定内存维护近似排名，**不存储具体元素，只存候选名单**。一旦元素频次足够进 Top-N，就记录下来；不够的自动淘汰。内存完全由初始化参数决定，与排行相关。

#### 命令

```bash
# 初始化 Top-K：保留 Top-10，宽度/深度控制精度和内存
TOPK.RESERVE hot_keywords 10 2000 7 0.9

# 增加计数（多次调用同一个 element 会累积）
TOPK.ADD hot_keywords "Java" "Redis" "Spring" "K8s"

# 查询当前 Top-K 列表（按频次降序）
TOPK.LIST hot_keywords

# 查询某个元素是否在 Top-K 内
TOPK.QUERY hot_keywords "Redis"       # → 1 在 / 0 不在

# 查看 Top-K 信息
TOPK.INFO hot_keywords
```

#### 适用场景

| 场景 | 为什么适合 |
|:--|:--|
| **热搜关键词 Top-10** | 每秒千级写入，只需 Top-N，无需全量排序 |
| **热门商品 Top-50** | 内存固定，不会因商品增长而膨胀 |
| **高频IP Top-100** | 防刷风控，找高频异常 IP |

#### 局限性

| 限制 | 说明 |
|:--|:--|
| 不存具体频次 | `TOPK.LIST` 只返回名单，不返回计数（要知道频次需配合 CMS） |
| 候选总量受参数限制 | `width × depth` 决定内存和精度，过低会丢长尾 |
| 极度倾斜分布效果最好 | 如果所有元素频率接近，Top-K 精度会显著下降 |
| 不支持删除单个元素 | 只能整体 `DEL` key 重建 |

**与 CMS 协同使用的常见做法**：**Top-K 维护候选名单，CMS 提供近似频次**——两者结合可同时得到近似排名与近似计数。示例：对 Top-K 输出的每个候选元素，再向对应 CMS 查询 `CMS.QUERY` 得到近似频次，即可得到"带近似计数的 Top-N 榜"（排名用 Top-K，频次用 CMS）。

#### 与 ZSet 对比

| | Top-K | ZSet |
|:--|:--|:--|
| 内存 | 固定（KB~MB 级） | O(N) 随 member 增长 |
| 精度 | 近似 | 精确 |
| 获取完整排行 | ❌ 只出 Top-N | ✅ ZREVRANGE |
| 查询任意 member | ❌ 只 Track Top-N | ✅ ZRANK/ZSCORE |

> **一句话**：需要精确 Top-100 用 ZSet；需要内存固定的近似 Top-N 用 Top-K。

### 7.9 Cuckoo Filter：支持删除的布隆过滤器

> **⚠ 需要 Redis Stack / RedisBloom 模块**

#### 原理

Cuckoo Filter 使用**双哈希表 + 布谷鸟驱逐**机制，每个元素存一个较短指纹（fingerprint）而非完整 key。相比标准 BloomFilter，它的关键优势是 **支持删除**（`CF.DEL`），且删除后误判率不会恶化。内存效率与 BF 相当或更好（取决于指纹长度和容量），远优于需要 3~4 倍内存的 Counting BloomFilter。

#### 命令

```bash
# 创建 Cuckoo Filter（误判率 1%，容量 100 万）
CF.RESERVE cf_blacklist 1000000

# 添加元素（支持 NX 避免重复添加计数问题）
CF.ADDNX cf_blacklist "phone:13800138000"

# 批量添加
CF.INSERT cf_blacklist ITEMS "user:1" "user:2" "user:3"

# 检查是否存在
CF.EXISTS cf_blacklist "phone:13800138000"   # → 1

# 删除元素（BF 做不到！）
CF.DEL cf_blacklist "phone:13800138000"      # → 1 删除成功

# 统计已添加的元素数（近似值）
CF.COUNT cf_blacklist

# 查看信息
CF.INFO cf_blacklist
```

#### 适用场景

| 场景 | 为什么选 CF 不选 BF |
|:--|:--|
| **临时黑名单** | 需要频繁添加和移除（BF 无法删除） |
| **去重 + 过期** | 设备指纹去重，定期清理过期条目 |
| **已推内容去重** | 推送完成后删除，释放空间 |

#### 注意事项

| 注意点 | 说明 |
|:--|:--|
| `CF.DEL` 只能删除已确认添加过的元素 | 如果元素从未被 `CF.ADDNX` 成功添加就去删，会报错 |
| 容量设太小会触发驱逐失败（插入失败） | `CF.RESERVE` 要预留 10-20% 余量 |
| 与 BF 不完全等价 | 满容量时 BF 只增加误判，CF 可能拒绝插入 |

#### Java 示例

```java
// Lettuce 示例
cfReserve("cf_users", 100_000, 0.01);
cfAdd("cf_users", "device:abc");
boolean exists = cfExists("cf_users", "device:abc");   // true
cfDel("cf_users", "device:abc");                       // 删除成功
```

### 7.10 T-Digest：近似百分位统计

> **⚠ 需要 Redis Stack / RedisBloom 模块**

#### 原理

T-Digest 使用**压缩的质心簇（centroid clusters）**近似数据分布。它不像原数据那样逐条存值，而是用少量质心表示大量数据点——数据密集区精度高（如 P50 附近），稀疏区精度低（如 P99.999 附近）。内存通常仅需 KB~MB 级即可处理亿级数据，适用于延迟监控、用户行为分析等场景。

#### 命令

```bash
# 创建 T-Digest
TDIGEST.CREATE latency_stats

# 添加观测值（支持多次添加）
TDIGEST.ADD latency_stats 1.2 1.5 0.8 3.1

# 查询指定百分位
TDIGEST.QUANTILE latency_stats 0.5   # → 1.5（P50 / 中位数）
TDIGEST.QUANTILE latency_stats 0.99  # → 3.0（P99）

# 查询值处于什么分位
TDIGEST.RANK latency_stats 1.5       # → [0.7]（约 P70）

# 查询分位数范围
TDIGEST.CDF latency_stats 1.0 2.0    # → 落在 [1.0, 2.0] 区间的占比

# 合并两个 T-Digest
TDIGEST.MERGE global_stats latency_stats_1 latency_stats_2

# 查看信息
TDIGEST.INFO latency_stats
```

#### 适用场景

| 场景 | 典型查询 |
|:--|:--|
| **API 延迟监控** | `P50/P95/P99` 毫秒延迟 |
| **用户行为分析** | 用户访问时长的 P50/P90 分位 |
| **容量规划** | 请求体积分布百分比 |
| **A/B 测试** | 实验组延迟 vs 对照组延迟分布对比 |

#### 局限性

| 限制 | 说明 |
|:--|:--|
| 不存原始数据 | 无法查询某一具体观测值，只能查百分位 |
| 近似结果 | P50 附近误差极小（<0.5%），P99.999 误差增大 |
| 不能替代精确统计 | 如果需要精确平均、精确 Top-N，用 ZSet 或 CMS |

> ⚠️ **模块版本说明**：CF（Cuckoo Filter）/ T-Digest 等命令与行为总体正确，但**模块版本和命令可能随 RedisBloom 版本变化**，生产前**以 RedisBloom 官方文档**确认命令细节（如 CF.RESERVE 容量、TDIGEST 精度参数），并先运行 `redis-cli MODULE LIST` 检查已加载模块及版本。

#### Java 示例

```java
// Lettuce 示例
tdigestCreate("latency_td");
tdigestAdd("latency_td", 1.2, 1.5, 0.8, 3.1, 2.0);
double p99 = tdigestQuantile("latency_td", 0.99);
double p50 = tdigestQuantile("latency_td", 0.5);
```

### 7.11 概率结构速查总览

| 结构 | 问题 | 内存 | 精度 | 删元素 | 存具体值 |
|:--|:--|:--|:--|:--|:--|
| **HLL** | 有多少不同元素？ | 12KB 固定 | 0.81% 误差 | ❌ | ❌ |
| **BloomFilter** | 在不在集合里？ | 可配（容量×误判率） | 可配误判率 | ❌ | ❌ |
| **Cuckoo Filter** | 在不在？需要删！ | 略大于 BF | 可配误判率 | ✅ | ❌ |
| **CMS** | 每个频率是多少？ | 可配（宽×深） | 可能高估 | ❌ | ❌ |
| **Top-K** | Top-N 是哪些？ | 固定（KB~MB） | 近似排名 | ❌ | 存 Top-N |
| **T-Digest** | P50/P99 是多少？ | KB~MB 级 | 近似百分位 | ❌ | ❌ |

**安装命令**（Docker Redis Stack 一次性装齐所有模块）：
```bash
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest
```

### 7.11 概率结构补充：校验与生产实践

**校验与计算工具/命令**：

- **BloomFilter 大小计算（Python 单行公式）**：
  ```python
  import math
  bits = -n * math.log(p) / (math.log(2)**2)   # n=元素数, p=误判率
  bytes = bits / 8
  ```
- **检查已加载模块及版本**（使用模块前先确认）：
  ```bash
  redis-cli MODULE LIST
  ```

**生产实践提示**：

1. **误差需业务层评估**：概率结构的误差要经业务层可接受性评估（A/B 测试或灰度），并配合**采样校准**（如用 HLL + Set 在小样本上做精确校验），**尤其当误差会驱动付费/计费/风控决策时不要盲目使用近似数据结构**。
2. **删除需求强优先 Cuckoo Filter**：对删除需求强烈的场景，优先 Cuckoo Filter（支持删除）而非标准 BloomFilter，或使用 Counting Bloom Filter 并评估内存代价。

## 八、场景化专用结构（内置 Bitmaps / Geospatial / Streams）

> 以下三种均为**标准 Redis 内置**，无需安装任何额外模块。它们基于现有数据类型实现，但提供专用命令应对特定场景。

### 8.1 Bitmaps：位图操作

#### 原理

Bitmaps 不是独立数据类型，而是 **String 类型的位操作接口**。Redis 将 String 视为连续 bit 数组，每条命令操作指定偏移量的 bit。一个 512MB 的 String 可表示 `512M × 8 = 42.9 亿` 个 bit。

#### 命令

```bash
# 设置指定偏移量的 bit（key 自动创建/扩容）
SETBIT sign:2025-07-29 1001 1     # 用户 1001 已签到
SETBIT sign:2025-07-29 1005 0     # 用户 1005 未签到

# 查询某 offset 的 bit
GETBIT sign:2025-07-29 1001       # → 1

# 统计 1 的数量（可指定字节范围）
BITCOUNT sign:2025-07-29          # → 总签到人数

# 一个月内全勤（所有天的签到做 AND）
BITOP AND full_attend sign:2025-07-01 sign:2025-07-02 ... sign:2025-07-31

# 获取第一个为 1 的 offset
BITPOS sign:2025-07-29 1          # → 第一个签到的用户 ID
```

#### 适用场景

| 场景 | 每用户内存 | 1 亿用户内存 |
|:--|:--|:--|
| **每日签到** | 1 bit | ~12MB / 日 |
| **用户在线状态** | 1 bit | ~12MB |
| **AB 实验分组** | 1 bit | ~12MB |
| **权限标识位** | 1 bit | ~12MB |

#### 与 Set 对比

| | Bitmaps | Set |
|:--|:--|:--|
| 每用户内存 | **1 bit** | ~100B+（hashtable） |
| 条件查询 | ✅ BITOP AND/OR/XOR | ❌ 需 SINTER 等大操作 |
| 灵活删除 | ❌ 只能 setbit 0 | ✅ SREM |
| 稀疏数据 | ❌ 浪费，用户 ID 跨度大时空占满 | ✅ 只存实际成员 |
| 用户 ID 不连续 | ❌ 空洞严重 | ✅ 无影响 |

> **关键判断**：用户 ID 连续或最大 ID 可接受（如 `uid < 1 亿`）→ Bitmaps；用户 ID 稀疏或不可预测（如 UUID）→ 不要用 Bitmaps。

#### Bitfields

Bitfields 是 Bitmap 的扩展，允许在同一个 key 中操作**多位整数字段**：

```bash
# 设置一个 8-bit unsigned int @ offset 0，值 100
BITFIELD player:42 SET u8 0 100    # 血量: 100
BITFIELD player:42 SET u8 8 50     # 魔法: 50
BITFIELD player:42 SET u16 16 5000 # 金币: 5000

# 原子自增
BITFIELD player:42 INCRBY u16 16 100  # 金币 +100

# 一次操作多个字段（原子）
BITFIELD player:42 SET u8 0 100 SET u8 8 50 INCRBY u16 16 100
```

**适用场景**：游戏角色属性（血量/魔法/金币紧凑存储）、IoT 设备状态位等。

### 8.2 Geospatial：地理位置

#### 原理

Geospatial 基于 **Sorted Set** 实现，使用 **Geohash 算法**将经纬度编码为一个可排序的数值。内部用 ZSet 的 skiplist 存储，所以 `ZREM`、`ZCARD` 等 ZSet 命令也能用（score 是 Geohash 值，member 是名称）。

#### 命令

```bash
# 添加位置（经度 在前，纬度 在后）
GEOADD shops 116.404 39.915 "星巴克国贸店"
GEOADD shops 116.418 39.921 "瑞幸建国门店"
GEOADD shops 116.391 39.907 "Manner王府井店"

# 计算两地点距离（默认单位 m，支持 km/mi/ft）
GEODIST shops "星巴克国贸店" "瑞幸建国门店" m   # → 1500.2

# 查询成员的经纬度
GEOPOS shops "星巴克国贸店"                     # → [116.404, 39.915]

# 获取 Geohash 字符串
GEOHASH shops "星巴克国贸店"                    # → wx4g09b...

# 搜索半径内的位置（附距离 + 坐标）
GEOSEARCH shops FROMMEMBER "星巴克国贸店" BYRADIUS 2 km \
    WITHCOORD WITHDIST ASC COUNT 10

# 按经纬度搜索
GEOSEARCH shops FROMLONLAT 116.40 39.91 BYRADIUS 5 km \
    WITHCOORD WITHDIST ASC
```

> **Redis 7 注意**：`GEORADIUS` / `GEORADIUSBYMEMBER` 在 6.2+ 已废弃，统一用 `GEOSEARCH` / `GEOSEARCHSTORE`。

#### 适用场景

| 场景 | 说明 |
|:--|:--|
| **附近的人/店** | `GEOSEARCH ... 5 km` 实时查附近商家 |
| **配送范围计算** | 判断地址是否在门店配送范围内 |
| **打车/外卖匹配** | 匹配最近的司机/骑手 |
| **地理围栏** | 用户进出特定区域触发推送 |

#### Java 示例

```java
// Lettuce 示例
GeoArgs.Unit km = GeoArgs.Unit.km;
GeoWithin<String> shops = redis.geosearch("shops",
    GeoSearch.fromMember("星巴克国贸店"),
    GeoSearch.byRadius(2, km),
    GeoArgs.Builder.withDistance().withCoordinates().asc());

for (GeoWithin<String> s : shops) {
    System.out.println(s.getMember() + " - " + s.getDistance());
}
```

#### 注意事项

| 注意 | 说明 |
|:--|:--|
| 不支持多边形/任意区域 | 只有半径搜索；多边形需额外计算过滤 |
| 精度有限 | Geohash 约 0.3m~5km 误差（取决于编码长度） |
| 底层是 ZSet | `GEOSEARCH` 结果按距离排序是正确的，但 `ZADD` 直接设 score 会破坏数据一致性 |

### 8.3 Streams：消息队列 / 事件流

#### 原理

Streams 是 Redis 5.0 引入的**持久化消息队列**，类似 Kafka 的简化版。核心概念：

```
Stream key "orders"
│
├── entry "1620000000-0"  { "user": "1001", "amount": "99.9" }
├── entry "1620000001-0"  { "user": "1005", "amount": "299.0" }
├── entry "1620000002-0"  { "user": "1008", "amount": "49.0" }
└── ...

消费者组 (Consumer Group)
├── consumer A (处理奇数)
└── consumer B (处理偶数)
    ↑
    每个 consumer 有自己的 offset（> 最后确认的 ID）
    未确认的消息进入 PEL (Pending Entries List)
```

- 每条消息有**全局唯一递增 ID**（时间戳-序号）
- 支持**消费者组**（类似 Kafka consumer group），自动分配消息
- **消息可持久化**（RDB/AOF），消费者 offset 也持久化
- **阻塞读取**（`XREAD BLOCK`）实现类似 Pub/Sub 但消息不丢失

#### 命令

```bash
# ═══ 基础操作 ═══

# 添加消息（ID 用 * 自动生成）
XADD orders * user_id 1001 amount 99.9 status "pending"

# 读取所有消息（从开头）
XREAD COUNT 10 STREAMS orders 0

# 阻塞读取新消息（等待 5 秒）
XREAD BLOCK 5000 STREAMS orders $

# 获取 Stream 长度
XLEN orders

# 按范围查询
XRANGE orders - + COUNT 5         # 所有消息，限 5 条


# ═══ 消费者组 ═══

# 创建消费者组（从头开始消费）
XGROUP CREATE orders group_order 0

# 消费者组读取（自动分发消息）
XREADGROUP GROUP group_order consumer1 COUNT 5 STREAMS orders >

# 确认消息处理完成
XACK orders group_order 1620000000-0

# 查看未确认消息（PEL）
XPENDING orders group_order

# 重新认领超时未确认的消息
XAUTOCLAIM orders group_order consumer1 60000 0
```

#### 适用场景

| 场景 | 说明 |
|:--|:--|
| **轻量消息队列** | 不需要 Kafka 重量级基础设施的场景 |
| **事件溯源** | 订单状态变更流、用户行为追踪 |
| **异步任务分发** | worker 池消费任务，支持 ACK/重试 |
| **消息广播** | 多个消费者组独立消费同一条 Stream |

#### 与 Kafka/RabbitMQ 对比

| | Redis Streams | Kafka | RabbitMQ |
|:--|:--|:--|:--|
| 持久化 | ✅ RDB/AOF | ✅ 磁盘 + 副本 | ✅ 磁盘 / 内存 |
| 消费者组 | ✅ | ✅ | ❌（需插件） |
| 消息回溯 | ✅ XRANGE | ✅ offset seek | ❌ 消费即删除 |
| 吞吐量 | 极高（内存级） | 极高（disk seq write） | 中高 |
| 运维复杂度 | ✅ 极低（Redis 自带） | 高（ZK/KRaft + Broker） | 中 |
| 适用规模 | 中~大 | 超大 | 中 |

#### 注意事项

| 注意 | 说明 |
|:--|:--|
| 消息不会自动删除 | 需手动 `XTRIM` / `XDEL` 或设置 `MAXLEN` 限制 |
| 消费速度跟不上会积压内存 | 设好 `MAXLEN` + 监控 PEL 积压 |
| 不支持消息分区 | 不如 Kafka 的 partition 并行能力 |

#### Java 示例

```java
// 发送消息
Map<String, String> msg = Map.of("user_id", "1001", "amount", "99.9");
redis.xadd("orders", XAddArgs.Builder.maxlen(10000), msg);

// 消费者组读取
List<StreamMessage<String, String>> msgs = redis.xreadgroup(
    Consumer.from("group_order", "consumer1"),
    XReadArgs.StreamOffset.from("orders", ReadFrom.lastConsumed()),
    XReadArgs.Builder.count(5)
);

for (StreamMessage<String, String> m : msgs) {
    String userId = m.getBody().get("user_id");
    // 处理消息...
    redis.xack("orders", "group_order", m.getId());
}
```

## 九、完整决策树

```
你要存的数据是什么类型？
│
├── 数值（计数器、自增 ID）
│   └── 直接用 Redis String SET 存整数
│       → int 编码自动触发，绕过 SDS 分配
│       → 支持 INCR / DECR 原子操作
│       无需任何额外压缩
│
├── 多个 key-value 对（对象属性，如用户信息、商品详情）
│   ├── 总 field 数 ≤ 512 且 每个 value ≤ 64B
│   │   └── 用 Hash，自动走 listpack 编码
│   │       → 比逐条 String 省 4-5 倍内存
│   │       → 支持 HSET / HGET 原子 field 级操作
│   │
│   └── 总 field 数 > 512 或 value > 64B
│       ├── 冷数据 / 低频读
│       │   └── 用 Hash + 每个 value zstd 压缩存 bytes
│       └── 热数据 / 高频读写
│           └── 用 Hash 原生存，不压缩
│               （或尝试提高 hash-max-listpack-entries 阈值）
│
├── 大 JSON / 文本（>1KB）
│   ├── 高频读写
│   │   └── 不压缩，直接存 String
│   │       （或拆字段为 Hash 减轻单条膨胀）
│   └── 低频读 / 冷数据
│       └── zstd 压缩后存二进制
│
├── 纯整数集合（去重、标签、用户组）
│   └── 使用 Set，自动走 intset 编码
│       → 比 hashtable 省 80% 内存
│       → 支持 SADD / SREM / SISMEMBER
│       新增非整数元素才降级为 hashtable
│
├── 有序排行榜 / 时间线
│   ├── member 少（<128）+ 短分值
│   │   └── 使用 ZSet，自动走 listpack 编码
│   └── 大量 member
│       └── ZSet skiplist，避免压缩（ZADD/ZRANK 需要结构完整性）
│
├── 消息队列 / Timeline 列表
│   └── 使用 List + list-compress-depth 调优
│       两端不压缩，中间节点压缩，兼顾读写性能
│
└── 必须保留 Redis 原子操作能力的
    └── 不要压缩！使用原生类型（String/Hash/List/ZSet）
        压缩后丧失 HSET/INCR/ZADD 等一切能力
```

---

## 十、RDB/AOF 持久化对内存的影响

在数据类型选型之外，**持久化机制本身的内存影响是容量规划中必须考虑的独立维度**。

### 10.1 RDB 快照：fork + COW 内存放大

```
正常：Redis 主进程占用 used_memory_rss
                                                            ┌── 父进程继续写入
fork() ──▶ 父子进程共享内存页                                 │
            └── 子进程遍历读取写 RDB                          │
                                                            └── 父写时触发 COW：
                                                                被修改的页复制一份给父进程
                                                                每复制一页 = +4KB 物理内存
```

**关键结论**：
- 近似所需物理内存 ≈ `used_memory_rss + last_cow_size × 安全裕度（1.5~2×）`
- COW 大小 = 父进程在子进程存活期间写操作触发复制的总字节数
- 关键监控字段：`rdb_last_bgsave_status`（上次 RDB 是否成功）、`rdb_last_cow_size`（上次 COW 字节数）、`rdb_last_save_time_sec`（上次 RDB 耗时）
- 若物理内存不足，OOM Killer 可能杀掉 Redis 进程

### 10.2 AOF rewrite：同样的 COW 机制

AOF rewrite 使用相同的 fork + COW 机制，差异在于子进程存活时长：
- AOF rewrite 快的场景（小文件）：单次 COW 小，但 rewrite 更频繁
- AOF rewrite 慢的场景（大文件）：单次 COW 可能接近甚至超过 RDB
- 关键监控字段：`aof_last_bgrewrite_status`、`aof_last_cow_size`

### 10.3 容量规划建议

| 策略 | 说明 |
|:--|:--|
| 物理内存 ≥ `used_memory_rss × 1.5~2` | 为 fork 时的 COW 峰值预留空间 |
| 把持久化移到 replica | 主节点关闭 RDB/AOF，副本承担持久化 |
| 避开高写时段触发持久化 | 配置 `save` / `auto-aof-rewrite` 参数匹配业务低谷 |
| 监控 `used_memory_rss` 与 `rdb_last_cow_size` / `aof_last_cow_size` | 当 `last_cow_size` 接近可用物理内存的 60-70% 时告警 |

### 10.5 AOF rewrite 与持久化调度实践

- **避开高峰期**：将 `save` / `auto-aof-rewrite` 参数与业务低峰窗口对应，或手动 `BGSAVE` / `BGREWRITEAOF` 触发在低峰期
- **副本承担持久化**：主节点专注于内存服务（关闭 RDB/AOF），在一台或多台 replica 上开启持久化作为备份源
- **AOF rewrite 触发策略**：适当调小 `auto-aof-rewrite-percentage`（如 100）以减少单次 rewrite 文件大小，配合 `auto-aof-rewrite-min-size`（如 64mb）避免频繁重写小文件
- **告警设置**：当 RDB 保存期间内存上涨 >30% 时触发缩容或延迟保存

### 10.4 lazyfree 机制

删除大对象/大键时，Redis 的同步删除会阻塞主线程。4.0+ 支持 lazyfree：
- `UNLINK key`（异步删除）
- `FLUSHDB ASYNC` / `FLUSHALL ASYNC`
- 配置 `lazyfree-lazy-eviction yes`（淘汰时异步释放）

**注意**：异步释放期间内存不会立即回收——`used_memory` 会短暂偏高直到后台线程完成对象释放。建议监控 `lazyfree_pending_objects`（若有）或后台回收队列长度，避免后台回收线程积压导致内存回收严重延后。

---

## 十一、场景验证：场景 1 压测的教训

回到本项目的 Lettuce Pipeline 压测（每秒 28 万次 SETEX）：

| 发现 | 原理 | 教训 |
|:--|:--|:--|
| 每秒 SETEX 425k → 287k（keySpace 扩大后） | 覆盖写避免了 dict rehash + SDS 分配，走的是更快路径 | **务必用大 keySpace 测首写** |
| 整数值 `SET k 12345` 比 `SET k "12345"` 省内存 | int 编码绕过 SDS 分配 | **能存整数就存整数** |
| Hash 批量存比逐条 SET 省 50% 内存 | listpack 连续内存零指针 | **对象存 Hash，不逐条 SET** |
| 压缩大 JSON 省 5× 空间但需要 CPU | 解压消耗 CPU，高并发下可能成为新瓶颈 | **压缩用于冷数据，不用于热路径** |

---

## 十二、监控与检查清单

### 12.1 关键命令

```bash
# 查看所有 key 的编码分布
redis-cli --bigkeys

# 查看特定 key 的内部编码
redis-cli OBJECT ENCODING mykey

# 查看内存碎片率
redis-cli INFO memory | grep mem_fragmentation_ratio

# 查看 memory 占用明细
redis-cli MEMORY STATS

# 查看特定 key 的内存占用
redis-cli MEMORY USAGE mykey
```

> **注意事项**：
> - `--bigkeys` 是 O(N) 全库扫描，在生产环境谨慎运行或限制在低峰期执行；建议用分级扫描或慢速后台任务代替频繁全库扫描。
> - `MEMORY USAGE` 在某些 Redis 版本中做采样/近似估算；新版本支持 `SAMPLES` 参数（如 `MEMORY USAGE mykey SAMPLES 0` 可传 0 做全量精确计算，对大复杂对象耗时更长），注意根据 key 大小选择合适的采样量。
> - `OBJECT ENCODING` 不会触发阻塞，可放心在生产使用。

### 12.2 告警阈值建议

| 指标 | 健康值 | 预警值 | 行动 |
|:--|:--|:--|:--|
| `mem_fragmentation_ratio` | 1.0-1.5 | > 2.0 | 重启实例整理碎片或开启 active-defrag |
| `active_defrag_running` | 0（空闲） | 持续为 1 | 碎片整理正在运行，监控 CPU 消耗 |
| `active_defrag_cycles` | 缓慢增长 | 快速增长 | 碎片整理周期频繁，可能需要调整阈值 |
| `used_memory_rss` vs 物理内存 | < 70% | > 85% | 扩容或拆分实例 |
| `rdb_last_cow_size` vs 可用物理内存 | < 50% | > 60-70% | 移到 replica 做持久化，或扩容 |
| Hash 平均 field 数 | < 512 | > 1000 | 评估拆分或启用 hashtable |
| 单 value 大小 | < 10KB | > 100KB | 评估压缩或拆分为 Hash |
| listpack/intset 编码占比 | > 60% | < 30% | 调整阈值参数恢复紧凑编码 |
| RDB 保存时内存上涨 | < 20% | > 30% | 延迟保存或调整 save 参数 |
| `blocked_clients` | 0 | > 0 持续 | 检查慢命令（KEYS/SORT/大删除） |

> **active-defrag 配置建议**：当 `mem_fragmentation_ratio` 持续偏高（>1.5）且短期无法重启时，可启用 active-defrag。参数：`activedefrag yes`；关键阈值 `active-defrag-threshold-lower 10`（碎片≥110% 时启动）、`active-defrag-threshold-upper 100`（碎片≥200% 时全力整理）。注意 active-defrag 会消耗 CPU，在高 CPU 利用率的实例上建议先评估影响。

### 12.3 内部编码参数速查

> 以下均为**示例值**（`512`/`128`/`64` 等），可通过 `redis.conf` 或 `CONFIG SET` 修改；**不同 Redis 版本/发行包的默认值可能不同**，生产务必用 `CONFIG GET` 确认当前实例实际值。

```bash
# === String ===
# 无参数（自动切换，不可配置）

# === Hash ===
hash-max-listpack-entries 512   # field 数阈值（示例值，默认依版本而异）
hash-max-listpack-value 64      # 单 field/value 大小阈值（字节）

# === ZSet ===
zset-max-listpack-entries 128
zset-max-listpack-value 64

# === Set ===
set-max-intset-entries 512       # intset → hashtable 阈值
set-max-listpack-entries 128     # listpack → hashtable 阈值
set-max-listpack-value 64

# === List ===
list-max-listpack-size -2        # 每节点最大 8KB（负数=字节，正数=元素数）
list-compress-depth 0            # 压缩深度（0=不压缩）
```

---

## 十三、极简总结

> 以下所有数值均为"典型 64-bit Linux + jemalloc 构建"的经验值。

### 核心认知

- Redis 在运行时会对数据进行**内部编码**（int/embstr/raw、listpack/hashtable、intset/listpack/hashtable、quicklist 等），自动在内存效率和访问性能之间做权衡。许多内存节省来自**"结构级"压缩**（例如将多个属性合并到一个 Hash listpack 中）而不是对单条 value 进行通用压缩。
- 典型 64-bit 环境下：
  - robj ≈ 16B，dictEntry ≈ 24B，SDS header **可变**（短字符串使用更小 header）。这些元数据在小 value 场景下占比较高。
  - embstr 的一次性分配优化（触发阈值约 44B，经验值）能显著减少小字符串的分配次数与碎片。

### 选型速查

| 问题 | 回答 |
|:--|:--|
| **需要转成二进制再存吗？** | 大多数情况不需要。优先利用 Redis 内部编码自动压缩，效果已经很好 |
| **什么时候才需要？** | 大 value（>1KB）+ 低频读取 + 文本/JSON 类有冗余的数据 |
| **最推荐的结构级优化？** | 用 Hash + listpack 编码代替逐条 String SET，内存省 40-50%，不损失原子操作 |
| **如果一定要压缩，选什么算法？** | `zstd (level 1)`，速度 500MB/s 解压 1400MB/s，压缩比 4-5× |
| **压缩后如何识别？** | 在 value 前加 header（magic + algo + version），或并行 metadata key 记录压缩信息 |
| **压缩最大代价？** | 每次读需额外 CPU 解压；压缩破坏 Redis 原子操作能力 |
| **SETEX 退化覆盖的风险？** | 在极端内存压力或实现 bug 场景下，过多 expires 条目、频繁 rehash 与内存分配失败可能引发异常行为；若怀疑存在具体故障，应基于日志、`INFO` output、RDB/AOF snapshot 与复现脚本做定位。常见运维缓解：监控 expires 字典大小、配置合适的 maxmemory + 淘汰策略、使用 `SET key value EX/PEX` 以确保原子性。不建议在无复现/日志证据时断言"静默丢失"。 |
| **整数为什么省内存？** | int 编码绕过 SDS 堆分配（但仍保留 robj 本体），免去 SDS header 与独立 alloc 开销 |
| **listpack 为什么能省 50%？** | 连续内存、零指针、无独立分配，天然抗碎片 |

### 运营注意

| 关注点 | 建议 |
|:--|:--|
| **RDB/AOF 内存峰值** | fork 时 COW 可导致内存接近翻倍，预留 `used_memory_rss × 1.5~2` 物理内存 |
| **编码升级成本** | listpack→hashtable / intset→hashtable 升级会产生内存重分配，高并发时需压测验证 |
| **压缩 value 的元信息** | 务必设计 magic+version+algo 的识别方案，避免多语言/跨版本解压失败 |
| **碎片监控** | `mem_fragmentation_ratio > 2.0` 时考虑 active-defrag 或重启整理 |
| **大 key 删除** | 使用 `UNLINK` 异步删除，避免阻塞主线程 |

### 生产落地 Checklist

- [ ] 在相同硬件上做基线压测（首写 + 覆盖写场景、RDB/AOF 操作期间、编码转换期间）
- [ ] 监控中加入 `OBJECT ENCODING` / `MEMORY USAGE` / `--bigkeys` / `mem_fragmentation_ratio` / `rdb_last_cow_size`
- [ ] 如果使用客户端压缩，完成 value header 方案（magic+version+algo）的跨语言验证
- [ ] 确认淘汰策略（推荐 `allkeys-lru`），避免 `volatile-lru` 在 TTL 丢失场景下的恶性循环
- [ ] 为 RDB 保存期间的内存峰值设置告警（当 `last_cow_size` 接近可用物理内存的 60-70% 时）

---

## 附录：验证 / 排查 / 测量命令与生产测试建议

### A. 推荐验证与排查命令

```bash
# 查看单 key 实际占用（含 metadata）
MEMORY USAGE <key> [SAMPLES <n>]

# 查看实例整体内存统计
INFO memory

# jemalloc 统计（若编译/启用 jemalloc）
INFO malloc-stats        # 视版本而定

# 查看对象编码
OBJECT ENCODING <key>

# 查看对象内部（debug，仅调试用；返回随版本变化且含非稳定字段）
DEBUG OBJECT <key>

# 找大 key
redis-cli --bigkeys

# 监测 listpack/hash 编码分布（批量采样）
# 用 SCAN + OBJECT ENCODING + HLEN 做 sampling 聚合统计
```

### B. 推荐工具

- **redis-memory-for-humans**（社区工具）：可视化分析 key 的内存分布；
- **redis-cli + 自写脚本（Python/Go）**：批量采样 `OBJECT ENCODING` 与 `HLEN`，估算编码分布；
- **云/容器环境**：结合 Prometheus + node_exporter + redis_exporter 采集内存/碎片/slowlog/latency。

### C. 生产测试建议

1. **在相同 allocator/Redis 版本与硬件上做负载压测**：特别测 HSET/HGET/HGETALL 的 p99，确认把 `hash-max-listpack-entries` 从 512 调到 2000 后不会把某些操作延迟拉高到不可接受范围；
2. **自动分片/垂直分片**：重点验证跨 shard 的聚合场景（HGETALL、Lua 脚本、事务等）在 Redis Cluster 下的可行性与成本；考虑用 hash tag 强制同 slot 时注意 key 命名规则；
3. **主动重建策略（策略 F）**：务必实现写入期间的一致性方案，并在灰度窗口测试写入丢失/重复场景；**编码转换的"检测→重建"应作为必须配套**而非可选兜底。

---

## 附录二：ZSet 常用验证命令与重建/分片实务

### A. 常用验证/排查命令

```bash
# 查看 key 编码（返回 listpack / skiplist，视版本）
OBJECT ENCODING <key>

# 查看 key 大小估算（含元数据）
MEMORY USAGE <key> [SAMPLES <n>]

# 集合统计
ZCARD <key>
ZSCORE <key> <member>
ZRANGE <key> 0 -1 WITHSCORES

# 探测大 ZSet / 找大 key（生产慎用全量）
redis-cli --bigkeys
# 或 SCAN + TYPE/ZCARD 聚合（采样限速）

# 实时探测 listpack/skiplist 分布（采样）
# SCAN 全表 + OBJECT ENCODING 统计（做采样限速）
```

### B. 重建实务要点（策略 F）

- 若能一次性用 `ZRANGE WITHSCORES` 拉取全部并在内存中排序，**优先用它**（结果有序，直接批量 ZADD 到 tmpKey）；
- 否则采用 **ZSCAN 分批**拉取，收集后在客户端按 score 排序再批量写入 tmpKey；
- 写入 tmpKey 时用 **pipelining/batching**（例如每次 ZADD 100–1000 个）以减少 RTT；
- 一致性方案：推荐 **"版本号 + Lua 脚本的 CAS RENAME"** 或短暂停写窗口；乐观 CAS 方案易于实现且适合大多数场合。

### C. 分片 key 命名与 Cluster 注意

- 若在 Redis Cluster 下希望把所有分片 key 放在**同一 slot**（例如便于跨分片原子脚本），使用 **hash tag**：如 `{leaderboard}:s0`、`{leaderboard}:s1`——所有 key 的 hash 都由 `{leaderboard}` 部分计算，落在同一 slot；
- 若需分片**分散到不同节点**以分散负载，则**不要使用 hash tag**；但分片后的聚合、全局排序操作代价高。

### D. 压缩 / 按需存储的交叉建议

- 若 member 值很长（如数 KB），评估把 **member id（短字符串或数字）** 放在 ZSet 中，并把**完整数据**放到另外 key（如 HASH 或 string 压缩 blob）——这样 ZSet 仅保存短 id + score，能极大减少 listpack/skiplist 的内存。
