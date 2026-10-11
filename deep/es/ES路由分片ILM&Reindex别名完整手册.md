---
title: "Elasticsearch 路由+分片分配+ILM冷热分层+Reindex重构+别名架构 完整版整合手册"
summary: "1. 索引/分片/节点层级基础关系 2. 路由寻址原理 & MurmurHash3 哈希机制 3. 两类分片倾斜成因与路由二次打散方案 4. 分片分配算法、磁盘水位、搬迁管控 5.…"
date: 2026-08-01
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 分库分表
  - ILM
category: deep
series: "es"
slug: "ES路由分片ILM&Reindex别名完整手册"
reading_time: 33
featured: false
draft: false
---
# Elasticsearch 路由+分片分配+ILM冷热分层+Reindex重构+别名架构 完整版整合手册

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
8. 索引别名设计思想 + 上线初始化标准模板
9. 全局分片异常统一排障手册
10. 跨中间件分片通用思想
11. 面试精简总结

---

# 一、索引 / 分片 / 节点 层级关系

## 层级结构

集群 Cluster → 多个索引 Index → 索引包含主分片+副本分片 → 分片部署在节点 Node → 节点依托服务器磁盘、内存、CPU

## 两条核心铁律

1. **一个分片永久只归属一个索引**，不能跨索引存储数据；不同索引分片可同名 shard-0，物理文件完全隔离。
2. **同一个分片的主分片、副本分片禁止部署在同一节点**，防止单机宕机同时丢失主副副本。
   
   ## 示例：3节点，order索引 9主1副本（总分片18）
   
   总分片公式：
   \[
   \text{总分片}=主分片数\times(副本数+1) = 9\times 2 = 18
   \]
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
- 节点：存储容器，容纳任意索引分片
- 分片：索引私有存储单元，不可跨索引复用

---

# 二、路由寻址底层原理 & MurmurHash3

## 1、路由计算公式（底层固定）

```java
String routingKey = 自定义routing != null ? 自定义routing : 文档_id;
int hash = Murmur3HashFunction.hash(routingKey);
int shardId = Math.abs(hash) % number_of_primary_shards;
```

### 硬性约束

1. `number_of_primary_shards` **索引创建后永久不可修改**，修改会导致全部路由错乱，数据无法查询
2. 副本数量 `number_of_replicas` 可动态调整，不影响路由逻辑
3. 同一个路由Key，分配分片终身固定
   
   ## 2、MurmurHash3 两个版本分工
   
   | 版本                  | 位数   | 使用场景              | 说明            |
   | ------------------- | ---- | ----------------- | ------------- |
   | MurmurHash3_x86_32  | 32位  | 分片路由计算            | 打散有序ID；哈希碰撞无害 |
   | MurmurHash3_x64_128 | 128位 | cardinality基数去重聚合 | 极低碰撞，统计精准     |
   
   ### 哈希碰撞说明
   
   不同 routing 算出相同哈希进入同一分片属于正常现象；
   分片内部依靠 `_id` 区分文档，**不会发生数据覆盖、更新错乱**；哈希目的是打散有序数据，不是彻底消除碰撞。
   
   ## 3、Routing 字符串长度规范
4. 不是越长打散越好：哈希结果固定32位，超长字符串只会增加CPU开销，易触发URL超长报错
5. 不是越短越好：字符过少样本空间小，容易哈希扎堆倾斜
6. 生产最优长度：**6~30个字符**
   
   ## 4、路由打散与MySQL兼容性
   
   路由二次打散仅修改 `_routing` 字段，文档 `_id`、业务主键不变，ES与MySQL可依靠唯一主键正常关联。

---

# 三、两类分片倾斜成因 + 路由二次打散解决方案

## 类型1：路由数据倾斜（业务层面，分片均衡算法无法解决）

### 成因

为减少跨分片聚合，使用**省份、日期、商户ID、大客户ID**作为自定义 routing；热点Key长期落在单个分片，分片数据体量悬殊。

### 典型场景

- 按日期路由：当日写入分片爆满，历史分片闲置
- 按省份路由：广东、浙江等大省分片体积远超其余分片
  
  ### 根治方案：路由二次后缀打散
  
  思路：`原业务key_随机后缀`，单个热点路由拆分多路，分散至多个分片
  示例：
  原始路由：`guangdong` → 固定单个分片
  打散路由：`guangdong_0`、`guangdong_1`、`guangdong_2` 分散多个分片
  
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
  
  打散份数仍无法缓解倾斜：主分片不能原地修改，新建更高分片数索引，`reindex` 迁移存量，业务双写切换别名。
  
  ## 类型2：节点分片分配倾斜（集群层面，ES均衡算法自动调节）
  
  现象：节点总分片数量差距过大，例：3节点理论各6分片，实际A=10、B=5、C=3。

---

# 四、分片节点分配算法完整解析

## 1、分配整体流程

前置过滤校验 → 候选节点打分计算 → 择优分配/搬迁分片

### 前置过滤规则（优先级最高）

1. 同分片主、副本不能部署同一节点
2. 磁盘水位超限节点禁止迁入分片
3. 冷热层级节点角色匹配校验（hot/warm/cold隔离）
4. 节点离线、故障、单节点分片超限直接剔除
   
   ## 2、加权打分均衡算法
   
   公式：
   `综合得分 = index_balance差值 × 0.55 + shard_balance差值 × 0.45`
   **得分越低，节点越空闲，优先分配分片**
5. **IndexBalance（权重0.55）**：控制同一个索引分片在节点均匀分布，避免单节点塞满某索引大量分片
6. **ShardBalance（权重0.45）**：控制集群所有索引总分片整体均衡
   均衡阈值 `cluster.routing.allocation.balance.threshold` 默认 1.0，偏差超出阈值才触发 rebalance；调小均衡更积极、IO更多，调大收敛惰性更强。
   
   ## 3、磁盘三级水位（分配强门槛）
- low(85%)：开始主动迁出分片
- high(90%)：禁止新分片迁入
- flood_stage(95%)：节点所有索引只读，防止磁盘打满宕机
  
  ## 4、分片搬迁并发配置（运维常用）
  
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
  
  业务高峰调低防IO雪崩，低峰调高加速分片均衡收敛。
  
  ## 5、主副分片补充规则
1. 节点宕机后，从可用副本中选举新主分片
2. 多副本自动打散不同节点、机架（开启机架感知）
3. 副本跟随主分片同步流转冷热层级

---

# 五、索引&节点容量规划规范（生产落地标准）

## 1、单分片合理容量（核心规划依据）

- 热数据高写入高频查询：**20~35GB**
- 常规业务索引通用最优：**35~50GB**
- 冷归档极少查询：上限放宽 60~80GB
  
  > 分片过大：迁移、段合并、故障恢复、快照恢复速度极慢
  
  ## 2、主分片数量计算公式
  
  `建议主分片数 ≈ 预估未来3年总数据量 ÷ 50GB`
  原则：**分片宁多勿少**，主分片创建后不可修改，前期预留余量，避免后期重构迁移。
  
  ## 3、副本配置说明
  
  示例：9主 + 副本数=1 → 9主9副本，总分片18，一对一主副配对；副本数量可随时动态调整。
  
  ## 4、节点磁盘使用率红线（冷热区分）
- hot热节点(SSD)：使用率 ≤75%
- warm温节点：使用率 ≤80%
- cold冷节点(HDD)：使用率 ≤82%
  预留空间应对分片搬迁、段合并、数据突增，规避磁盘水位异常。
  
  ## 5、单节点分片总数上限
  
  保守阈值：单节点总分片 ≤ 800
  警戒上限：≤ 1000
  分片过多元数据占用堆内存暴涨，集群管控压力激增。
  
  ## 6、单个索引总体积上限
  
  中小集群(3~5节点)：单索引总容量 ≤1.5TB
  中大型集群：单索引 ≤3TB
  时序数据禁止单索引长期膨胀，采用 Rollover+ILM 滚动拆分。

---

# 六、ILM 冷热分层完整手册

## 6.1 底层核心原理

1. **流转最小单位：整个索引，不支持单条文档单独冷热迁移**
2. 计时基准（高频误区）
   - 普通静态索引：计时起点 = 索引创建时间，与文档 `create_time/update_time` 无关
   - Rollover滚动索引：计时起点 = 索引滚动切换时刻
     
     > ILM 不会读取文档内部业务时间字段做冷热判断
3. 调度模式
   内置 `ilm-service` 后台轮询线程，**非 Cron 定时任务**
   默认巡检间隔10分钟，配置项：`indices.lifecycle.poll_interval`
   每轮遍历绑定策略索引，上一阶段动作未完成则跳过本轮，不会重复执行。
4. 流转单向不可逆：`Hot → Warm → Cold → Delete`，无法自动回流热节点
5. 流转本质：修改索引 `data_tier` 分配标签约束，分片分配器自动将分片搬迁至对应节点池
   
   ## 6.2 ILM轮询 vs 自定义定时任务 对比
   
   结论：ILM轮询稳定性、容错、集群可控性全面优于自研Cron定时任务
6. 负载：轮询分散触发无瞬时尖峰；Cron整点批量搬迁极易打满IO
7. 幂等：ILM内置状态机，阶段未完成不往下执行；自研需自行实现幂等、重试、状态记录
8. 资源管控：复用集群原生分片搬迁限流配置；定时任务需额外开发限流逻辑
9. 异常自愈：迁移失败下一轮自动重试；定时任务失败需要人工排查补偿
10. 资源开销：轮询仅遍历索引元数据，空闲开销极低；定时任务常驻调度维护成本高
11. 唯一短板：存在最多一个轮询周期时间误差；Cron可精准整点执行
    补充：修改 `data_tier` 标签是流转动作，两种调度修改标签开销基本一致，差距在调度架构设计。
    
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
    | rollover.max_docs               | 文档条数触发滚动     | 默认注释关闭 | 仅文档大小高度规整时序数据启用      | 按需配置，常规禁用              |
    | set_priority                    | 分片故障恢复优先级    | 100    | 核心业务索引优先恢复           | 普通100，核心110~120        |
    
    > 非时序静态索引可直接删除整个rollover配置
    
    ### Warm 温阶段
    
    | 参数                          | 作用            | 默认   | 修改场景               | 推荐值                  |
    | --------------------------- | ------------- | ---- | ------------------ | -------------------- |
    | min_age                     | 滚动后多久转入温节点    | 7d   | 活跃查询周期调整、提前释放热节点资源 | 活跃久：10~15d；快速归档：3~5d |
    | allocate.require.data_tier  | 强制分片分配warm节点  | warm | 整体下线冷热分层架构         | 注释allocate配置         |
    | forcemerge.max_num_segments | 段合并优化查询，IO消耗高 | 1    | 温层查询多少、高峰期防IO打满    | 有查询保留1；几乎无查询关闭       |
    | shrink.number_of_shards     | 收缩主分片（不可逆）    | 3    | 集群分片过多内存紧张、仍有聚合查询  | 有查询：3~5片；极少查询：1~2片   |
    | readonly                    | 索引只读防延迟写入生成新段 | 开启   | 业务存在滞后补发数据         | 有迟写则关闭readonly       |
    | set_priority                | 分片恢复优先级       | 50   | 重要历史订单提升恢复优先级      | 微调60~70              |
    
    ### Cold 冷阶段
    
    | 参数                         | 作用             | 默认   | 修改场景              | 推荐值                     |
    | -------------------------- | -------------- | ---- | ----------------- | ----------------------- |
    | min_age                    | 多久转入冷归档节点      | 30d  | 存储成本敏感提前归档、频繁溯源延后 | 成本敏感：15~20d；频繁溯源：45~60d |
    | allocate.require.data_tier | 强制分片分配cold节点   | cold | 下线冷热分层架构          | 注释allocate配置            |
    | freeze                     | 索引冻结降内存，查询性能下降 | 开启   | 偶尔检索排查、集群内存紧张     | 几乎不查询开启；偶尔检索关闭          |
    | set_priority               | 分片恢复优先级        | 10   | 归档数据降低资源抢占        | 调整5~8                   |
    
    ### Delete 删除阶段
    
    | 参数       | 作用          | 默认  | 修改场景             | 推荐值                                    |
    | -------- | ----------- | --- | ---------------- | -------------------------------------- |
    | min_age  | 滚动后留存时长自动删除 | 90d | 合规留存、磁盘紧急清理、永久归档 | 短期日志：60~90d；合规：180d~3年；永久保存：删除delete节点 |
    | delete动作 | 删除索引不可恢复    | 开启  | 防止核心数据误删         | 拉长周期+快照备份，不随意关闭                        |
    
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
    
    ### 2）索引模板绑定ILM
    
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
12. 冷热分层仅优化存储成本，**无法解决热数据路由倾斜**，热节点必须做路由打散
13. shrink分片收缩不可逆，前期必须合理规划主分片数量
14. 流转存在十几分钟正常延迟，轮询+迁移+段合并导致卡点不精准
15. forcemerge IO消耗极高，尽量避开业务高峰期
16. 集群必须部署 `data_hot/data_warm/data_cold` 对应节点，缺失则流转卡住
17. 禁止手动修改索引 `data_tier`，易分片乱跑、集群变黄
18. freeze适合长期归档；频繁查询的冷索引建议关闭冻结
19. 自动删除风险高，核心数据提前配置快照备份，测试验证后上生产
    
    ## 6.6 ILM故障排查对照表
    
    | 现象                 | 根因                    | 排查命令                                  | 解决方案                |
    | ------------------ | --------------------- | ------------------------------------- | ------------------- |
    | 索引到期不流转，停留在hot     | 缺层级节点/分片搬迁阻塞/上阶段动作未完成 | GET 索引/_ilm/explain                   | 补齐节点；低峰调大迁移并发       |
    | Rollover不滚动，索引持续膨胀 | 写入未走别名、阈值设置过大         | GET _alias/别名                         | 修正写入别名；调整容量阈值       |
    | ILM流转集群IO飙升        | forcemerge+分片搬迁并发执行   | GET _cat/recovery?v                   | 高峰临时关闭rebalance均衡   |
    | 索引提前进入冷节点          | 混淆索引创建时间与业务数据时间       | GET 索引/_settings                      | 修正min_age，理清ILM计时逻辑 |
    | 索引冻结后查询变慢          | freeze牺牲性能换取内存占用      | GET 索引/_settings?filter_path=*.frozen | 有查询需求移除freeze       |
    | 索引被误删除             | 删除阶段min_age配置过小       | GET _ilm/policy/策略名                   | 拉长留存周期，临时注释delete   |
    
    ## 6.7 ILM常用运维命令
    
    ```http
    # 查看索引生命周期流转详情（核心排障）
    GET /index_name/_ilm/explain
    # 查看全局ILM轮询配置
    GET _cluster/settings?include_defaults=true&filter_path=indices.lifecycle*
    # 手动触发一轮ILM巡检
    POST _ilm/start
    POST _ilm/stop
    # 修改轮询间隔
    PUT _cluster/settings
    {
    "transient": {
    "indices.lifecycle.poll_interval": "10m"
    }
    }
    # 查看索引冷热分配标签
    GET index_name/_settings?filter_path=*.data_tier
    ```
    
    ## 6.8 ILM策略修改快速判断口诀
20. 写入波动 → 修改rollover容量/时长
21. 温层查询变化 → 启停forcemerge、调整shrink分片数
22. 集群内存紧张 → 冷索引全开freeze、温层适度缩分片
23. 磁盘成本紧张 → 缩短warm/cold/min_age提前进廉价盘
24. 业务留存变更 → 统一修改四阶段min_age
25. IO高峰期抖动 → 关闭forcemerge、错开流转时段
26. 流转快慢不合适 → 调整poll_interval巡检间隔

---

# 七、Reindex 数据重构全场景方案

## 前置核心结论

ILM、分片均衡、磁盘水位属于**常驻动态集群配置**，满足条件自动执行；
修改字段、修改分片数、修改路由规则属于**一次性架构变更**，ES无法原地修改，只能采用：
**新建索引 → Reindex迁移（支持Painless脚本转换）→ 业务双写保增量 → 原子别名切换 → 观察收尾**
整套流程无法依靠配置全自动完成。

## 7.1 场景一：全量修改字段名称、字段类型

### 不可原地修改清单

✅ 仅新增字段可原地PUT mapping
❌ 修改已有字段类型、字段重命名、修改分词器均无法原地执行，必须Reindex重建

### 完整零停机步骤

1. 梳理新旧字段映射关系
2. 创建新索引，定义全新Settings+Mapping+ILM
3. Reindex + Painless脚本实现字段改名、类型转换
   
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
4. 总条数、抽样数据一致性校验
5. 业务开启双写，避免迁移间隙增量丢失
6. 时间范围Reindex追赶增量数据
7. 原子别名切换无感切流
8. 平稳观察，停止双写，择机清理旧索引
   
   ### 风险避坑
   
   日期格式不匹配、空值脏数据提前判断兜底；超大索引限流分批迁移；切换后旧索引保留一段时间用于回滚。
   
   ## 7.2 场景二：重新分片、修改路由规则、全局打散热点
   
   ### 原理
   
   分片计算公式依赖主分片数量+路由Key，任意一个变更，原有分片分配全部失效；
   split/shrink仅倍数增减分片，**不能修改路由、不能打散热点倾斜**，唯一通用方案仍是Reindex重建。
   
   ### 完整步骤
9. 创建新索引：配置新主分片数量、新_routing规则、ILM、别名
10. Reindex脚本生成新路由值，数据全局重新哈希打散
    
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
11. 校验文档总量、分片分布均匀性
12. 业务双写、增量数据追赶同步
13. 原子切换别名
14. 观察平稳、停双写、归档旧索引
    
    ### 常见误区澄清
15. rebalance只会挪动分片节点位置，**不会改变文档路由归属**，解决不了分片内部数据倾斜
16. split/shrink无法重构路由、无法打散热点
17. ES无原地全局重哈希配置开关，重构只能走Reindex方案

---

# 八、索引别名架构设计 + 上线初始化标准模板

## 核心认知

1. 别名不是索引，是虚拟软链接，本身不存数据；真实索引带版本迭代（order_v1、order_v2），别名是长期固定业务访问入口。
2. 最佳实践：**索引创建之初就绑定别名，业务读写全程访问别名，禁止硬编码真实索引名**
3. 原子切换优势：单指令解绑旧+绑定新，无中间空档报错，回滚简单，无需改代码、无需发版重启。
   
   ## 8.1 索引初始化完整模板（索引+别名+ILM三合一，生产直接复用）
   
   ### 第一步：提前创建ILM策略（已创建可跳过）
   
   前文 `order_hot_warm_cold_policy` 策略不再重复粘贴
   
   ### 第二步：新建索引同时绑定业务别名
   
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
4. 增删改查统一使用 `alias_order`，杜绝硬编码 `order_v1`
5. 后续改字段、改分片、改路由重构，仅切换别名即可无感迭代
   
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
   # 查看ILM挂载情况
   GET order_v1/_settings?filter_path=*.lifecycle
   # 查看分片副本配置
   GET order_v1/_settings
   ```
   
   ## 8.5 别名设计总结
   
   上线初始化创建带版本物理索引，同步绑定固定业务别名，挂载ILM实现冷热自动流转；该架构天然适配后续Reindex各类重构，仅原子切换别名即可业务无感迁移，降低迭代与回滚成本。

---

# 九、全局分片异常统一排障手册

### 现象1：节点分片数量分布不均衡

原因：均衡阈值保守、搬迁并发偏低、节点磁盘水位偏高
处理：低峰调大rebalance并发、调低balance.threshold、清理高水位磁盘，等待自动收敛

### 现象2：集群黄/红，分片 unassigned 未分配

原因：磁盘超限、主副同节点约束、冷热层级不匹配、节点总数不足以容纳副本
排查：`GET _cluster/allocation/explain`
处理：清理磁盘、临时调低副本、补齐对应层级节点

### 现象3：分片搬迁速度极慢

原因：迁入迁出并发偏小、恢复带宽限速、节点IO负载高
处理：适度调高并发、放开带宽限流、避开业务高峰迁移

### 现象4：分片数量均衡，但单个分片数据爆满倾斜

原因：自定义routing热点倾斜，分配算法不感知分片内部数据体积
处理：路由二次打散、新建高分片索引Reindex迁移

### 现象5：高峰期分片搬迁打满IO

临时关闭全局自动均衡

```http
PUT _cluster/settings
{"transient":{"cluster.routing.allocation.allow_rebalance":"none"}}
```

### 现象6：冷热分片跨层级乱跑

原因：手动篡改索引data_tier、索引模板配置错乱
处理：重置索引层级绑定，核对节点角色配置

---

# 十、跨中间件统一分片思想（ES / MySQL / Redis / Kafka）

1. 分布式分片底层通用逻辑：**分片键 → 哈希/取模运算 → 绑定固定分片**
2. 分片键为热度不均匀字段（时间、地域、头部商户），必然产生数据倾斜
3. 最优架构方案
   - 首选高离散唯一主键（雪花ID）天然均衡
   - 业务必须使用热点维度做分片键，则进行后缀二次打散分流
4. 前期规划原则：分片预留余量，宁多勿少；后期扩容、重构、迁移成本极高
5. 成熟中间件共性：参数支持动态配置、内置均衡自愈、生命周期管控，业务长期近乎零维护；自研分片体系开发、踩坑、运维成本极高，中小企业性价比极低
   补充：主流中间件大多由大厂海量业务打磨验证边界场景；小公司不适合从零自研分布式分片架构，务实路线吃透开源中间件原理、适度二次封装适配业务即可。

---

# 十一、面试3分钟整体总结背诵版

ES分片分为路由数据倾斜、节点分片分配倾斜两类。路由依靠MurmurHash3 32位哈希取模分配分片，主分片创建后不可修改，哈希碰撞不会覆盖数据；省份、日期自定义路由极易热点倾斜，解决方案为路由Key后缀二次打散。
节点分片依靠IndexBalance、ShardBalance加权算法实现分片数量均衡，受磁盘三级水位、主副隔离、冷热层级三大前置约束管控分配逻辑；容量上单分片控制30~50GB，前期分片规划宁多勿少。
冷热分层依托ILM实现索引单向Hot→Warm→Cold→Delete流转，仅依据索引创建/滚动时间判断，不识别文档业务时间；采用后台轮询调度，对比自研Cron定时任务流量更平滑、自带幂等状态、异常自愈更强，仅修改索引data_tier标签完成层级迁移，只能节约存储成本，无法治理热分片倾斜。
字段改名改类型、重新分片、修改路由规则均无法原地变更，统一采用**新建索引+Reindex脚本迁移+业务双写+原子别名切换**方案；业务上线初期即规划固定访问别名，底层索引迭代重构无需修改业务代码，做到无感切换、回滚便捷。ILM、分片均衡属于集群常驻自动配置，而Reindex重构属于一次性人工运维变更流程，无法全自动配置执行。
该分片思想通用于MySQL分库分表、Redis集群、Kafka分区；成熟中间件动态配置、内置自愈能力，运维成本更低，中小企业优先用好开源方案，不盲目自研分布式分片轮子。
