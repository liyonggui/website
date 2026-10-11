# ES分片\+路由\+ILM冷热\+Rollover\+Reindex\+全层级回流 最终完整版手册

## 全文目录

1. 索引与分片核心基础

2. 路由哈希底层原理与分片分配规则

3. 两种分片倾斜问题 \+ 生产根治方案

4. 分片均衡算法 \+ 磁盘水位 \+ 搬迁限流运维

5. 生产索引、分片、节点容量标准化规划

6. ILM冷热分层全解（原理、误区、参数、运维、排障）

7. Reindex零停机重构全业务场景方案

8. 索引别名架构 \+ 普通业务索引上线标准模板

9. Rollover时序滚动索引生产初始化全套模板

10. **ILM全链路反向回流：热/温/冷任意层级回退通用方案（核心新增）**

11. 集群分片红黄异常全套排障手册

12. ES/MySQL/Redis/Kafka 通用分片思想

13. 面试3分钟完整背诵总结

---

# 1\. 索引与分片核心基础

## 1\.1 集群层级结构

集群 → 索引 → 主分片/副本分片 → 物理节点 → 服务器硬件资源（CPU/内存/磁盘）

## 1\.2 两条不可违背铁律

1. 主分片数量**创建后永久不可修改**，改动会导致全部路由失效，数据查不到

2. 同一个分片的主分片和副本分片，绝不允许部署在同一台节点，单机宕机直接丢数据

## 1\.3 总分片计算公式

总分片数 = 主分片数 ×（副本数 \+ 1）

示例：9主1副本 = 18个总分片，3节点集群，理论每节点均分6个分片

---

# 2\. 路由哈希底层原理与分片分配规则

## 2\.1 文档路由分片计算公式（底层固定不可改）

```java
// 路由key优先使用自定义routing，没有则使用文档_id
String routingKey = routing != null ? routing : doc._id;
// 32位Murmur哈希运算
int hash = Murmur3HashFunction.hash(routingKey);
// 取模命中固定主分片
int shardId = Math.abs(hash) % number_of_primary_shards;

```

## 2\.2 两种哈希算法分工

|哈希算法|用途|说明|
|---|---|---|
|MurmurHash3\_x86\_32|分片路由寻址|允许少量哈希碰撞，不会覆盖数据|
|MurmurHash3\_x64\_128|基数去重cardinality|极低碰撞，保证统计精准|

## 2\.3 关键误区

- 哈希碰撞不会丢失/覆盖数据，分片内靠\_id区分文档

- 副本数量可以随时动态调整，完全不影响路由规则

- 同一个routingKey，永远固定落在同一个分片

---

# 3\. 两种分片倾斜问题 \+ 生产根治方案

## 3\.1 业务层：路由数据倾斜（均衡算法解决不了）

### 成因

为了减少跨分片聚合，使用省份、日期、大商户ID、大客户ID作为routing，热点key全部扎堆一个分片，单分片数据爆炸。

### 生产解决方案：路由key二次后缀打散

原热点key：guangdong → 固定单分片

打散后：guangdong\_0 / guangdong\_1 / guangdong\_2，分散至多个分片，打散热点压力

### 可用Java工具类

```java
public class RoutingSplitUtil {
    // 写入生成打散路由
    public static String getSplitRouting(String bizKey, int splitNum, Long snowId) {
        if (splitNum <= 1) return bizKey;
        int suffix = Math.abs(snowId.intValue()) % splitNum;
        return bizKey + "_" + suffix;
    }
    // 查询遍历所有分片路由，全量检索
    public static List<String> listAllRouting(String bizKey, int splitNum) {
        List<String> list = new ArrayList<>();
        for (int i = 0; i < splitNum; i++) {
            list.add(bizKey + "_" + i);
        }
        return list;
    }
}

```

## 3\.2 集群层：节点分片数量倾斜（ES自动均衡可修复）

现象：节点总分片数量差距过大，原因是分片搬迁限流、均衡阈值保守、磁盘水位过高。

---

# 4\. 分片均衡算法 \+ 磁盘水位 \+ 搬迁限流运维

## 4\.1 ES分片均衡打分公式

综合得分 = 索引分片均衡差值\(0\.55权重\) \+ 集群总分片均衡差值\(0\.45权重\)

得分越低，节点越空闲，优先分配分片

## 4\.2 磁盘三级水位（硬性拦截，优先级最高）

- 85% low：开始主动迁出分片

- 90% high：禁止新分片迁入

- 95% flood\_stage：整集群索引只读，保护磁盘不打满宕机

## 4\.3 分片搬迁限流配置（生产必配，防IO雪崩）

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

---

# 5\. 生产索引、分片、节点容量标准化规划

## 5\.1 单分片标准容量（生产红线）

- 热数据高读写：20\-35GB

- 普通业务索引：35\-50GB（通用最优值）

- 冷归档极少查询：上限60\-80GB

分片过大：迁移、恢复、段合并极慢，故障恢复风险极高

## 5\.2 主分片规划原则

预估未来3年总数据量 ÷ 50GB = 建议主分片数

**核心原则：宁多勿少，主分片不可扩容**

## 5\.3 节点磁盘使用率红线

- hot热节点SSD：≤75%

- warm温节点：≤80%

- cold冷节点HDD：≤82%

---

# 6\. ILM冷热分层全解（高频面试\+生产核心）

## 6\.1 ILM核心铁律（必考）

1. 流转单向不可逆：Hot → Warm → Cold → Delete，**原生不支持自动回流**

2. 计时时间：看索引创建/滚动时间，**不识别业务文档时间**（最大误区）

3. 最小流转单位：整个索引，不支持单条数据冷热迁移

4. 调度方式：后台10分钟轮询，不是定时Cron任务

## 6\.2 四阶段标准生产ILM策略

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

## 6\.3 ILM常见故障排障命令

```http
# 查看索引生命周期详细卡住原因
GET index_name/_ilm/explain

# 手动启停ILM
POST _ilm/start
POST _ilm/stop

```

---

# 7\. Reindex零停机重构全业务场景方案

## 7\.1 哪些操作不能原地修改，必须Reindex

- 修改字段类型、字段重命名、修改分词器

- 修改主分片数量

- 修改自定义路由规则、打散热点数据

## 7\.2 标准零停机重构流程

新建索引 → Reindex全量迁移 → 业务双写追增量 → 原子别名切换 → 观察平稳 → 下线旧索引

## 7\.3 字段迁移Reindex脚本

```http
POST _reindex
{
  "source": {"index": "order_v1"},
  "dest": {"index": "order_v2"},
  "script": {
    "source": "ctx._source.orderId = ctx._source.remove(\"o_id\")"
  }
}

```

---

# 8\. 索引别名架构 \+ 普通业务索引上线标准模板

## 8\.1 别名核心思想

业务永远访问别名，绝不硬编码物理索引名；底层索引迭代、重构、迁移，业务无需改代码、无需重启服务。

## 8\.2 原子别名切换（无感切流/回滚）

```http
POST _aliases
{
  "actions": [
    {"remove": {"index": "order_v1", "alias": "alias_order"}},
    {"add": {"index": "order_v2", "alias": "alias_order"}}
  ]
}

```

---

# 9\. Rollover时序滚动索引生产全套模板（日志/流水/埋点专用）

## 9\.1 架构说明

写入别名唯一写索引，达到容量/时间阈值自动滚动生成新索引，旧索引自动进入ILM冷热生命周期，适配无限时序写入场景。

## 9\.2 滚动索引模板

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
        "createTime": {"type": "date"}
      }
    }
  },
  "priority": 20
}

```

## 9\.3 初始化首个可写索引

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

---

# 10\. ILM全链路反向回流【全文重点】任意层级向上回退通用方案

## 10\.1 统一结论（回答你刚才的问题）

**热→温→冷→删除，任意上游层级回滚，操作完全通用！**
不管是温回热、冷回温、冷直接跨层回热，底层逻辑一模一样：
1\. 修改索引data\_tier上层标签，自动触发分片回迁
2\. 暂停索引ILM，防止再次自动下沉
唯一区别：data\_tier参数值不同

## 10\.2 全场景一键回退命令速查（直接复制可用）

### 场景1：Warm 温层 → Hot 热层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "hot"
}
POST your_index/_ilm/stop

```

### 场景2：Cold 冷层 → Warm 温层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "warm"
}
POST your_index/_ilm/stop

```

### 场景3：Cold 冷层 跨层直接回 Hot 热层

```http
PUT your_index/_settings
{
  "index.routing.allocation.data_tier": "hot"
}
POST your_index/_ilm/stop

```

## 10\.3 通用进度查看命令（所有回流统一）

```http
GET _cat/recovery?v&h=index,shard,percent,source_node,target_node,stage

```

## 10\.4 回流之后必须处理的4个不可逆动作

1. **shrink缩分片：只能迁节点，分片数量永远无法复原，必须Reindex才能恢复**

2. forcemerge段合并：合并不可逆，无需处理，不影响读写

3. 温层只读：回流后需要写入，手动解除只读

4. 冷层冻结：回流后需要高频查询，手动关闭冻结

## 10\.5 致命边界：Delete阶段无法回退

索引一旦被ILM删除，物理文件彻底清除，无法直接回退；只能依赖快照备份恢复。

## 10\.6 批量一键回流Shell脚本（全通用，改一行适配冷热）

```bash
#!/bin/bash
ES_HOST="http://127.0.0.1:9200"

# 批量回迁所有温层索引，换成cold即可批量回迁冷索引
INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.data_tier:warm" | jq -r 'keys[] | select(startswith(".") | not)')

for index in $INDEX_LIST
do
  curl -X PUT "$ES_HOST/$index/_settings" -H "Content-Type:application/json" -d '{"index.routing.allocation.data_tier":"hot"}'
  curl -X POST "$ES_HOST/$index/_ilm/stop"
done

```

---

# 11\. 集群分片红黄异常全套排障手册

- 分片未分配：使用GET \_cluster/allocation/explain精准定位原因

- 高峰期IO打满：临时关闭集群自动分片均衡

- 冷热分片乱跑：禁止手动修改data\_tier，核对节点角色

- 分片搬迁过慢：调高恢复带宽与并发数

---

# 12\. 中间件通用分片思想（面试加分）

ES、MySQL分库分表、Redis集群、Kafka分区底层完全一致：

1. 分片key哈希取模，固定绑定分片

2. 热点维度做分片键必然倾斜

3. 前期分片宁多勿少，后期扩容成本极高

4. 不要自研分片，原生生命周期\+均衡能力远强于自研

---

# 13\. 面试3分钟完整版背诵话术（直接背）

ES通过Murmur3哈希\+取模实现文档路由，主分片一旦创建不可修改；业务自定义路由容易产生热点分片倾斜，解决方案是路由key二次后缀打散。

集群依靠双层加权算法实现分片均衡，同时受磁盘水位、主副分片隔离、冷热节点三层硬性约束。生产上单分片控制在30\-50GB，前期分片规划宁多勿少。

ILM冷热流转是单向Hot→Warm→Cold→Delete，依靠后台轮询调度，不识别业务时间，只能节约存储，无法解决热数据倾斜。**所有层级向上回退操作完全通用，只需要修改data\_tier标签并且暂停ILM即可**，但是shrink缩分片不可逆，删除后数据无法恢复。

凡是改字段、改分片、改路由，全部无法原地变更，统一使用Reindex\+双写\+别名原子切换实现零停机迁移。时序无限写入业务采用Rollover滚动索引自动拆分索引，搭配ILM完成全生命周期管理。

整套分片思想通用于所有分布式中间件，生产优先使用原生能力，拒绝自研分布式分片。

> （注：部分内容可能由 AI 生成）
