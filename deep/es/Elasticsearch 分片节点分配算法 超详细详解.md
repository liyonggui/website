---
title: "Elasticsearch 分片节点分配算法 超详细详解"
summary: "覆盖：分配整体流程、分片分配四大核心算法、平衡策略、分配约束、倾斜成因、要点，结合你前面冷热分层、分片倾斜知识点打通。"
date: 2026-07-13
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 分库分表
category: deep
series: "es"
slug: "Elasticsearch 分片节点分配算法 超详细详解"
reading_time: 23
featured: false
draft: false
---
# Elasticsearch 分片节点分配算法 超详细详解

覆盖：分配整体流程、分片分配四大核心算法、平衡策略、分配约束、倾斜成因、要点，结合你前面冷热分层、分片倾斜知识点打通。

## 一、前置基础概念

### 1. 什么是分片分配

主分片、副本分片在集群节点之间**选择存放节点、搬迁、新建、恢复**的全过程，由集群内置调度器 `AllocationService` 统一控制。
三大行为：

1. **分配（allocate）**：分片找不到节点，选节点放上去
2. **搬迁（move）**：分片从A节点迁移到B节点
3. **重平衡（rebalance）**：集群整体不均衡，自动挪动分片抹平差距
   
   ### 2. 核心原则（硬性约束，优先级最高）
4. **主副分片不能同节点**：防止单机宕机同时丢主+副本
5. 同一索引多个副本尽量打散到不同节点，提升容灾
6. 磁盘水位超限节点禁止迁入分片
7. 机架感知/IP感知约束（开启后跨机架部署）
   
   ### 3. 整体决策链路
   
   获取集群分片状态 → 校验所有分配约束 → 计算所有候选节点打分 → 择优分配/搬迁

---

# 二、ES 分片分配四大核心算法（ES7/8 通用）

配置入口（动态可改）

```http
PUT _cluster/settings
{
 "transient": {
 "cluster.routing.allocation.balance.index": 0.55,
 "cluster.routing.allocation.balance.shard": 0.45,
 "cluster.routing.allocation.balance.threshold": 1.0
 }
}
```

总分公式：
`节点综合得分 = 索引均衡得分 × 权重A + 分片均衡得分 × 权重B`
得分**越低越优先选中**（ES设计逻辑：压力越小分数越低）

## 1. Index Balance 索引均衡算法（权重默认 0.55）

### 作用

控制**同一个索引的分片均匀散布在各个节点**，避免一个节点塞满某一个索引大量分片。
计算公式：

> 单个节点该索引分片数量 − 集群节点该索引分片平均值 = 索引不均衡差值
> 差值越大，节点得分越高，越不适合分配新分片。
> 举例：
> 索引总9主9副本，共18分片，3节点
> 平均每个节点应承载：6 个分片
> 节点A：承载10个 → 差值+4，分数偏高，尽量不再分配该索引分片
> 节点B：承载5个 → 差值-1，分数低，优先分配

### 适用场景

防止单个节点被某个大索引占满资源。

## 2. Shard Balance 全局分片均衡算法（权重默认 0.45）

### 作用

**全局所有索引总分片在节点间均衡**，抹平节点整体分片总量差距。
公式：

> 节点总分片数 − 集群单节点平均总分片数 = 全局分片差值
> 节点总分片越多，得分越高，尽量不再分配新分片。
> 举例：
> 3节点集群总分片30，均值10
> 节点1：16分片（分数高，拒绝分配）
> 节点2：9分片（分数低，优先分配）

## 3. Disk Threshold Balance 磁盘容量均衡算法（隐性强优先级）

不是加权打分，是**准入门槛前置判断**，优先级高于上面两个均衡算法。
三级水位：

1. low 低水位（默认85%）：开始尝试把分片迁出该节点
2. high 高水位（默认90%）：禁止新分片迁入，只能迁出
3. flood_stage 洪水水位（默认95%）：该节点所有分片只读，拒绝任何写入，防止磁盘打满系统崩溃
   逻辑：
   候选节点磁盘占用超过阈值 → 直接剔除候选列表，不参与打分。
   这也是很多分片不均衡最常见原因：部分节点磁盘快满，分片只能往剩余少数节点扎堆。
   
   ## 4. Awareness Balance 机架/IP感知分配算法（约束型分配策略）
   
   开启配置：
   
   ```yaml
   cluster.routing.allocation.awareness.attributes: rack_id
   ```
   
   ### 核心逻辑
4. 强制主、副本分片分布在**不同机架**
5. 分片尽量在多机架均匀分摊，单机架故障不会整体丢副本
6. 机架分片数量差距过大时，触发跨机架分片搬迁平衡
   
   ### 典型问题
   
   机架数量少、节点分布不均，会导致分片无法充分均衡，出现局部倾斜。

---

# 三、分片分配完整决策步骤（源码执行顺序）

1. **过滤阶段（筛选可用节点）**
   - 节点离线、故障、冻结 → 剔除
   - 磁盘水位超标节点 → 剔除
   - 满足主副不能同节点约束校验
   - 满足机架/IP感知约束
   - 节点角色不匹配（冷热分层 data_hot/data_warm/data_cold）剔除
     
     > 你前面冷热分层关键：温分片不能分配到hot节点
2. **打分阶段（对剩余候选节点计算综合分数）**
   `总分 = index_balance × 0.55 + shard_balance × 0.45`
3. **择优分配**
   分数最低（负载最轻）的节点作为目标节点，执行分片分配/搬迁
4. **全局再平衡判断**
   集群整体不均衡度超过 `balance.threshold（默认1.0）`，后台自动触发分片搬迁重平衡
   
   ## 不均衡阈值通俗解释
   
   threshold=1.0：节点分片偏差比例超过100%才触发搬迁；
   调小更容易平衡，但搬迁变多IO压力变大；调大平衡惰性更强。

---

# 四、主分片 & 副本分片分配差异化规则

## 1. 主分片选举规则

1. 集群状态正常时，沿用原有主分片位置
2. 原主分片宕机：从现有副本中**优先级选举新主分片**
3. 分片分配算法只负责选存放位置，不负责主身份选举
   
   ## 2. 副本分配硬性铁律
4. 副本绝对不能和对应主分片同一个节点
5. 多副本尽量打散不同节点、不同机架
6. 冷热分层场景：主在hot，副本也会跟随层级约束
   
   ## 3. 分片搬迁并发控制（运维常用）
   
   ```yaml
   # 单个节点同时最多迁入几个分片
   cluster.routing.allocation.node_concurrent_incoming_recoveries: 2
   # 单个节点同时最多迁出几个分片
   cluster.routing.allocation.node_concurrent_outgoing_recoveries: 2
   # 集群整体并行重平衡分片数量
   cluster.routing.allocation.cluster_concurrent_rebalance: 2
   ```
   
   高峰期调小防IO打满，低峰期调大加速均衡。

---

# 五、两种倾斜区分：路由倾斜 vs 节点分配倾斜（极易混淆，高频）

## 类型1：**业务Routing分片倾斜（分片内部数据不均）**

成因：自定义routing热点key（省份、日期、商家），哈希固定一个分片，单分片数据体积巨大
分配算法**解决不了这类倾斜**，属于上层路由设计问题
解决：路由二次打散、重建多分片索引

## 类型2：**节点分配不均衡（分片个数摆放不均）**

成因：分片分配算法、磁盘水位、机架约束、节点上下线导致节点总分片数量差距大
表现：有的节点分片十几个，有的很少
解决：调整平衡权重、磁盘水位、均衡并发，等待ES自动rebalance

> 关键总结：
> 分片分配算法只管**分片放在哪个节点、分片个数均衡**；
> **不管分片内部数据多少**，管不了routing带来的数据容量倾斜。

---

# 六、冷热分层下的分片分配特殊逻辑（匹配你前面方案）

1. 新建滚动索引默认分配到 `data_hot` 节点
2. ILM流转到warm阶段：分配约束强制 `data_tier=warm`，分配算法只在温节点池打分选择
3. 进入cold阶段：仅冷节点参与分配候选
4. 不同层级之间分片互不跨池分配，天然冷热隔离
5. 热点倾斜只需要在hot层做路由打散，warm/cold层倾斜无业务影响

---

# 七、常见分配异常排查命令

```http
# 查看分片为什么无法分配、搬迁原因（最核心排查指令）
GET _cluster/allocation/explain
# 查看集群均衡设置
GET _cluster/settings?include_defaults=true&filter_path=*.balance*,*.disk*
# 查看每个节点分片数量统计
GET _cat/nodes?v&h=name,shards,heapPercent,diskPercent
# 查看分片分配延迟设置（节点重启短暂延迟分配防雪崩）
GET _cluster/settings?include_defaults=true&filter_path=*.delay*
```

## 典型异常举例

1. `too many shards`：节点分片过多，index balance得分过高拒绝分配
2. `disk watermark breached`：磁盘水位触发，节点被排除
3. `same allocation id`：主副分片不能同节点约束拦截
4. `tier requirement mismatch`：冷热层级不匹配无法分配

---

# 八、极简总结

1. ES分片分配由AllocationService管控，分**过滤筛选→打分择优→分配搬迁**三步；
2. 两大核心打分算法：IndexBalance（单索引分片均衡）、ShardBalance（全局总分片均衡），带固定权重；磁盘水位、机架感知、节点角色是前置准入过滤条件；
3. 分配算法只均衡**分片在节点的摆放数量**，无法解决自定义routing带来的分片内部数据容量倾斜；
4. 副本强制不和主分片同节点，冷热分层依靠节点角色过滤实现分片池隔离；
5. 集群不均衡超过阈值自动rebalance，可通过并发参数控制搬迁速度，避免IO冲击业务。
   

# Elasticsearch 分片分配异常排查&排障步骤手册

适用场景：分片数量分布不均、分片无法分配、分片搬迁卡住、集群不均衡、冷热分层分片异常、分片unassigned、磁盘倾斜等，线上可直接按现象对照排查处理

## 前置通用排查入口（所有问题先执行这3条）

### 1、查看集群整体分片状态

```http
GET _cat/shards?v
# 筛选指定索引
GET _cat/shards/索引名?v
# 按分片数量排序，快速看出节点分片多少
GET _cat/nodes?v&h=name,shards,diskPercent,heapPercent,node.role
```

### 2、分片无法分配/未分配原因最核心命令

```http
# 解释单个未分配分片
GET _cluster/allocation/explain
# 指定索引查看详细分配原因
GET /索引名/_cluster/allocation/explain
```

### 3、查看集群均衡、磁盘水位默认配置

```http
GET _cluster/settings?include_defaults=true&filter_path=*.balance*,*.disk*,*.allocation.node_concurrent*,*.delay*
```

---

# 一、现象分类+原因定位+分步解决方案

## 现象1：节点总分片数量差距极大（分片摆放不均衡，分配算法收敛慢）

### 典型表现

3节点、9主1副总分片18，理论每节点6片；
节点A：10片、节点B：5片、节点C：3片，长期不平衡

### 根因

1. 分片重平衡阈值偏大，ES不愿主动搬迁分片（默认threshold:1.0）
2. 分片搬迁并发过低，均衡速度极慢
3. 个别节点磁盘占用偏高，被打分压低，分配器尽量不往上面放分片
4. 节点上下线、扩容缩容后，均衡未跑完
   
   ### 排障步骤
5. 先检查磁盘水位是否异常
   
   ```http
   GET _cat/nodes?v&h=name,diskPercent
   ```
   
   若某节点磁盘接近高水位，先清理磁盘或扩容磁盘。
6. 临时调高均衡并发（业务低峰执行，高峰期不宜过大）
   
   ```http
   PUT _cluster/settings
   {
   "transient": {
   "cluster.routing.allocation.cluster_concurrent_rebalance": 4,
   "cluster.routing.allocation.node_concurrent_incoming_recoveries": 3,
   "cluster.routing.allocation.node_concurrent_outgoing_recoveries": 3
   }
   }
   ```
7. 适当收窄均衡阈值，加快收敛
   
   ```http
   PUT _cluster/settings
   {
   "transient": {
   "cluster.routing.allocation.balance.threshold": 0.8
   }
   }
   ```
8. 等待一段时间观察，均衡完成后回调并发参数，避免常态大量搬迁消耗IO
   
   ### 规避方案
   
   节点硬件配置统一，上线初期规划分片数，避免频繁上下节点。
   
   ## 现象2：大量分片 unassigned 未分配（黄色/红色集群）
   
   ### 常见细分原因&对应处理
   
   #### 原因1：主副本禁止同节点，节点数量不足以放置副本
   
   示例：索引9主1副本，总共需要至少2个节点；集群只剩单节点 → 副本全部无法分配
   处理：扩容节点；或临时调低副本数应急
   
   ```http
   PUT 索引/_settings
   {"number_of_replicas":0}
   ```
   
   #### 原因2：节点磁盘触发高水位/洪水水位，拒绝迁入分片
   
   `allocation explain` 提示：disk watermark breached
   处理：
9. 清理节点无用索引、快照释放空间
10. 临时微调磁盘水位（长期建议扩容磁盘）
    
    ```http
    PUT _cluster/settings
    {
    "transient": {
    "cluster.routing.allocation.disk.watermark.low":"88%",
    "cluster.routing.allocation.disk.watermark.high":"92%",
    "cluster.routing.allocation.disk.watermark.flood_stage":"96%"
    }
    }
    ```
    
    #### 原因3：节点重启分配延迟（默认1分钟），等待时间未到
    
    报错提示：delayed allocation
    查看延迟配置：
    
    ```http
    GET _cluster/settings?include_defaults=true&filter_path=unassigned.delayed
    ```
    
    可缩短延迟加速分配（谨慎）
    
    ```http
    PUT _cluster/settings
    {"transient":{"cluster.routing.allocation.unassigned.delayed.allocation_timeout":"30s"}}
    ```
    
    #### 原因4：冷热分层层级不匹配，分片只能进hot但只剩warm/cold节点
    
    提示：tier requirement mismatch
    处理：
11. 检查索引绑定data_tier配置
12. 补齐对应角色节点；或修改索引分配约束临时兼容
    
    ```http
    PUT 索引/_settings
    {
    "index.routing.allocation.require.data_tier": null
    }
    ```
    
    #### 原因5：分片数量上限限制 `cluster.max_shards_per_node` 打满
    
    单个节点分片超限，拒绝新分片放入，调高全局阈值
    
    ```http
    PUT _cluster/settings
    {"transient":{"cluster.max_shards_per_node":1500}}
    ```
    
    ## 现象3：分片搬迁特别慢，rebalance卡住半天不动
    
    ### 根因
13. 迁入/迁出、集群重平衡并发设置太小
14. 索引段合并限流、传输节流限速
15. 磁盘IO打满，系统负载高，ES主动节流自保
    
    ### 处理步骤
16. 调大搬迁并发（低峰操作）
    
    ```http
    PUT _cluster/settings
    {
    "transient": {
    "cluster.routing.allocation.node_concurrent_incoming_recoveries":4,
    "cluster.routing.allocation.node_concurrent_outgoing_recoveries":4,
    "cluster.routing.allocation.cluster_concurrent_rebalance":4
    }
    }
    ```
17. 检查恢复传输限速，适度放开
    
    ```http
    PUT _cluster/settings
    {"transient":{"indices.recovery.max_bytes_per_sec":"80mb"}}
    ```
18. 观察节点CPU、iowait，若硬件瓶颈则不能盲目调大并发，只能拉长均衡周期
    
    ## 现象4：同一个索引分片扎堆某个节点（Index Balance失衡）
    
    ### 表现
    
    某个节点承载该索引分片远多于其他节点，其他节点偏少
    
    ### 根因
    
    索引均衡权重、分片分配打分偏向；节点磁盘差异导致打分偏差
    
    ### 处理
19. 微调两大均衡权重（默认index=0.55，shard=0.45）
    
    ```http
    PUT _cluster/settings
    {
    "transient": {
    "cluster.routing.allocation.balance.index": 0.65,
    "cluster.routing.allocation.balance.shard": 0.35
    }
    }
    ```
    
    增大index权重，ES会更优先保证单索引分片均匀散布
20. 确认无磁盘水位阻拦，等待自动rebalance收敛
    
    ## 现象5：冷热分层分片乱跑，热分片跑到冷节点、温分片进hot节点
    
    ### 根因
21. ILM流转后分配require约束未正确设置
22. 索引模板data_tier配置错乱
23. 手动移动分片绕过层级约束
    
    ### 排障步骤
24. 查看索引当前分层约束
    
    ```http
    GET 索引/_settings?filter_path=*.data_tier
    ```
25. 修正索引强制分配节点层级
    
    ```http
    # 强制限定只在hot节点
    PUT 索引/_settings
    {
    "index.routing.allocation.require.data_tier":"hot"
    }
    ```
26. 检查节点角色配置是否正确：`node.roles: [data_hot/data_warm/data_cold]`
27. ILM策略重新校验流转配置，必要时重建ILM策略
    
    ## 现象6：分片内部数据容量倾斜（分片个数均衡，但单个分片几十GB，其他很小）
    
    > 重点区分：**分配算法管分片个数均衡，管不了分片内部数据量倾斜**
    
    ### 根因
    
    自定义routing绑定热点维度（省份、商家、日期），热点key固定一个分片，属于业务路由设计问题，不是分片分配bug
    
    ### 解决方案
28. 短期：路由二次打散，给业务key追加后缀分流
29. 中期：新建更多主分片索引，Reindex迁移数据
30. 时序数据优先滚动索引+ILM，避免单索引持续膨胀
    
    ## 现象7：开启机架感知后分片极度不均衡、部分分片无法分配
    
    ### 根因
    
    机架节点数量不对称、副本数过多，跨机架放置条件无法满足
    
    ### 排查
31. 查看感知配置
    
    ```http
    GET _cluster/settings?include_defaults=true&filter_path=awareness
    ```
32. 核对机架节点数量、副本数量是否匹配
33. 要么补充对应机架节点，要么降低副本数，或临时关闭感知调试

---

# 二、应急开关（业务高峰期防雪崩常用）

## 1、临时关闭集群分片重平衡（高峰期防止IO打崩）

```http
PUT _cluster/settings
{
 "transient": {
 "cluster.routing.allocation.allow_rebalance":"indices_all_active"
 }
}
```

可选值：

- always：随时均衡
- indices_primaries_active：仅主分片全部就绪才均衡（默认）
- indices_all_active：所有分片就绪才均衡
- none：完全禁止rebalance
  
  ## 2、全局临时禁止分片分配（紧急维护）
  
  ```http
  PUT _cluster/settings
  {"transient":{"cluster.routing.allocation.enable":"none"}}
  # 恢复
  PUT _cluster/settings
  {"transient":{"cluster.routing.allocation.enable":"all"}}
  ```
  
  可选：none / new_primaries / all / primaries
  
  ## 3、手动移动分片（极端不均衡人工干预）
  
  ```http
  POST _cluster/reroute
  {
  "move": {
  "index": "索引名",
  "shard": 分片号,
  "from_node": "源节点名",
  "to_node": "目标节点名"
  }
  }
  ```

---

# 三、事后预防规范（避免反复出现分配异常）

1. 新建索引提前规划主分片数量，前期预留余量，避免后期扩容重构
2. 节点硬件同构，磁盘容量、机型统一，减少磁盘水位带来分配偏差
3. 自定义routing场景必须做热点打散，区分「分片摆放不均衡」和「分片内部数据倾斜」
4. 冷热分层严格绑定索引模板+ILM，不要手动修改分片层级
5. 业务高峰收小分片搬迁并发，低峰放开均衡提速
6. 定期监控磁盘水位、分片分布，提前扩容预警
   
   # 四、精简总结
7. 分片分配失衡分两类：**分片摆放数量不均（分配算法可调节解决）、分片内部数据容量倾斜（路由架构问题）**；
8. 排查优先用 allocation explain 定位未分配真实原因，磁盘水位、主副本同节点、冷热层级、节点分片上限是四大高频诱因；
9. 可通过均衡权重、搬迁并发、均衡阈值调节收敛速度，高峰期可临时关闭rebalance防IO冲击；
10. 分配算法只管分片放在哪个节点，解决不了自定义routing热点倾斜。
    
    
    
    
    
