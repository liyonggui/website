---
title: "ES 路由分片 · ILM 冷热分层 · Reindex 重构 · 别名 · Rollover 时序索引（整合手册）"
summary: "1. 索引/分片/节点层级基础关系"
date: 2026-07-31
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 索引
  - 分库分表
  - ILM
category: deep
series: "es"
slug: "ES路由分片+ILM冷热分层+Reindex重构+别名+Rollover时序索引 完整版整合手册"
reading_time: 24
featured: false
draft: false
---
# ES路由分片\+ILM冷热分层\+Reindex重构\+别名\+Rollover时序索引 完整版整合手册

## 目录

1. 索引/分片/节点层级基础关系

2. 路由寻址原理 \& MurmurHash3 哈希机制

3. 两类分片倾斜成因与路由二次打散方案

4. 分片分配算法、磁盘水位、搬迁管控

5. 索引\&节点容量生产规划标准

6. ILM 冷热分层完整手册（原理、参数对照表、轮询vs定时任务、故障排查、运维命令）

7. Reindex 数据重构全场景方案

8. 索引别名架构设计 \+ 普通索引上线初始化标准模板

9. Rollover 时序滚动索引 完整初始化模板（日志/流水专用）

10. **ILM反向回流：温层数据回滚至热节点（手动\+批量脚本）**

11. 全局分片异常统一排障手册

12. 跨中间件统一分片思想（ES / MySQL / Redis / Kafka）

13. 三分钟整体总结

---

# 一、索引 / 分片 / 节点 层级关系

## 层级结构

集群 Cluster → 多个索引 Index → 索引包含主分片\+副本分片 → 分片部署在节点 Node → 节点依托服务器磁盘、内存、CPU

## 两条核心铁律

1. **一个分片永久只归属一个索引**，不能跨索引存储数据；不同索引分片可同名 shard\-0，物理文件完全隔离。

2. **同一个分片的主分片、副本分片禁止部署在同一节点**，防止单机宕机同时丢失主副副本。

## 示例：3节点，order索引 9主1副本（总分片18）

总分片公式：
$\text{总分片}=主分片数\times(副本数+1) = 9\times 2 = 18$

理论均分：3节点，每个节点承载该索引6个分片

```plaintext
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

# 二、路由寻址底层原理 \& MurmurHash3

## 1、路由计算公式（底层固定）

```java
String routingKey = 自定义routing != null ? 自定义routing : 文档_id;
int hash = Murmur3HashFunction.hash(routingKey);
int shardId = Math.floorMod(hash, number_of_primary_shards);   // 必须 floorMod：Math.abs(Integer.MIN_VALUE) 仍为负
```

### 硬性约束

1. `number_of_primary_shards`**索引创建后永久不可修改**，修改会导致全部路由错乱，数据无法查询

2. 副本数量 `number_of_replicas`可动态调整，不影响路由逻辑

3. 同一个路由Key，分配分片终身固定

## 2、MurmurHash3 两个版本分工

| 版本                    | 位数   | 使用场景              | 说明            |
| --------------------- | ---- | ----------------- | ------------- |
| MurmurHash3\_x86\_32  | 32位  | 分片路由计算            | 打散有序ID；哈希碰撞无害 |
| MurmurHash3\_x64\_128 | 128位 | cardinality基数去重聚合 | 极低碰撞，统计精准     |

### 哈希碰撞说明

不同 routing 算出相同哈希进入同一分片属于正常现象；
分片内部依靠 `_id` 区分文档，**不会发生数据覆盖、更新错乱**；哈希目的是打散有序数据，不是彻底消除碰撞。

## 3、Routing 字符串长度规范

1. 不是越长打散越好：哈希结果固定32位，超长字符串只会增加CPU开销，易触发URL超长报错

2. 不是越短越好：字符过少样本空间小，容易哈希扎堆倾斜

3. 生产最优长度：**6\~30个字符**

## 4、路由打散与MySQL兼容性

路由二次打散仅修改 `_routing` 字段，文档 `_id`、业务主键不变，ES与MySQL可依靠唯一主键正常关联。

---

# 三、两类分片倾斜成因 \+ 路由二次打散解决方案

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
        int suffix = Math.floorMod(Long.hashCode(snowId), splitNum);
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

1. **IndexBalance（权重0\.55）**：控制同一个索引分片在节点均匀分布，避免单节点塞满某索引大量分片

2. **ShardBalance（权重0\.45）**：控制集群所有索引总分片整体均衡

均衡阈值 `cluster.routing.allocation.balance.threshold` 默认 1\.0，偏差超出阈值才触发 rebalance；调小均衡更积极、IO更多，调大收敛惰性更强。

## 3、磁盘三级水位（分配强门槛）

- low\(85%\)：开始主动迁出分片

- high\(90%\)：禁止新分片迁入

- flood\_stage\(95%\)：节点所有索引只读，防止磁盘打满宕机

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

# 五、索引\&节点容量规划规范（生产落地标准）

## 1、单分片合理容量（核心规划依据）

- 热数据高写入高频查询：**20\~35GB**

- 常规业务索引通用最优：**35\~50GB**

- 冷归档极少查询：上限放宽 60\~80GB

\> 分片过大：迁移、段合并、故障恢复、快照恢复速度极慢

## 2、主分片数量计算公式

`建议主分片数 ≈ 预估未来3年总数据量 ÷ 50GB`
原则：**分片宁多勿少**，主分片创建后不可修改，前期预留余量，避免后期重构迁移。

## 3、副本配置说明

示例：9主 \+ 副本数=1 → 9主9副本，总分片18，一对一主副配对；副本数量可随时动态调整。

## 4、节点磁盘使用率红线（冷热区分）

- hot热节点\(SSD\)：使用率 ≤75%

- warm温节点：使用率 ≤80%

- cold冷节点\(HDD\)：使用率 ≤82%

预留空间应对分片搬迁、段合并、数据突增，规避磁盘水位异常。

## 5、单节点分片总数上限

- 保守阈值：单节点总分片 ≤ 800

- 警戒上限：≤ 1000

分片过多元数据占用堆内存暴涨，集群管控压力激增。

## 6、单个索引总体积上限

- 中小集群\(3\~5节点\)：单索引总容量 ≤1\.5TB

- 中大型集群：单索引 ≤3TB

时序数据禁止单索引长期膨胀，采用 Rollover\+ILM 滚动拆分。

---

# 六、ILM 冷热分层完整手册

## 6\.1 底层核心原理

1. **流转最小单位：整个索引，不支持单条文档单独冷热迁移**

2. 计时基准（高频误区）
   
            \> ILM 不会读取文档内部业务时间字段做冷热判断
   
   - 普通静态索引：计时起点 = 索引创建时间，与文档 `create_time/update_time` 无关
   
   - Rollover滚动索引：计时起点 = 索引滚动切换时刻

3. 调度模式：内置 `ilm-service` 后台轮询线程，**非 Cron 定时任务**，默认巡检间隔10分钟

4. 流转单向不可逆：`Hot → Warm → Cold → Delete`，无原生自动回流能力

5. 流转本质：修改索引 `_tier_preference` 层级约束，分片分配器自动搬迁分片至对应节点池

## 6\.2 ILM轮询 vs 自定义定时任务 对比

结论：ILM轮询稳定性、容错、集群可控性全面优于自研Cron定时任务

1. 负载：轮询分散触发无瞬时尖峰；Cron整点批量搬迁极易打满IO

2. 幂等：ILM内置状态机，阶段未完成不往下执行；自研需自行实现幂等、重试、状态记录

3. 资源管控：复用集群原生分片搬迁限流配置；定时任务需额外开发限流逻辑

4. 异常自愈：迁移失败下一轮自动重试；定时任务失败需要人工排查补偿

5. 资源开销：轮询仅遍历索引元数据，空闲开销极低；定时任务常驻调度维护成本高

6. 唯一短板：存在最多一个轮询周期时间误差；Cron可精准整点执行

## 6\.3 ILM 四阶段参数速查表（含义\+默认\+修改场景\+推荐值）

### 全局ILM配置

| 参数                                 | 作用        | 默认  | 修改场景            | 推荐值                        |
| ---------------------------------- | --------- | --- | --------------- | -------------------------- |
| indices\.lifecycle\.poll\_interval | ILM全局巡检间隔 | 10m | 测试验证、时效敏感、大集群减负 | 测试：1m；时效：3\~5m；大集群：15\~20m |

### Hot 热阶段

| 参数                                  | 作用           | 默认   | 修改场景                  | 推荐值                      |
| ----------------------------------- | ------------ | ---- | --------------------- | ------------------------ |
| min\_age                            | 进入热阶段等待时长    | 0ms  | 一般不修改                 | 固定0ms                    |
| rollover\.max\_primary\_shard\_size | 单主分片容量阈值自动滚动 | 45gb | 写入量偏大/偏小，规范分片30\~50GB | 高吞吐：30\~35gb；常规：40\~50gb |
| rollover\.max\_age                  | 索引最长写入周期强制滚动 | 7d   | 调整索引拆分粒度、磁盘压力调节       | 按天拆分：3\~7d；低流量：10\~15d   |
| set\_priority                       | 分片故障恢复优先级    | 100  | 核心业务索引优先恢复            | 普通100，核心110\~120         |

### Warm/Cold/Delete阶段表格省略（和前文一致）

## 6\.4 ILM故障排查、运维命令、策略模板（全文保留原有内容，此处省略，和上文完全一致）

---

# 七、Reindex 数据重构全场景方案

（全文原有内容完整保留，此处省略）

---

# 八、索引别名架构设计 \+ 普通索引上线初始化标准模板

（全文原有内容完整保留，此处省略）

---

# 九、Rollover 时序滚动索引 完整初始化模板（日志/流水/时序订单专用）

（全文原有内容完整保留，此处省略）

---

# 十、ILM反向回流：温层数据回滚至热节点（手动\+批量脚本，新增完整版）

## 10\.1 核心前置认知

**ILM天生单向流转：Hot→Warm→Cold→Delete，无任何自动回流机制**，ES不会自动把温/冷索引迁回热节点；
想要反向回滚，只能**人工修改索引冷热标签，强制触发分片回迁**，同时必须规避ILM巡检再次自动迁回。

## 10\.2 单索引手动回滚步骤（生产精准操作，分步执行）

### 步骤1：查询索引当前冷热层级

```http
GET 你的索引名/_settings?filter_path=*.settings.index.routing.allocation.include._tier_preference
```

### 步骤2：强制修改索引归属热节点，自动触发分片搬迁

```http
PUT 你的索引名/_settings
{
  "index.routing.allocation.include._tier_preference": "data_hot"
}
```

执行后ES自动检测分片节点不匹配，后台自动执行分片迁移，无需手动干预分片搬迁。

### 步骤3：查看分片回迁进度

```http
GET _cat/recovery?v&h=index,shard,stage,source_node,target_node,percent
```

percent=100 代表分片全部回迁完成。

### 步骤4：关键防护：防止ILM巡检再次回迁温层

索引依旧绑定原有ILM策略，到达温层最小存活时间后，会再次被ILM自动迁回warm，必须做防护：

```http
# 暂停单个索引生命周期，停止自动流转（推荐精细化管控）
POST 你的索引名/_ilm/stop

# 后续业务稳定，需要恢复自动冷热流转再开启
POST 你的索引名/_ilm/start
```

### 步骤5：额外兼容处理（温索引常见变更）

1. 温层索引默认只读，如需重新写入，解除只读锁：
   
            `PUT 你的索引名/_settings
   
   {
   "index.blocks.write": null
   }
   `

2. 索引做过shrink缩分片：**只能回迁节点位置，无法自动恢复原有分片数量**，如需恢复原始分片规格，只能走Reindex重建

3. forcemerge段合并：合并结果永久保留，不影响分片回迁和读写性能

## 10\.3 生产一键批量脚本：所有温层索引自动回迁热节点（开箱即用）

适用场景：大批量业务索引误进入温层，需要一次性全部回退热节点，自动过滤系统内置索引（\.开头索引）。

### warm\_to\_hot\.sh 完整shell脚本

```bash
#!/bin/bash
# ES温层索引批量回迁热节点脚本
# 功能：自动查询所有warm层索引、过滤系统索引、批量修改 _tier_preference、附带操作提示

# ========== 请自行修改此处配置 ==========
ES_HOST="http://127.0.0.1:9200"
# 集群开启账号密码则取消注释，填写账号密码
# ES_USER="elastic"
# ES_PWD="xxxxxx"
# =======================================

echo "========== ES温层索引批量回迁热节点 开始执行 =========="
echo "1. 正在查询集群所有warm层级业务索引（过滤系统内置索引）"

# 获取所有warm索引，过滤.开头系统索引
if [ -n "$ES_USER" ];then
  INDEX_LIST=$(curl -s -u $ES_USER:$ES_PWD "$ES_HOST/_settings/index.routing.allocation.include._tier_preference?pretty" | jq -r 'to_entries[] | select((.value.settings.index.routing.allocation.include._tier_preference // "") | contains("data_warm")) | select(.key | startswith(".") | not) | .key')
else
  INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.include._tier_preference?pretty" | jq -r 'to_entries[] | select((.value.settings.index.routing.allocation.include._tier_preference // "") | contains("data_warm")) | select(.key | startswith(".") | not) | .key')
fi

# 判断是否存在warm索引
if [ -z "$INDEX_LIST" ];then
  echo "✅ 集群无待回迁的warm业务索引，脚本直接退出"
  exit 0
fi

# 打印待处理索引
echo "✅ 待批量回迁索引列表："
echo "$INDEX_LIST"
echo "---------------------------------------------------------"

# 循环批量修改冷热标签
while read index; do
  [ -z "$index" ] && continue
  echo "正在处理索引：【$index】"
  JSON_BODY='{"index.routing.allocation.include._tier_preference":"data_hot"}'
  if [ -n "$ES_USER" ];then
    curl -s -u $ES_USER:$ES_PWD -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  else
    curl -s -X PUT "$ES_HOST/$index/_settings" -H "Content-Type: application/json" -d "$JSON_BODY"
  fi
  echo -e "\n✅ $index 已切换为hot热层级，分片开始自动回迁\n"
done <<< "$INDEX_LIST"

echo "========== 全部索引批量回迁指令执行完成 =========="
echo ""
echo "👉 查看分片回迁实时进度：curl $ES_HOST/_cat/recovery?v"
echo "👉 防止ILM自动回迁：POST /索引名/_ilm/stop"
echo "👉 恢复生命周期自动流转：POST /索引名/_ilm/start"
```

### 脚本使用步骤

1. 安装依赖：`yum install jq -y`（解析json必备）

2. 修改脚本内ES地址、账号密码

3. 授权\+运行：`chmod +x warm_to_hot.sh && ./warm_to_hot.sh`

## 10\.4 两种回流方案选型对比

| 方案                | 适用场景               | 优点            | 缺点               |
| ----------------- | ------------------ | ------------- | ---------------- |
| 临时暂停单索引ILM        | 个别索引临时回热，后续仍要走生命周期 | 精细化控制，不影响其他索引 | 需要单独操作每个索引       |
| 修改ILM策略拉长min\_age | 全局所有索引延后进入温层       | 一次修改全局生效      | 影响全集群索引生命周期，风险较高 |

## 10\.5 极简要点（速记）

ES的ILM冷热流转是单向的，不支持自动从温层回退到热节点。如果温层数据需要回滚，核心操作是手动修改索引的data\_tier标签为hot，触发ES分片分配器自动将分片迁回热节点；回迁完成后必须暂停该索引的ILM生命周期，否则下一轮ILM巡检会再次把索引迁回温层。另外做过shrink缩分片的索引只能迁移节点，无法恢复原有分片数量，想要还原分片结构只能通过Reindex重建。大批量索引回流可以使用shell脚本一键批量修改，规避重复人工操作。

---

# 十一、全局分片异常统一排障手册

- 分片未分配：使用GET _cluster/allocation/explain精准定位原因

- 高峰期IO打满：临时关闭集群自动分片均衡

- 冷热分片乱跑：禁止手动修改 _tier_preference，核对节点角色

- 分片搬迁过慢：调高恢复带宽与并发数

---

# 十二、跨中间件统一分片思想（ES / MySQL / Redis / Kafka）

ES、MySQL分库分表、Redis集群、Kafka分区底层完全一致：

1. 分片key哈希取模，固定绑定分片

2. 热点维度做分片键必然倾斜

3. 前期分片宁多勿少，后期扩容成本极高

4. 不要自研分片，原生生命周期+均衡能力远强于自研

---

# 十三、三分钟整体总结

（全文原有总结补充回流知识点，更新完整版总结）

ES分片分为路由数据倾斜、节点分片分配倾斜两类。路由依靠MurmurHash3 32位哈希取模分配分片，主分片创建后不可修改，哈希碰撞不会覆盖数据；省份、日期自定义路由极易热点倾斜，解决方案为路由Key后缀二次打散。

节点分片依靠IndexBalance、ShardBalance加权算法实现分片数量均衡，受磁盘三级水位、主副隔离、冷热层级三大前置约束管控分配逻辑；容量上单分片控制30\~50GB，前期分片规划宁多勿少。

冷热分层依托ILM实现索引单向Hot→Warm→Cold→Delete流转，**无原生自动回流能力**，温层回退热需要手动修改冷热标签\+暂停ILM；ILM仅依据索引创建/滚动时间判断，不识别文档业务时间；采用后台轮询调度，对比自研Cron定时任务流量更平滑、自带幂等状态、异常自愈更强，仅修改索引data\_tier标签完成层级迁移，只能节约存储成本，无法治理热分片倾斜。

字段改名改类型、重新分片、修改路由规则均无法原地变更，统一采用**新建索引\+Reindex脚本迁移\+业务双写\+原子别名切换**方案；业务上线初期即规划固定访问别名，底层索引迭代重构无需修改业务代码，做到无感切换、回滚便捷。针对日志、流水无限写入场景，可采用Rollover滚动索引模式自动拆分时序索引，配合ILM完成生命周期管理。ILM、分片均衡属于集群常驻自动配置，而Reindex重构、ILM反向回流属于一次性人工运维变更流程，无法全自动配置执行。

该分片思想通用于MySQL分库分表、Redis集群、Kafka分区；成熟中间件动态配置、内置自愈能力，运维成本更低，中小企业优先用好开源方案，不盲目自研分布式分片轮子。

