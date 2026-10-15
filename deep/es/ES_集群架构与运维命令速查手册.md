---
title: "ES 集群架构与运维命令速查手册"
summary: "本文档拆分自完整版ES手册，分为三大模块：集群架构高频问题（新增）+ 核心业务高频问题 + 全量运维命令 + 批量运维脚本，命令可直接复制上线使用，适合…"
date: 2026-08-05
author: "小番茄程序猿"
tags:
  - Elasticsearch
category: deep
series: "es"
slug: "ES 集群架构与运维命令速查手册"
reading_time: 24
featured: false
draft: false
---
# ES 集群架构与运维命令速查手册

## 文档说明

本文档拆分自完整版ES手册，分为三大模块：**集群架构高频问题（新增）+ 核心业务高频问题 + 全量运维命令 \+ 批量运维脚本**，命令可直接复制上线使用，适合日常运维随身查阅。

---

# 第一部分：ES集群架构高频问题（新增，大厂必问）

## 1\. ES集群有哪些节点角色？各自作用是什么？生产如何规划节点？

### 标准答案

ES7\.0之后默认**仅分为主节点、数据节点**，同时支持专用冷热节点、协调节点、 ingest预处理节点，六大角色分工明确，生产必须做节点角色隔离，禁止单节点混用所有角色。

1. **主节点（Master Node）**

    - 核心职责：集群全局管理，维护集群元数据（索引、分片、节点信息）、分片分配与搬迁、集群状态变更、ILM策略调度

    - 不负责读写数据、不存储分片，压力极小

    - 生产配置：3个专用主节点（奇数），只投票不存数据，避免元数据压力过大

2. **数据节点（Data Node）**

    - 核心职责：存储主/副本分片，承接文档读写、聚合、查询、段合并等所有数据IO操作

    - 集群压力核心，CPU、内存、磁盘开销最大

3. **协调节点（Coordinating Node）**

    - 无投票权、不存分片，只负责请求转发、结果聚合、路由分发

    - 接收客户端请求，拆分请求下发到对应分片，合并多分片返回结果后应答客户端

    - 高并发生产建议单独部署，避免数据节点被查询聚合打满影响写入

4. **Ingest预处理节点**：写入前通过管道脚本预处理数据（字段清洗、格式转换），不建议和热数据节点混用

5. **冷热分层专用节点（Hot/Warm/Cold）**：通过node\.attr打标签，配合ILM实现分片自动迁移，SSD热节点、普通磁盘温节点、低成本HDD冷节点，分层降本

### 生产最佳节点规划

3台专用主节点 \+ 多台冷热数据节点 \+ 2台协调节点，彻底解耦管理、存储、请求转发压力。

## 2\. 什么是集群脑裂？如何产生？生产怎么解决？

### 标准答案

**脑裂定义**：网络波动、节点卡顿导致集群分裂成两个独立子集群，各自选举主节点，双向独立工作，索引分片数据不一致，最终集群彻底混乱、数据丢失。

#### 产生原因

1. 网络抖动、防火墙限流，节点之间心跳超时失联

2. 单节点CPU飙高、GC过长，无法响应集群心跳

3. 主节点数量配置不合理，偶数主节点极易触发脑裂

#### 生产解决方案（ES7默认优化\+人工配置兜底）

1. **必选：主节点数量配置为奇数（3/5台），禁止2台主节点**

2. 配置最小主节点投票数：`discovery.zen.minimum_master_nodes`（ES7自动适配，低版本必须手动配置，公式：主节点数/2 \+1）

3. 调大心跳超时参数，规避短暂网络抖动误判失联

4. 集群内网专用网络，杜绝公网跨机房通信

## 3\. ES集群通信机制是什么？客户端请求完整执行流程？

### 集群内部通信

- 节点间：基于TCP的Transport通信，端口9300，用于心跳检测、分片同步、集群状态同步、分片搬迁

- 客户端与集群：HTTP协议，端口9200，接收业务读写、运维API请求

### 客户端写入一条数据完整流程（高频）

1. 客户端发起写入请求，连接协调节点

2. 协调节点通过murmurhash3路由算法，计算文档归属主分片

3. 请求转发至对应主分片所在节点，主分片执行写入，写入成功后同步至所有副本分片

4. 主副分片全部写入完成，返回ack应答给协调节点，最终响应客户端

**核心关键点**：写入必须主分片先落盘，同步副本成功才算写入成功；查询可以走主分片也可以走副本分片，分担查询压力。

## 4\. ES集群高可用如何保证？主副本分片设计原理？

### 标准答案

1. **分片高可用约束**：同一个分片的主分片和副本分片，**绝对不能部署在同一台物理节点**，单机宕机不会丢失分片数据

2. **主分片故障自愈**：主分片所在节点宕机，集群自动从现有副本分片选举新主分片，集群不中断读写

3. **副本自动同步**：主分片所有写入操作实时同步副本，保证主副数据一致性

4. **集群自愈能力**：节点恢复后，自动补齐缺失副本，集群分片自动重新均衡

### 追问：副本数可以大于节点数吗？

不可以。副本数不能超过集群可用数据节点数，否则副本无法分配，集群直接变黄。比如3个数据节点，副本数最大只能设置为2。

## 5\. 集群变红、变黄分别代表什么？常见原因与修复方案？

- **集群绿色（Green）**：所有主分片、副本分片全部正常分配，集群完全健康

- **集群黄色（Yellow）**：所有主分片正常，部分副本分片未分配。**业务读写不受影响，无数据丢失风险**
常见原因：节点下线、磁盘水位过高、冷热标签不匹配、节点资源不足
        

- **集群红色（Red）**：存在主分片未分配，**对应索引读写全部异常，业务报错，存在数据丢失风险**
常见原因：多节点宕机、主分片所在节点彻底损坏、磁盘打满、索引文件损坏
        

### 快速排查命令

`GET _cluster/allocation/explain` 一键定位未分配分片根因

## 6\. 跨机房部署ES集群可行吗？有什么痛点？生产建议？

### 标准答案

**不建议跨机房部署ES集群**，尽量单机房集群，多机房做快照异地备份即可。

#### 核心痛点

1. 机房网络延迟高，节点间心跳超时极易触发脑裂

2. 分片跨机房同步延迟大，主副分片同步缓慢，写入性能暴跌

3. 分片搬迁跨机房带宽开销极大，IO和网络双重压力

#### 异地容灾正确方案

单机房部署主集群，定时快照同步至异地对象存储，异地搭建备用集群定时恢复快照，而非直接组建跨机房ES集群。

## 7\. ES集群扩容怎么做？横向扩容节点后分片会自动均衡吗？

### 标准答案

1. **横向扩容流程**：新节点配置相同集群名、相同secret密钥，启动后自动加入集群

2. **分片均衡机制**：默认自动rebalance，新节点加入后，集群会自动将原有分片搬迁至新节点，均衡节点分片数量

3. **生产避坑**：业务高峰期新增节点，必须调低分片搬迁并发和带宽，防止分片搬迁打满集群IO，引发业务雪崩；低峰期再放开均衡策略

## 8\. ES7相较于ES6，集群架构核心改动有哪些？

1. 彻底移除**Type类型**，一个索引只能有一种文档结构

2. 默认主分片数从5改为1，适配现在大磁盘单机架构

3. 废弃手动配置最小主节点数，集群自动计算防止脑裂

4. 原生内置ILM冷热生命周期、SLM快照生命周期，无需第三方插件

5. 引入data\_tier冷热节点分层架构，官方原生支持冷热分离

---

# 第二部分：核心业务高频问题

## 1、分片路由相关

### 1\.1 ES 文档路由分片原理？

文档路由公式：

```java
shardId = abs(MurmurHash3(routingKey)) % number_of_primary_shards
```

1. routingKey 优先使用自定义 routing，无则使用文档 \_id；

2. 主分片数量创建后**永久不可修改**，修改会导致所有文档路由错乱查不到数据；

3. 同一个 routingKey 永远落在同一个分片；

4. 哈希碰撞正常，分片内依靠 \_id 区分文档，不会覆盖数据。

### 1\.2 什么是分片倾斜？两种倾斜怎么解决？

1. **路由数据倾斜（业务层面）**
原因：以省份、日期、大客户ID等热点维度做 routing，大量数据扎堆单个分片。
            
解决方案：路由key增加随机后缀打散 `key_0 / key_1 / key_2`，查询遍历全部后缀路由检索；严重倾斜只能 Reindex 重建索引打散。
        

2. **节点分片数量倾斜（集群层面）**
原因：分片搬迁限流、均衡阈值过大、磁盘水位限制。
解决方案：低峰调大分片搬迁并发，调整均衡阈值，清理磁盘空间等待自动 rebalance。
        

### 1\.3 单分片合理容量规划原则？

常规业务：**35\~50GB**

热写入高频查询：20\~35GB

冷归档少查询：上限 60\~80GB

分片过大弊端：迁移、段合并、故障恢复、快照恢复极慢，故障风险高。

主分片预估公式：未来3年总数据量 ÷ 50GB，原则宁多勿少。

## 2、分片分配与磁盘水位

### 2\.1 ES 分片均衡打分逻辑？

综合得分 = index\_balance差值\(权重0\.55\) \+ shard\_balance差值\(权重0\.45\)

得分越低节点越空闲，优先分配分片；默认阈值1\.0，偏差超阈值才触发自动均衡。

### 2\.2 磁盘三级水位作用？

- low 85%：主动迁出分片释放空间

- high 90%：禁止新分片迁入

- flood\_stage 95%：节点所有索引只读，防止磁盘打满集群宕机

## 3、ILM 冷热分层（重中之重）

### 3\.1 ILM 流转特点？

1. 单向不可逆：`Hot → Warm → Cold → Delete`，原生无自动回流；

2. 最小单位是整个索引，不能单条文档冷热迁移；

3. 计时起点：普通索引=创建时间，Rollover索引=滚动生成时间，**不识别业务create\_time字段**；

4. 后台轮询执行（默认10分钟），不是定时Cron任务；

5. shrink、forcemerge、readonly、freeze 部分动作不可逆。

### 3\.2 温/冷层级能不能回退热层？通用操作是什么？

可以手动回退，**所有层级向上回退操作逻辑完全通用**：

1. 修改索引 `index.routing.allocation.include._tier_preference` 为上层标签；

2. ES 自动触发分片搬迁回对应节点池；

3. 暂停该索引 ILM，防止下一轮巡检再次自动下沉；

4. shrink 缩分片只能挪节点，分片数量无法自动复原，恢复分片只能 Reindex。

### 3\.3 ILM Delete 删除索引后还能恢复吗？快照需要手动配置吗？

1. ILM delete 是物理删除索引文件，**无内置回收站、无自动回退机制**；

2. 仅提前打过快照才能恢复，无备份数据永久丢失；

3. **ES 自建集群默认没有任何自动快照**：快照仓库、SLM定时自动备份、ILM删除前置快照全部需要手动创建配置；

4. 云厂商ES自带快照，是厂商提前配置完成，并非ES原生自带能力。

### 3\.4 ILM 删除防丢两种方案？

1. SLM + wait_for_snapshot（ES 7.6+）：ILM delete 阶段挂 `wait_for_snapshot` 动作，先等待指定 SLM 策略产出包含该索引的快照，再执行删除，到期等不到快照则阶段失败、索引保留；

2. 独立SLM全局定时快照：不侵入ILM，按天全量备份，兜底误删、误改、磁盘损坏，生产更推荐。

⚠️ 注意：ES 任何版本都没有 ILM 的 `snapshot` action，"delete 阶段前置 snapshot 配置"照抄必报 `unsupported action [snapshot]`，防丢一律走上面两种正规方案。

## 4、Reindex 重构相关

### 4\.1 哪些场景必须用 Reindex，不能原地修改？

1. 修改已有字段类型、字段重命名、修改分词器；

2. 修改主分片数量；

3. 修改自定义 routing 规则、打散热点分片。

### 4\.2 零停机 Reindex 完整流程？

新建索引 → 全量Reindex迁移 → 业务双写追增量 → 数据对齐 → 别名原子切换 → 观察稳定下线旧索引。

## 5、别名 \& Rollover 滚动索引

### 5\.1 索引别名设计好处？

1. 业务只访问别名，底层索引迭代、重构不用改代码、不用重启服务；

2. 切换、回滚只需要一条别名API，风险极低；

3. Rollover 依靠写入别名实现时序索引自动拆分。

### 5\.2 Rollover 原理与适用场景？

设置带 `is_write_index:true` 的写入别名，达到容量/时长阈值自动生成新索引，旧索引只读进入ILM生命周期；适合日志、流水、时序订单无限写入场景。

## 6、跨中间件通用分片思想（加分项）

ES、MySQL分库分表、Redis槽位、Kafka分区底层逻辑一致：

1. 分片键哈希取模绑定固定分片；

2. 热点字段做分片键必然数据倾斜；

3. 分片前期宁多勿少，后期重构成本极高；

4. 优先使用中间件原生生命周期\+均衡能力，不要自研分片逻辑。

## 7、3 分钟要点总结（含集群架构）

ES集群分为专用主节点、数据节点、协调节点，生产采用3主\+冷热数据节点架构规避脑裂；集群依靠TCP内网通信，主副本分片物理隔离保证高可用，节点宕机可自动选举新主分片实现自愈。集群红黄状态分别对应主分片异常、副本分片异常，扩容节点后分片会自动均衡，高峰期需要限流避免IO雪崩。

ES 通过 MurmurHash3 哈希取模实现文档路由，主分片不可修改，自定义路由容易产生分片倾斜，解决方案是路由后缀打散。集群依靠双层加权算法做分片均衡，受磁盘水位、主副隔离、冷热节点三层约束，单分片控制在30\~50GB，前期分片宁多勿少。

ILM 冷热流转单向 Hot→Warm→Cold→Delete，轮询执行不识别业务时间，只能节约存储不能解决热点倾斜；层级可以手动向上回退，修改 data\_tier 加暂停 ILM 即可，操作全层级通用；索引删除不可逆，ES无默认快照，快照仓库、SLM备份都需要手动部署。

改字段、改分片、改路由都只能 Reindex 零停机重构，配合别名无感切换；时序业务用 Rollover 自动拆索引配合 ILM 全生命周期管理。整套分片思想通用各类分布式中间件，生产优先原生能力，避免自研分片架构。

---

# 第三部分：全场景运维命令合集（可直接复制执行）

## 1、集群健康 \& 分片排查（新增集群专属排查命令）

```http
# 集群整体健康状态
GET _cluster/health?v

# 查看所有节点角色、内存、磁盘、堆内存
GET _cat/nodes?v&h=name,node.role,disk.used_percent,heap.current,heap.max

# 分片明细查看
GET _cat/shards/索引名?v

# 未分配分片根因定位（集群红黄必用）
GET _cluster/allocation/explain

# 分片搬迁进度查看（冷热迁移、恢复、reindex迁移）
GET _cat/recovery?v&h=index,shard,percent,source_node,target_node,stage

```

## 2、分片均衡 \& 搬迁限流配置

```http
# 分片搬迁限流（生产常用）
PUT _cluster/settings
{
  "transient": {
    "cluster.routing.allocation.node_concurrent_incoming_recoveries": 2,
    "cluster.routing.allocation.node_concurrent_outgoing_recoveries": 2,
    "cluster.routing.allocation.cluster_concurrent_rebalance": 2,
    "indices.recovery.max_bytes_per_sec": "40mb"
  }
}

# 高峰期临时关闭自动均衡
PUT _cluster/settings
{"transient":{"cluster.routing.allocation.allow_rebalance":"none"}}

# 恢复均衡
PUT _cluster/settings
{"transient":{"cluster.routing.allocation.allow_rebalance":"indices_all_active"}}

```

## 3、ILM 生命周期运维命令

```http
# 查看索引ILM详情、卡住原因
GET 索引名/_ilm/explain

# 单索引启停ILM
POST 索引名/_ilm/stop
POST 索引名/_ilm/start

# 全局启停ILM
POST _ilm/stop
POST _ilm/start

# 修改ILM全局巡检间隔
PUT _cluster/settings
{
  "transient": {
    "indices.lifecycle.poll_interval": "5m"
  }
}

# 查看索引当前冷热层级
GET 索引名/_settings?filter_path=*.settings.index.routing.allocation.include._tier_preference

```

## 4、ILM 冷热层级向上回退全套命令

### Warm → Hot

```http
PUT 索引名/_settings
{
  "index.routing.allocation.include._tier_preference": "data_hot"
}
POST 索引名/_ilm/stop

```

### Cold → Warm

```http
PUT 索引名/_settings
{
  "index.routing.allocation.include._tier_preference": "data_warm"
}
POST 索引名/_ilm/stop

```

### Cold → Hot 跨层回退

```http
PUT 索引名/_settings
{
  "index.routing.allocation.include._tier_preference": "data_hot"
}
POST 索引名/_ilm/stop

```

### 回流配套解锁命令

```http
# 解除索引只读
PUT 索引名/_settings
{"index.blocks.write": null}

# 关闭索引冻结
PUT 索引名/_settings
{"index.frozen": false}

# 恢复ILM自动流转
POST 索引名/_ilm/start

```

## 5、索引别名管理

```http
# 原子切换别名（重构切流/回滚）
POST _aliases
{
  "actions": [
    {"remove": {"index": "order_v1", "alias": "alias_order"}},
    {"add": {"index": "order_v2", "alias": "alias_order"}}
  ]
}

# 查看别名绑定关系
GET _alias/alias_order

```

## 6、Rollover 滚动索引运维

```http
# 手动触发滚动测试
POST alias_order_roll/_rollover

# 查看写入索引状态
GET _alias/alias_order_roll

```

## 7、Reindex 数据迁移常用

```http
# 基础全量迁移
POST _reindex
{
  "source": {"index": "旧索引"},
  "dest": {"index": "新索引"}
}

# 字段转换Reindex示例
POST _reindex
{
  "source": {"index": "order_v1"},
  "dest": {"index": "order_v2"},
  "script": {
    "source": "ctx._source.orderId = ctx._source.remove(\"o_id\")"
  }
}

# 异步执行大索引迁移
POST _reindex?wait_for_completion=false

```

## 8、快照备份 \& 恢复（防ILM误删）

```http
# 1、创建快照仓库
PUT _snapshot/es_backup_repo
{
  "type": "fs",
  "settings": {
    "location": "/data/es_snapshot",
    "compress": true
  }
}

# 2、查看所有快照
GET _snapshot/es_backup_repo/_all

# 3、从快照恢复被删除索引（自动加前缀避免重名）
POST _snapshot/es_backup_repo/ilm-del-snap-order-000001/_restore
{
  "indices": "order-000001",
  "include_global_state": false,
  "rename_pattern": "(.+)",
  "rename_replacement": "restored_$1"
}

```

## 9、SLM 自动定时快照策略

```http
PUT _slm/policy/es_daily_snap_policy
{
  "schedule": "0 0 2 * * ?",
  "repository": "es_backup_repo",
  "name": "daily-snap-{now/d}",
  "config": {
    "indices": "order-*",
    "include_global_state": false
  },
  "retention": {
    "expire_after": "30d",
    "min_count": 7
  }
}

```

## 10、ILM 删除阶段前置快照配置片段（ES 7.6+：wait_for_snapshot）

⚠️ ES 没有 ILM `snapshot` action，正确写法是 delete 阶段挂 `wait_for_snapshot`：先等 SLM 策略产出包含该索引的快照，再执行删除。

```json
"delete": {
  "min_age": "90d",
  "actions": {
    "wait_for_snapshot": {
      "policy": "es_daily_snap_policy"
    },
    "delete": {}
  }
}
```

依赖前置：`es_daily_snap_policy` 为已创建的 SLM 策略（`PUT _slm/policy/es_daily_snap_policy`，ES 7.4+）。低于 7.6 的环境改用外部调度脚本在到期前手动打快照。

---

# 第四部分：批量运维 Shell 脚本

## 脚本1：批量所有 Warm 索引回迁 Hot（warm\_to\_hot\.sh）

```bash
#!/bin/bash
ES_HOST="http://127.0.0.1:9200"
# ES_USER="elastic"
# ES_PWD="xxxxxx"

echo "========== ES温层索引批量回迁热节点 开始执行 =========="
INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.include._tier_preference?pretty" | jq -r 'to_entries[] | select((.value.settings.index.routing.allocation.include._tier_preference // "") | contains("data_warm")) | select(.key | startswith(".") | not) | .key')

if [ -z "$INDEX_LIST" ];then
  echo "无warm索引需要回迁"
  exit 0
fi

echo "$INDEX_LIST"
while read index; do
[ -z "$index" ] && continue
JSON_BODY='{"index.routing.allocation.include._tier_preference":"data_hot"}'
if [ -n "$ES_USER" ];then
  curl -s -u $ES_USER:$ES_PWD -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  curl -s -u $ES_USER:$ES_PWD -X POST "$ES_HOST/$index/_ilm/stop"
else
  curl -s -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  curl -s -X POST "$ES_HOST/$index/_ilm/stop"
fi
echo "$index 已设置hot并暂停ILM"
done <<< "$INDEX_LIST"

echo "执行完成，查看迁移进度：GET _cat/recovery?v"

```

### 使用方式

```bash
yum install jq -y
chmod +x warm_to_hot.sh
./warm_to_hot.sh

```

如需批量 Cold→Warm / Cold→Hot，修改脚本内 jq 过滤 `contains("data_cold")`，同步修改目标 tier 即可复用。

