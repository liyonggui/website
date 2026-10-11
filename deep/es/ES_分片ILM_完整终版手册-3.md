---
title: "Elasticsearch 路由+分片分配+ILM冷热分层+Reindex重构+别名架构+Rollover时序索引+ILM反向回流 完整版完整文档"
summary: "（生产即用）"
date: 2026-07-27
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 索引
  - 分库分表
  - ILM
category: deep
series: "es"
slug: "ES_分片ILM_完整终版手册-3"
reading_time: 54
featured: false
draft: false
---
# Elasticsearch 路由+分片分配+ILM冷热分层+Reindex重构+别名架构+Rollover时序索引+ILM反向回流 完整版完整文档

（生产即用）

## 目录

1. 索引/分片/节点层级基础关系
2. 路由寻址原理 & MurmurHash3 哈希机制
3. 两类分片倾斜成因与路由二次打散方案
4. 分片分配算法、磁盘水位、搬迁管控
5. 索引&节点容量生产规划标准
6. ILM 冷热分层完整手册（原理、参数对照表、轮询vs定时任务、故障排查、运维命令）
7. Reindex 数据重构全场景方案
   7.1 修改字段名/字段类型迁移方案
   7.2 重新分片、修改路由规则、全局打散热点方案
8. 索引别名架构设计 + 普通索引上线初始化标准模板
9. Rollover 时序滚动索引 完整初始化模板（日志/流水专用）
10. ILM反向回流：温/冷层级向上回退通用方案+一键命令速查表+批量脚本
11. 全局分片异常统一排障手册
12. 跨中间件统一分片思想（ES / MySQL / Redis / Kafka）
13. 三分钟整体总结

---

# 一、索引 / 分片 / 节点 层级关系

## 层级结构

集群 Cluster → 多个索引 Index → 索引包含主分片+副本分片 → 分片部署在节点 Node → 节点依托服务器磁盘、内存、CPU资源

## 两条核心铁律

1. **一个分片永久只归属一个索引**，不能跨索引存储数据；不同索引分片可同名 shard-0，物理文件完全隔离。
2. **同一个分片的主分片、副本分片禁止部署在同一节点**，防止单机宕机同时丢失主副副本，失去高可用。

## 示例：3节点，order索引 9主1副本（总分片18）

总分片计算公式：
总分片 = 主分片数 × (副本数 + 1) = 9 × (1+1) = 18
理论均分：3节点，每个节点承载该索引6个分片

```
NodeA
├─ order 索引6个分片
├─ goods索引若干分片（多索引分片共存正常）

NodeB
├─ order 索引6个分片
├─ goods索引若干分片

NodeC
├─ order 索引6个分片
├─ goods索引若干分片
```

- 节点：存储容器，可容纳任意索引的分片
- 分片：索引私有存储单元，分片归属创建后固定，不可跨索引复用

---

# 二、路由寻址底层原理 & MurmurHash3

## 1、路由计算公式（底层固定逻辑）

```java
String routingKey = 自定义routing != null ? 自定义routing : 文档_id;
int hash = Murmur3HashFunction.hash(routingKey);
int shardId = Math.abs(hash) % number_of_primary_shards;
```

### 硬性约束

1. `number_of_primary_shards` **索引创建后永久不可修改**，一旦修改，全部文档路由计算错乱，数据无法查询。
2. 副本数量 `number_of_replicas` 可动态调整，完全不影响路由逻辑。
3. 同一个 routingKey，运算得到的分片ID终身固定，文档永远落在同一个分片。

## 2、MurmurHash3 两个版本分工

| 版本                  | 位数   | 使用场景              | 说明                       |
| ------------------- | ---- | ----------------- | ------------------------ |
| MurmurHash3_x86_32  | 32位  | 分片路由计算            | 打散有序ID；存在少量哈希碰撞，碰撞不会覆盖数据 |
| MurmurHash3_x64_128 | 128位 | cardinality基数去重聚合 | 极低碰撞概率，保证去重统计结果精准        |

### 哈希碰撞说明

不同 routingKey 算出相同哈希值、进入同一个分片属于正常现象；
分片内部依靠文档 `_id` 区分不同文档，**不会发生数据覆盖、更新错乱**；哈希设计目的是打散有序数据，不是彻底消除碰撞。

## 3、Routing 字符串长度规范

1. 不是越长打散效果越好：哈希结果固定32位，超长字符串只会增加CPU计算开销，还容易触发URL超长报错。
2. 不是越短越好：字符过少样本空间小，极易出现哈希扎堆、数据倾斜。
3. 生产最优长度：**6~30个字符**。

## 4、路由打散与MySQL兼容性

路由二次打散仅修改文档 `_routing` 字段，文档 `_id`、业务主键保持不变，ES与MySQL可以依靠唯一主键正常做数据关联对账。

---

# 三、两类分片倾斜成因 + 路由二次打散解决方案

## 类型1：路由数据倾斜（业务层面，分片均衡算法无法解决）

### 成因

为减少跨分片聚合查询开销，业务使用**省份、日期、商户ID、大客户ID**作为自定义 routing；热点Key长期落在单个分片，导致分片之间数据体量差距巨大。

### 典型场景

- 按日期路由：当日写入分片数据爆满，历史日期分片长期闲置。
- 按省份路由：广东、浙江等人口大省分片体积远大于其他省份分片。

### 根治方案：路由二次后缀打散

思路：`原业务key_随机后缀`，单个热点路由拆分多路，分散至多个分片
示例：
原始路由：`guangdong` → 固定单个分片
打散路由：`guangdong_0`、`guangdong_1`、`guangdong_2`，分散到多个分片，均衡数据压力

### Java 工具类

```java
import java.util.ArrayList;
import java.util.List;

public class RoutingSplitUtil {
    // 写入生成打散路由
    public static String getSplitRouting(String bizKey, int splitNum, Long snowId) {
        if (splitNum <= 1) return bizKey;
        int suffix = Math.abs(snowId.intValue()) % splitNum;
        return bizKey + "_" + suffix;
    }
    // 查询遍历全部路由批量检索
    public static List<String> listAllRouting(String bizKey, int splitNum) {
        List<String> list = new ArrayList<>();
        for (int i = 0; i < splitNum; i++) {
            list.add(bizKey + "_" + i);
        }
        return list;
    }
}
```

### 扩容兜底方案

打散配置份数仍无法缓解倾斜：主分片不能原地修改，新建更高分片数索引，`reindex` 迁移存量数据，业务双写切换别名完成重构。

## 类型2：节点分片分配倾斜（集群层面，ES均衡算法自动调节）

现象：集群节点总分片数量差距过大，例：3节点理论各6分片，实际节点A=10、节点B=5、节点C=3。
诱因：分片搬迁限流保守、均衡阈值设置偏大、节点磁盘水位触发分配限制。

---

# 四、分片节点分配算法完整解析

## 1、分配整体流程

前置过滤校验 → 候选节点打分计算 → 择优分配/搬迁分片

### 前置过滤规则（优先级最高，优先执行）

1. 同分片主、副本不能部署同一节点
2. 磁盘水位超限节点禁止迁入分片
3. 冷热层级节点角色匹配校验（hot/warm/cold节点隔离）
4. 节点离线、故障、单节点分片总数超限直接剔除

## 2、加权打分均衡算法

公式：
`综合得分 = index_balance差值 × 0.55 + shard_balance差值 × 0.45`
**得分越低，节点越空闲，优先分配分片**

1. **IndexBalance（权重0.55）**：控制同一个索引分片在节点均匀分布，避免单节点塞满某一个索引大量分片
2. **ShardBalance（权重0.45）**：控制集群所有索引总分片整体均衡分布

均衡阈值 `cluster.routing.allocation.balance.threshold` 默认 1.0，分片数量偏差超出阈值才触发 rebalance；调小均衡更积极、IO开销更多，调大收敛惰性更强。

## 3、磁盘三级水位（分配强门槛）

- low(85%)：节点磁盘使用率达到阈值，开始主动迁出分片释放空间
- high(90%)：禁止新分片迁入该节点
- flood_stage(95%)：节点所有索引进入只读模式，防止磁盘打满引发集群宕机

## 4、分片搬迁并发配置（生产常用，防IO雪崩）

```http
PUT _cluster/settings
{
  "transient": {
    "cluster.routing.allocation.node_concurrent_incoming_recoveries": 2,
    "cluster.routing.allocation.node_concurrent_outgoing_recoveries": 2,
    "cluster.routing.allocation.cluster_concurrent_rebalance": 2,
    "indices.recovery.max_bytes_per_sec": "40mb"
  }
}
```

业务高峰期调低并发与带宽，避免IO打满；低峰期调高参数，加速分片均衡收敛。

## 5、主副分片补充规则

1. 节点宕机后，ES自动从可用副本中选举新主分片，保证写入可用性
2. 多副本自动打散到不同节点、不同机架（开启机架感知配置后）
3. 副本分片跟随主分片同步流转冷热层级

---

# 五、索引&节点容量规划规范（生产落地标准）

## 1、单分片合理容量（核心规划依据）

- 热数据高写入高频查询：**20~35GB**

- 常规业务索引通用最优区间：**35~50GB**

- 冷归档极少查询索引：上限放宽至 60~80GB
  
  > 分片过大弊端：分片迁移、段合并、故障恢复、快照恢复速度极慢，故障风险大幅提升

## 2、主分片数量计算公式

`建议主分片数 ≈ 预估未来3年总数据量 ÷ 50GB`
原则：**分片宁多勿少**，主分片创建后不可修改，前期预留余量，避免后期必须重构迁移。

## 3、副本配置说明

示例：9主 + 副本数=1 → 9主9副本，总分片18，一一对应配对；副本数量可随时动态调整，无需重建索引。

## 4、节点磁盘使用率红线（冷热区分）

- hot热节点(SSD)：使用率 ≤75%
- warm温节点：使用率 ≤80%
- cold冷节点(HDD)：使用率 ≤82%
  预留空间应对分片搬迁、段合并、数据突增，规避磁盘水位触发分配异常。

## 5、单节点分片总数上限

保守阈值：单节点总分片 ≤ 800
警戒上限：≤ 1000
分片数量过多，元数据占用堆内存暴涨，集群管控压力激增，心跳、分配效率下降。

## 6、单个索引总体积上限

中小集群(3~5节点)：单索引总容量 ≤1.5TB
中大型集群：单索引 ≤3TB
时序数据禁止单索引无限膨胀，采用 Rollover+ILM 自动拆分索引治理。

---

# 六、ILM 冷热分层完整手册

## 6.1 底层核心原理

1. **流转最小单位：整个索引，不支持单条文档单独冷热迁移**

2. 计时基准（高频误区）
   
   - 普通静态索引：计时起点 = 索引创建时间，与文档 `create_time/update_time` 业务字段无关
   
   - Rollover滚动索引：计时起点 = 索引滚动生成切换时刻
     
     > ILM 不会读取文档内部业务时间字段做冷热判断

3. 调度模式
   内置 `ilm-service` 后台轮询线程，**并非 Cron 定时任务**，默认巡检间隔10分钟，配置项：`indices.lifecycle.poll_interval`
   每轮遍历绑定策略的索引，上一阶段动作未完成则跳过本轮，不会重复执行。

4. 流转单向不可逆：`Hot → Warm → Cold → Delete`，ES无原生自动回流机制

5. 流转本质：修改索引 `data_tier` 分配标签约束，分片分配器自动将分片搬迁至对应节点池

## 6.2 ILM轮询 vs 自定义定时任务 对比

结论：ILM轮询稳定性、容错、集群可控性全面优于自研Cron定时任务

1. 负载：轮询分散触发，无瞬时IO尖峰；Cron整点批量搬迁极易打满磁盘IO
2. 幂等：ILM内置状态机，阶段未完成不会向下流转；自研需要自行实现幂等、重试、状态存储逻辑
3. 资源管控：复用集群原生分片搬迁限流配置；定时任务需要额外开发限流逻辑
4. 异常自愈：迁移失败下一轮自动重试；定时任务失败需要人工排查补偿数据
5. 资源开销：轮询仅遍历索引元数据，空闲状态开销极低；定时任务常驻调度，维护成本更高
6. 唯一短板：存在最多一个轮询周期时间误差；Cron可以做到精准整点执行

补充：修改 `data_tier` 标签是流转执行动作，两种调度修改标签本身开销基本一致，差距核心在调度架构设计。

## 6.3 ILM 四阶段参数速查表（含义+默认+修改场景+推荐值）

### 全局ILM配置

| 参数                              | 作用        | 默认  | 修改场景            | 推荐值                      |
| ------------------------------- | --------- | --- | --------------- | ------------------------ |
| indices.lifecycle.poll_interval | ILM全局巡检间隔 | 10m | 测试验证、时效敏感、大集群减负 | 测试：1m；时效：3~5m；大集群：15~20m |

### Hot 热阶段

| 参数                              | 作用           | 默认     | 修改场景                 | 推荐值                    |
| ------------------------------- | ------------ | ------ | -------------------- | ---------------------- |
| min_age                         | 进入热阶段等待时长    | 0ms    | 一般不修改                | 固定0ms                  |
| rollover.max_primary_shard_size | 单主分片容量阈值自动滚动 | 45gb   | 写入量偏大/偏小，规范分片30~50GB | 高吞吐：30~35gb；常规：40~50gb |
| rollover.max_age                | 索引最长写入周期强制滚动 | 7d     | 调整索引拆分粒度、磁盘压力调节      | 按天拆分：3~7d；低流量：10~15d   |
| rollover.max_docs               | 文档条数触发滚动     | 默认注释关闭 | 仅文档大小高度规整时序数据启用      | 按需配置，常规业务禁用            |
| set_priority                    | 分片故障恢复优先级    | 100    | 核心业务索引优先恢复           | 普通100，核心110~120        |

> 非时序静态索引可直接删除整个rollover配置节点

### Warm 温阶段

| 参数                          | 作用            | 默认   | 修改场景               | 推荐值                  |
| --------------------------- | ------------- | ---- | ------------------ | -------------------- |
| min_age                     | 滚动生成后多久转入温层   | 7d   | 活跃查询周期调整、提前释放热节点资源 | 访问久：10~15d；快速归档：3~5d |
| allocate.require.data_tier  | 强制分片分配到warm节点 | warm | 整体下线冷热分层架构         | 注释allocate节点即可取消迁移   |
| forcemerge.max_num_segments | 段合并优化查询，IO消耗高 | 1    | 温层访问频次、高峰期防IO抖动    | 有查询保留1；几乎无查询可关闭      |
| shrink.number_of_shards     | 收缩主分片数量（不可逆）  | 3    | 集群分片过多内存紧张、仍有聚合查询  | 有聚合：3~5片；极少查询可缩至1片   |
| readonly                    | 索引自动设置只读      | 开启   | 业务存在滞后补发写入场景       | 有迟写需求则移除readonly动作   |
| set_priority                | 分片恢复优先级       | 50   | 重要历史订单提升恢复优先级      | 微调至60~70             |

### Cold 冷阶段

| 参数                         | 作用              | 默认   | 修改场景              | 推荐值                    |
| -------------------------- | --------------- | ---- | ----------------- | ---------------------- |
| min_age                    | 多久转入冷归档节点       | 30d  | 存储成本敏感提前归档、频繁溯源延后 | 省钱归档：15~20d；经常查：45~60d |
| allocate.require.data_tier | 强制分片分配到cold节点   | cold | 下线分层架构            | 注释allocate配置           |
| freeze                     | 索引冻结降低内存占用，查询变慢 | 开启   | 偶尔检索排查、集群内存紧张     | 几乎不查开启；经常查询关闭冻结        |
| set_priority               | 分片恢复优先级         | 10   | 归档数据降低资源抢占        | 调整为5~8                 |

### Delete 删除阶段

| 参数       | 作用          | 默认  | 修改场景             | 推荐值                                      |
| -------- | ----------- | --- | ---------------- | ---------------------------------------- |
| min_age  | 索引留存时长后自动删除 | 90d | 合规留存、磁盘紧急清理、永久归档 | 短期日志：60~90d；合规：180d~3年；永久保存：删除delete整个节点 |
| delete动作 | 物理删除索引，不可恢复 | 开启  | 核心数据防误删          | 拉长留存周期+开启快照备份，不随意关闭                      |

## 6.4 可直接上线ILM策略+索引模板

### 1）创建ILM策略

```json
PUT _ilm/policy/order_hot_warm_cold_policy
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {
            "max_age": "7d",
            "max_primary_shard_size": "45gb"
          },
          "set_priority": {"priority": 100}
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "allocate": {"require":{"data_tier":"warm"}},
          "forcemerge": {"max_num_segments": 1},
          "shrink": {"number_of_shards": 3},
          "set_priority": {"priority": 50}
        }
      },
      "cold": {
        "min_age": "30d",
        "actions": {
          "allocate": {"require":{"data_tier":"cold"}},
          "freeze": {},
          "set_priority": {"priority": 10}
        }
      },
      "delete": {
        "min_age": "90d",
        "actions": {"delete": {}}
      }
    }
  }
}
```

### 2）通用索引模板绑定ILM

```json
PUT _index_template/template_order
{
  "index_patterns": ["order_*"],
  "template": {
    "settings": {
      "number_of_shards": 9,
      "number_of_replicas": 1,
      "index.lifecycle.name": "order_hot_warm_cold_policy",
      "index.routing.allocation.data_tier": "hot"
    },
    "mappings": {
      "properties": {
        "orderId": {"type": "keyword"},
        "province": {"type": "keyword"},
        "createTime": {"type": "date"}
      }
    }
  },
  "priority": 10
}
```

## 6.5 ILM生产八大注意事项

1. 冷热分层仅优化存储成本，**无法解决热数据路由倾斜问题**，热点分片必须主动做路由打散。
2. shrink分片收缩属于不可逆操作，前期必须合理规划主分片数量，收缩后无法自动复原。
3. 流转存在十几分钟正常延迟，轮询间隔+分片迁移耗时，流转时间卡点不会完全精准。
4. forcemerge段合并IO消耗极高，尽量避开业务高峰期执行。
5. 集群必须提前部署 `data_hot/data_warm/data_cold` 对应属性节点，缺少对应节点流转会卡住停滞。
6. 禁止手动随意修改索引 `data_tier` 配置，容易造成分片乱跑、集群变黄。
7. freeze冻结适合长期归档索引，频繁查询索引建议关闭冻结避免查询延迟上涨。
8. 自动删除存在数据丢失风险，核心业务数据必须提前配置快照备份，验证无误再启用删除阶段。

## 6.6 ILM故障排查对照表

| 现象                   | 根因                           | 排查命令                                   | 解决方案                           |
| -------------------- | ---------------------------- | -------------------------------------- | ------------------------------ |
| 索引到期不流转，一直停留在hot     | 缺少对应层级节点 / 分片搬迁阻塞 / 上阶段动作未完成 | GET 索引名/_ilm/explain                   | 补齐节点；低峰调大搬迁并发；排查阻塞任务           |
| Rollover长期不滚动，索引持续膨胀 | 写入没有走别名、阈值设置过大、索引为空          | GET _alias/别名                          | 业务切写入别名；调整rollover阈值；允许空索引滚动配置 |
| ILM流转期间集群IO飙升        | forcemerge+分片搬迁并发执行          | GET _cat/recovery?v                    | 高峰期临时关闭forcemerge，错开搬迁时段       |
| 索引提前进入冷层             | 混淆索引创建时间与业务文档时间              | GET 索引名/_ilm/explain                   | 理解ILM计时规则，调整min_age参数          |
| 冻结后查询变慢              | freeze机制内存换磁盘，设计使然           | GET 索引名/_settings?filter_path=*.frozen | 频繁查询执行关闭冻结                     |
| 索引被误删除               | delete阶段min_age设置过小          | GET _ilm/policy/策略名                    | 拉长留存时长；依赖快照备份恢复数据              |

## 6.7 ILM常用运维命令

```http
# 查看索引生命周期流转详情、卡住根因（核心排障）
GET /index_name/_ilm/explain

# 查看全局ILM轮询配置
GET _cluster/settings?include_defaults=true&filter_path=indices.lifecycle*

# 单个索引启停生命周期
POST index_name/_ilm/stop
POST index_name/_ilm/start

# 全局启停所有ILM
POST _ilm/stop
POST _ilm/start

# 修改全局巡检间隔
PUT _cluster/settings
{
  "transient": {
    "indices.lifecycle.poll_interval": "5m"
  }
}

# 查询所有绑定ILM策略的索引
GET _settings/index.lifecycle.name:*

# 查看索引当前冷热层级
GET index_name/_settings?filter_path=*.data_tier
```

## 6.8 ILM策略修改快速判断口诀

1. 写入量波动大 → 调整rollover容量/时长阈值
2. 温层查询频次变化 → 启停forcemerge、调整shrink分片数量
3. 集群堆内存紧张 → 冷索引统一开启freeze，温层适度缩分片
4. 存储成本压力大 → 缩短warm/cold的min_age提前归档
5. 业务留存规则变更 → 统一修改四阶段min_age时长
6. 高峰期IO抖动 → 临时关闭forcemerge、错开分片搬迁时段
7. 流转快慢不符合预期 → 调整全局poll_interval巡检间隔

---

# 七、Reindex 数据重构全场景方案

## 前置核心结论

ILM、分片均衡、磁盘水位属于**常驻动态集群配置**，满足条件自动执行；
修改字段、修改分片数、修改路由规则属于**一次性架构变更**，ES底层无法原地修改，统一落地流程：
**新建索引 → Reindex迁移（支持Painless脚本转换）→ 业务双写保障增量一致性 → 原子别名切换 → 观察平稳收尾**
整套流程无法依靠集群配置全自动执行，必须人工运维落地。

## 7.1 场景一：全量修改字段名称、字段类型

### 不可原地修改清单

✅ 仅新增字段支持直接PUT Mapping原地修改
❌ 修改已有字段类型、字段重命名、修改字段分词器均无法原地执行，必须重建索引Reindex迁移

### 完整零停机改造步骤

1. 梳理新旧字段一一映射关系

2. 创建目标新索引，定义全新Settings+Mapping+绑定ILM策略

3. 执行Reindex + Painless脚本完成字段改名、类型转换赋值
   
   ```http
   POST _reindex
   {
   "source": {"index": "order_v1"},
   "dest": {"index": "order_v2"},
   "script": {
    "source": """
      if(ctx._source.containsKey('o_id')){
        ctx._source.order_id = String.valueOf(ctx._source.remove('o_id'));
      }
      if(ctx._source.containsKey('create_dt')){
        ctx._source.create_time = ctx._source.remove('create_dt');
      }
    """
   }
   }
   ```

4. 新旧索引总条数统计、抽样数据校验，确认无丢失、格式无误

5. 业务开启双写，避免迁移间隙新增数据丢失

6. 时间范围Reindex追赶窗口期增量数据，两边数据完全对齐

7. 原子别名切换，业务无感切流至新索引

8. 平稳观察数日，停止双写逻辑，择机归档删除旧索引释放空间

### 风险避坑要点

日期字符串转date提前匹配format避免解析失败；空值、脏数据增加判断兜底；超大索引开启限流、分批迁移；切换后旧索引保留一段时间用于回滚兜底，不要立刻删除。

## 7.2 场景二：重新分片、修改路由规则、全局打散热点

### 底层原理

分片计算公式由「主分片数量+路由Key」共同决定，任意一项变更，原有分片分配全部失效；
split/shrink仅支持倍数增减分片，**不能修改路由逻辑、无法打散热点倾斜**，通用解决方案只能Reindex重建索引。

### 完整落地步骤

1. 创建新索引：配置全新主分片数量、自定义_routing规则、ILM策略、初始化别名

2. Reindex内嵌脚本生成新路由值，全量数据重新哈希打散分配
   
   ```http
   POST _reindex?slices=auto&requests_per_second=2000
   {
   "source": {"index": "order_old"},
   "dest": {"index": "order_new"},
   "script": {
    "source": """
      def suffix = Math.abs(ctx._source.orderId.hashCode()) % 3;
      ctx._routing = ctx._source.province + "_" + suffix;
    """
   }
   }
   ```

3. 校验文档总量、统计各分片文档数量，确认倾斜问题缓解、分片分布均匀

4. 业务开启双写，同步增量写入新旧两个索引

5. 时间区间Reindex追赶迁移期间增量数据

6. 执行原子别名切换，业务访问无缝切换

7. 运行平稳后关闭双写，旧索引观察一段时间后清理归档

### 常见误区澄清

1. rebalance只会挪动分片所在节点位置，**不会变更文档路由归属**，无法解决分片内部数据量倾斜。
2. split/shrink只能倍数调整分片数量，不能重构路由规则，不能打散热点分片。
3. ES没有原地全局重哈希配置开关，路由、分片变更重构只能采用Reindex方案。

---

# 八、索引别名架构设计 + 普通索引上线初始化标准模板

## 核心认知

1. 别名本身不是索引，是虚拟软链接，不占用存储空间、不能直接写入数据；真实索引带版本迭代命名（order_v1、order_v2），别名是长期固定业务访问入口。
2. 最佳实践：**索引创建之初就绑定业务别名，业务读写全程访问别名，禁止硬编码真实物理索引名称**。
3. 原子切换优势：单条指令同时解绑旧索引+绑定新索引，无中间空档查询报错；回滚操作简单，无需修改业务代码、无需服务重启发版。

## 8.1 索引初始化完整模板（索引+别名+ILM三合一，生产直接复用）

### 前置：ILM策略已创建（order_hot_warm_cold_policy，前文已有，不再重复）

### 新建索引同时绑定业务别名

```json
PUT order_v1
{
  "settings": {
    "number_of_shards": 9,
    "number_of_replicas": 1,
    "index.lifecycle.name": "order_hot_warm_cold_policy",
    "index.routing.allocation.data_tier": "hot"
  },
  "mappings": {
    "properties": {
      "orderId": {"type": "keyword"},
      "province": {"type": "keyword"},
      "createTime": {"type": "date", "format": "yyyy-MM-dd HH:mm:ss||epoch_millis"},
      "amount": {"type": "double"}
    }
  },
  "aliases": {
    "alias_order": {}
  }
}
```

## 8.2 业务强制使用规范

1. 新增、查询、更新、删除全部统一使用 `alias_order`，杜绝代码写死 `order_v1`
2. 后续改字段结构、调整分片数量、重构路由打散，仅切换别名即可完成无感迭代

## 8.3 重构专用原子切换别名语句

```http
POST _aliases
{
  "actions": [
    {"remove": {"index": "order_v1", "alias": "alias_order"}},
    {"add": {"index": "order_v2", "alias": "alias_order"}}
  ]
}
```

## 8.4 配套校验命令

```http
# 查看别名绑定关系
GET _alias/alias_order

# 查看索引ILM绑定配置
GET order_v1/_settings?filter_path=*.lifecycle

# 查看分片、副本配置
GET order_v1/_settings
```

## 8.5 别名设计总结

业务上线初始化创建带版本物理索引，同步绑定固定业务别名，挂载ILM实现冷热自动流转；该架构天然适配后续Reindex各类重构场景，仅原子切换别名即可实现业务无感迁移，大幅降低迭代变更与回滚成本。

---

# 九、Rollover 时序滚动索引 完整初始化模板（日志/流水/时序订单专用）

## 9.1 Rollover 核心原理回顾

1. 依靠**写入别名**持续接收写入，索引达到「时长阈值 / 单主分片容量阈值」自动新建物理索引
2. 旧索引自动脱离写入，交由 ILM 执行冷热流转、分片收缩、归档删除
3. 命名结构规范
   - 写入别名：`alias_order_roll`（业务永久只写入该别名）
   - 初始物理索引：`order-000001`
   - 自动滚动生成：`order-000002`、`order-000003`……
4. 适用场景：系统日志、监控埋点、流水记录、时序订单等无限持续写入类业务

## 9.2 前置依赖：复用已有ILM策略 order_hot_warm_cold_policy

## 9.3 步骤1：创建滚动索引专属模板（统一管控后续自动新建索引配置）

```json
PUT _index_template/template_order_roll
{
  "index_patterns": ["order-*"],
  "template": {
    "settings": {
      "number_of_shards": 9,
      "number_of_replicas": 1,
      "index.lifecycle.name": "order_hot_warm_cold_policy",
      "index.routing.allocation.data_tier": "hot"
    },
    "mappings": {
      "properties": {
        "orderId": {"type": "keyword"},
        "province": {"type": "keyword"},
        "createTime": {"type": "date", "format": "yyyy-MM-dd HH:mm:ss||epoch_millis"},
        "amount": {"type": "double"}
      }
    }
  },
  "priority": 20
}
```

## 9.4 步骤2：初始化首个滚动索引 + 配置可写别名

```json
PUT order-000001
{
  "aliases": {
    "alias_order_roll": {
      "is_write_index": true
    }
  }
}
```

关键属性说明：
`"is_write_index": true`
代表该别名下**唯一可写入索引**；rollover触发后ES自动新建索引、切换该标识归属，旧索引自动变为只读，进入生命周期流转。

## 9.5 业务使用规范

1. 新增写入、更新文档统一使用别名 `alias_order_roll`
2. 单索引查询指定具体索引名称；批量查询全部历史数据使用通配符 `order-*`
3. 禁止业务代码硬编码 `order-000001`、`order-000002` 等物理索引名称

## 9.6 手动触发 Rollover 测试（验证机制有效性）

```http
POST alias_order_roll/_rollover
```

满足 `max_age` 或 `max_primary_shard_size` 任一阈值，自动生成 `order-000002`，自动切换写入标识。

## 9.7 常用运维排查命令

```http
# 查看别名当前写入索引状态
GET _alias/alias_order_roll

# 查看单个索引ILM流转进度
GET order-000001/_ilm/explain

# 查看滚动索引模板配置
GET _index_template/template_order_roll
```

## 9.8 后续架构重构方案（改分片/改路由/改Mapping）

和普通索引重构逻辑完全一致：

1. 新建一套全新索引模板 + 全新写入别名体系
2. 分批Reindex迁移存量 `order-*` 历史数据
3. 业务切换写入新别名，平稳运行后逐步下线旧滚动链路

## 9.9 Rollover 关键避坑要点

1. 同一个别名同一时刻只能有一个 `is_write_index: true`，由ES自动维护，禁止人工随意修改。
2. ILM内部 `rollover` 配置只管控触发滚动的条件，索引命名规则、分片、Mapping结构由索引模板统一约束。
3. 时序索引严格控制单分片30~50GB，避免滚动滞后、分片过大导致迁移缓慢。
4. 海量滚动索引场景管控单节点总分片数量，防止元数据撑爆JVM堆内存。
5. Rollover索引ILM计时起点为**索引滚动生成时刻**，并非文档内部业务时间，流转规则和前文说明完全一致。

## 9.10 本节小结

Rollover滚动索引面向时序无限写入场景，依托写入别名+`is_write_index`实现自动拆分索引，由索引模板统一约束新建索引分片、Mapping、ILM配置；达到容量或时长阈值自动生成新索引，旧索引进入ILM冷热生命周期流转；整体基于别名架构，后续分片、路由、字段结构重构依旧采用Reindex+别名切换方案，业务全程无需修改代码。

---

# 十、ILM反向回流：温/冷层级向上回退通用方案+一键命令速查表+批量脚本

## 10.1 核心前置认知

ILM设计为**单向流转 Hot→Warm→Cold→Delete，不存在原生自动回流逻辑**，索引一旦打上 `data_tier: warm/cold` 并完成分片搬迁，想要回退上一层级，只能**人工修改索引冷热标签，强制触发分片回迁**，同时必须做防护，避免ILM下一轮巡检再次自动下沉。

### 通用底层逻辑（所有层级回退操作完全通用）

1. 修改索引 `index.routing.allocation.data_tier` 为目标上层层级

2. ES分片分配器检测分片所在节点与标签不匹配，后台自动执行分片搬迁

3. 暂停该索引ILM生命周期，防止再次正向流转下沉

4. 统一查看搬迁进度命令
   
   ```http
   GET _cat/recovery?v&h=index,shard,percent,source_node,target_node,stage
   ```

5. 搬迁全部 `percent=100` 代表回迁完成

## 10.2 单索引手动回退分步操作

### 步骤1：查询索引当前冷热层级

```http
GET your_index/_settings?filter_path=*.data_tier
```

### 场景1：Warm → Hot 温层回退热层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "hot"
}
POST your_index/_ilm/stop
```

### 场景2：Cold → Warm 冷层回退温层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "warm"
}
POST your_index/_ilm/stop
```

### 场景3：Cold → Hot 冷层跨层直接回退热层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "hot"
}
POST your_index/_ilm/stop
```

### 步骤4：回迁完成恢复自动生命周期

```http
POST your_index/_ilm/start
```

### 步骤5：配套兼容处理（索引存在对应操作才需要执行）

1. 温层默认只读，需要重新写入则解除只读锁
   
   ```http
   PUT your_index/_settings
   {
   "index.blocks.write": null
   }
   ```

2. 冷层开启过freeze冻结，高频查询需要关闭冻结
   
   ```http
   PUT your_index/_settings
   {
   "index.frozen": false
   }
   ```

3. 索引执行过shrink缩分片：**仅能搬迁节点位置，分片数量无法自动复原**，如需恢复原始分片规模只能新建索引Reindex重构

4. forcemerge段合并结果永久保留，不影响回迁与读写，无需额外处理

## 10.3 致命边界：Delete删除阶段无法回退（新增快照全套补充说明）

索引被 ILM 执行 delete 动作后，物理文件直接清除，没有内置回退机制；唯一补救手段：删除前有快照备份，从快照恢复重建索引，无备份则数据永久丢失。

### 重点答疑：ES快照是否需要手动配置？（高频追问）

💡 **标准答案：自建ES集群，所有快照相关功能全部需要手动配置，ES原生无任何自动快照、无默认回收站、无ILM删除自动备份**

1. ES出厂默认：快照仓库为空、无定时备份任务、ILM删除流程**零内置备份逻辑**，删索引直接物理清空磁盘文件，无任何兜底
2. 云托管ES（阿里云/腾讯云ES）：自动自带快照，是云厂商提前帮你手动搭建好了仓库+SLM策略，**并非ES原生自带能力**
3. 想要ILM删除后可恢复，必须运维提前手动部署整套备份架构，缺一不可

### 生产两种ILM删除防丢数据兜底方案（完整可直接复制）

#### 方案一：ILM原生delete阶段前置快照（删除前自动备份，精准防护）

手动修改ILM策略，索引到期**先打快照、再删索引**，原生ILM默认没有该动作，必须手动添加配置：

```json
"delete": {
  "min_age": "90d",
  "actions": {
    // 先自动创建快照备份
    "snapshot": {
      "repository": "es_backup_repo",
      "name": "ilm-del-snap-{{index}}"
    },
    // 备份完成后再物理删除索引
    "delete": {}
  }
}
```

#### 方案二：SLM自动快照生命周期（生产最优，全局兜底）

不修改ILM策略，独立配置SLM定时自动快照，每日定时全量备份索引，覆盖误删、误改、磁盘损坏全场景风险，生产通用方案。

1. 第一步：手动创建快照仓库（必做前置）
   
   ```http
   PUT _snapshot/es_backup_repo
   {
   "type": "fs",
   "settings": {
    "location": "/data/es_snapshot",
    "compress": true
   }
   }
   ```

2. 第二步：手动创建SLM每日自动备份策略
   
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

### 快照恢复索引极简运维命令（误删恢复直接复制执行）

1. 查看仓库下所有快照名称
   
   ```http
   GET _snapshot/es_backup_repo/_all
   ```

2. 从指定快照恢复被删除索引（示例恢复 order-000001）
   
   ```http
   POST _snapshot/es_backup_repo/ilm-del-snap-order-000001/_restore
   {
   "indices": "order-000001",
   "include_global_state": false,
   "rename_pattern": "(.+)",
   "rename_replacement": "restored_$1"
   }
   ```
   
   参数说明：
- `rename_pattern`、`rename_replacement`：避免恢复与现有索引重名，自动加前缀 `restored_`；如需同名覆盖可移除这两行
3. 查看快照恢复进度
   
   ```http
   GET _cat/recovery?v
   ```

### 一句话总结

ILM删除索引不可逆，ES本身没有自动快照，快照仓库、SLM自动备份、ILM删除前置快照全部需要手动配置；只有提前做好快照备份，被删除的索引才能通过快照API恢复重建，无备份数据彻底丢失无法找回。

## 10.4 生产一键批量Shell脚本：所有Warm索引批量回迁Hot

文件名：`warm_to_hot.sh`

```bash
#!/bin/bash
ES_HOST="http://127.0.0.1:9200"
# 集群开启账号密码取消下面注释并填写
# ES_USER="elastic"
# ES_PWD="xxxxxx"

echo "========== ES温层索引批量回迁热节点 开始执行 =========="
echo "正在查询集群所有warm层级业务索引（过滤系统内置.开头索引）"

# 获取所有warm索引，过滤系统索引
if [ -n "$ES_USER" ];then
  INDEX_LIST=$(curl -s -u $ES_USER:$ES_PWD "$ES_HOST/_settings/index.routing.allocation.data_tier:warm?pretty" | jq -r 'keys[] | select(startswith(".") | not)')
else
  INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.data_tier:warm?pretty" | jq -r 'keys[] | select(startswith(".") | not)')
fi

if [ -z "$INDEX_LIST" ];then
  echo "✅ 集群无待回迁的warm业务索引，脚本直接退出"
  exit 0
fi

echo "✅ 待批量回迁索引列表："
echo "$INDEX_LIST"
echo "---------------------------------------------------------"

# 循环逐个修改冷热标签并暂停ILM
while read index; do
  if [ -z "$index" ];then
    continue
  fi
  echo "正在处理索引：【$index】"
  JSON_BODY='{"index.routing.allocation.data_tier":"hot"}'
  if [ -n "$ES_USER" ];then
    curl -s -u $ES_USER:$ES_PWD -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  else
    curl -s -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  fi
  curl -s -X POST "$ES_HOST/$index/_ilm/stop"
  echo -e "\n✅ $index 已切换为hot热层级，分片开始自动回迁\n"
done <<< "$INDEX_LIST"

echo "========== 全部索引批量回迁指令执行完成 =========="
echo ""
echo "👉 查看分片回迁实时进度：curl $ES_HOST/_cat/recovery?v"
echo "👉 防止ILM自动回迁：POST /索引名/_ilm/stop"
echo "👉 恢复生命周期自动流转：POST /索引名/_ilm/start"
```

### 脚本使用步骤

1. 安装jq依赖（解析JSON必备）
   
   ```bash
   # CentOS
   yum install jq -y
   # Ubuntu/Debian
   apt install jq -y
   ```

2. 修改脚本内ES地址、账号密码

3. 授权+运行
   
   ```bash
   chmod +x warm_to_hot.sh
   ./warm_to_hot.sh
   ```

### 脚本通用改造说明

如需批量Cold→Warm / Cold→Hot，仅修改一行过滤匹配条件即可：

```bash
INDEX_LIST=$(curl xxx | jq -r 'keys[] | select(startswith(".") | not)')
```

将匹配 `index.routing.allocation.data_tier:warm` 修改为 `index.routing.allocation.data_tier:cold`，目标data_tier同步修改即可复用整套逻辑。

## 10.5 回流方案选型对比

| 方案                 | 适用场景                 | 优点              | 缺点                |
| ------------------ | -------------------- | --------------- | ----------------- |
| 逐个暂停单索引ILM         | 个别索引临时回热，后续仍需要生命周期流转 | 精细化控制，不影响集群全局策略 | 需要逐个手动操作          |
| 修改全局ILM策略拉长min_age | 全局所有索引延后进入温层         | 一次修改全局生效        | 影响全部索引生命周期，变更风险偏高 |

## 10.6 极简要点总结

ES的ILM冷热流转是单向的，不支持自动从温层、冷层回退到热节点。如果温层、冷层数据需要回滚上一级，统一操作逻辑是手动修改索引的data_tier标签为目标上层，触发ES分片分配器自动将分片迁回对应节点；回迁完成后必须暂停该索引的ILM生命周期，否则下一轮ILM巡检会再次把索引自动下沉。另外执行过shrink缩分片的索引只能迁移节点位置，无法自动恢复原有分片数量，想要还原分片结构只能通过Reindex重建索引；索引进入Delete阶段被删除后无法直接回退，只能依靠快照备份恢复。大批量索引回流可以编写Shell脚本一键批量执行，减少重复人工操作。

---

# 十一、全局分片异常统一排障手册

### 现象1：节点分片数量分布不均衡

原因：均衡阈值设置保守、分片搬迁并发偏低、节点磁盘水位触发分配限制
处理：业务低峰调大rebalance并发、调低balance阈值、清理高磁盘占用节点，等待分片自动收敛均衡

### 现象2：集群变黄/变红，分片 unassigned 未分配

原因：磁盘超限、主副分片同节点约束、冷热层级节点不匹配、副本数量大于节点数、节点宕机失联
排查定位命令：`GET _cluster/allocation/explain`
处理：清理磁盘空间、临时调低副本数、补齐对应冷热节点、修复故障节点

### 现象3：分片搬迁速度极慢

原因：单节点出入并发限制、恢复带宽限速、节点IO压力过高
处理：适度调高recovery并发与带宽上限、错开业务高峰期迁移

### 现象4：分片数量均衡，但单个分片数据量极大、热点倾斜

原因：自定义routing设计不合理，产生业务热点分片，rebalance仅挪动节点不会改变分片内文档归属
处理：路由二次打散设计、新建分片规格索引Reindex全局重新打散数据

### 现象5：分片搬迁导致高峰期IO雪崩

临时关闭集群全局自动均衡规避抖动

```http
PUT _cluster/settings
{"transient":{"cluster.routing.allocation.allow_rebalance":"none"}}
```

业务低峰恢复均衡

```http
PUT _cluster/settings
{"transient":{"cluster.routing.allocation.allow_rebalance":"indices_all_active"}}
```

### 现象6：冷热分片跨层级乱跑、自动跳层

原因：运维人员手动随意修改索引data_tier配置、索引模板分配规则错乱、节点属性配置不一致
处理：统一重置索引层级绑定配置，核对集群所有节点冷热属性标签，规范模板与ILM配置

---

# 十二、跨中间件统一分片思想（ES / MySQL / Redis / Kafka）

1. 分布式分片底层通用核心逻辑：**分片键 → 哈希/取模运算 → 绑定固定分片**
2. 如果分片键本身是热度不均衡维度（日期、省份、大客户ID），必然出现数据倾斜问题
3. 最优架构方案
   - 优先选用高离散唯一主键（雪花ID等）天然均衡分片
   - 业务必须使用热点维度做分片键时，采用后缀二次打散分流
4. 前期分片规划原则：预留余量、宁多勿少；后期扩容、重构、数据迁移成本极高
5. 主流成熟中间件共性：原生支持动态配置、分片自动均衡、生命周期自动管控，长期运维成本极低；自行研发分片路由体系，开发、压测、踩坑、运维成本极高，中小企业性价比极低

补充：各大中间件架构均经过大厂海量业务场景打磨，覆盖各类边界异常场景；小型团队务实路线：吃透原生能力、按需二次封装适配业务，不要盲目自研分布式分片架构。

---

# 十三、三分钟整体总结

ES依靠MurmurHash3哈希取模实现文档路由分片，主分片创建之后不可修改；业务自定义路由极易产生热点分片倾斜，标准解决方案为路由Key增加后缀二次打散均衡数据。
集群依靠IndexBalance、ShardBalance双层加权算法实现分片数量均衡分配，分配逻辑同时受磁盘三级水位、主副分片隔离、冷热节点归属三层硬性约束；生产上单分片控制在30~50GB区间，前期分片规划遵循宁多勿少原则。

ILM冷热流转为单向链路 Hot→Warm→Cold→Delete，依靠后台轮询调度，不会识别文档内部业务时间，只能优化存储成本，无法解决热分片数据倾斜；**所有层级向上回退操作模式完全通用**，修改索引data_tier标签触发分片回迁，同时暂停索引ILM防止再次下沉；shrink分片收缩属于不可逆变更，删除后的索引无法直接回退，仅可依靠快照备份恢复。
ES无默认自动快照，快照仓库、SLM定时备份、ILM删除前置备份均需要手动部署配置。

字段改名改类型、调整主分片数量、重构路由打散热点均无法原地修改，统一采用**新建索引+Reindex脚本迁移+业务双写同步增量+原子别名切换**零停机改造方案；业务上线初期就规划固定访问别名，底层索引迭代重构全程不用修改业务代码，切换、回滚简单便捷。面向日志、流水无限时序写入场景，可采用Rollover滚动索引自动拆分索引，配合ILM完成完整生命周期治理。
ILM、分片均衡属于集群常驻自动配置体系，Reindex结构重构、ILM反向回流属于一次性人工运维变更流程，无法全自动配置执行。

该分片设计思想通用于MySQL分库分表、Redis集群槽位、Kafka分区设计；成熟中间件自带均衡自愈与生命周期能力，运维成本更低，中小企业优先使用原生方案，不盲目自研分布式分片路由架构。


