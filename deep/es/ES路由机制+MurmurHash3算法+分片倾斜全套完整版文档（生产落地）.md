---
title: "ES 路由机制与 MurmurHash3 · 分片倾斜全套（生产落地）"
summary: "java // 1. 确定路由key：优先自定义routing，没有则使用文档id String effectiveRouting = routing != null ? rout…"
date: 2026-08-02
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 分库分表
category: deep
series: "es"
slug: "ES路由机制+MurmurHash3算法+分片倾斜全套完整版文档（生产落地）"
reading_time: 18
featured: false
draft: false
---
# ES路由机制\+MurmurHash3算法\+分片倾斜全套完整版文档（生产落地）

## 一、ES写入分片路由核心公式（源码原生）

### 1\. 路由计算源码公式（ES7\.17 固定不变）

```java
// 1. 确定路由key：优先自定义routing，没有则使用文档_id
String effectiveRouting = routing != null ? routing : id;
// 2. 32位哈希运算
int hash = Murmur3HashFunction.hash(effectiveRouting);
// 3. 取模得到最终主分片编号
int shardId = Math.abs(hash) % 主分片数量;
```

### 2\. 核心前置结论

- **主分片数创建索引后永久不可修改**，所以同一条数据路由分片永远固定

- ES路由层面：**固定使用 MurmurHash3\_x86\_32（32位）**，不是128位

- 128位哈希仅用于基数聚合cardinality，和分片路由毫无关系

---

## 二、MurmurHash3 全网最全知识点汇总（精准避坑）

### 1\. 算法基础介绍

MurmurHash3：非加密快速哈希算法，优势：运算快、雪崩性极强、低碰撞，专门用于数据打散，**不可逆，不可做加密**。

分为两个官方版本，**不是固定128bit**：

| 算法版本                  | 输出位数           | ES使用场景                    | 核心特点                   |
| --------------------- | -------------- | ------------------------- | ---------------------- |
| MurmurHash3\_x86\_32  | 32位int         | **分片路由（唯一用途）**            | 速度快，碰撞无害，满足分片打散即可      |
| MurmurHash3\_x64\_128 | 128bit（2个long） | cardinality去重聚合、murmur3字段 | 超大哈希空间，极致降低哈希碰撞，保证统计精准 |

### 2\. 高频误区逐一纠正（高频问题）

1. **误区1：分片路由用的是128位哈希**
   纠正：完全错误，路由全程32位哈希，128位和分片路由无关

2. **误区2：32位哈希代表分片存储有上限**
   纠正：32位只是哈希中间计算值，**和分片存储数据量无任何限制**，分片上限由磁盘决定

3. **误区3：哈希碰撞会导致文档覆盖**
   纠正：路由哈希碰撞无害，不同id算出相同哈希，只会进入同一个分片；分片内部依靠原始文档\_id区分数据，**永远不会覆盖**

4. **误区4：MurmurHash3作用是消除哈希碰撞**
   纠正：核心作用是**打散有序数据，解决分片数据倾斜**，不负责消除碰撞

### 3\. 哈希碰撞通俗案例

两个不同文档ID：id1=abc1001、id2=xyz9999

经过32位哈希计算后hash值一致，进入同一个分片

结果：两条数据共存，无任何异常，仅仅少量数据扎堆，对整体集群无影响

### 4\. 为什么路由不用128位哈希降低碰撞？

- 路由碰撞本身无业务危害，没必要牺牲性能

- 32位int CPU计算更快，海量写入开销更低

- 32位哈希空间已经足够大，碰撞概率本身极低

### 5\. 什么时候必须用128位哈希？

基数统计cardinality：哈希碰撞会导致唯一值统计结果偏小，业务有损，必须用128位大空间哈希压低碰撞概率。

---

## 三、最优路由方案：雪花算法ID作为默认路由

### 1\. 雪花ID结构

1符号位 \+ 41时间戳 \+ 10机器ID \+ 12序列号

### 2\. 为什么雪花ID是ES最理想路由？

1. 自带时序\+机器码\+序列号，自带离散度，无规律

2. 经过MurmurHash3雪崩打散后，数据**绝对均匀分布所有分片**，零分片倾斜

3. 无需自定义routing，无需业务改造，开箱即用

4. 单条文档CRUD路由固定，性能最优

### 3\. 总结

**无分片内聚合查询需求，一律使用雪花ID默认路由，是生产最优解**

---

## 四、业务强制自定义routing场景\&分片倾斜根源

### 1\. 必须自定义routing的业务场景

为了减少跨分片聚合开销，提升查询性能，业务必须指定路由：

- 按**日期**路由：当日日志只查单个分片

- 按**地区/省份**路由：省内订单无需全分片检索

- 按**品牌/商家名称**路由：商家数据分片内聚合

### 2\. 分片倾斜根本原因（核心考点）

一旦固定业务维度作为routing：**一个key永久对应一个分片**

头部热点key（广东、当日日期、头部商家）数据量远超其他key，**哈希算法再均匀也无法解决业务体量不均**，最终产生严重分片倾斜

### 3\. 三类线上倾斜痛点

- 日期路由：当天写入分片爆炸，历史分片空闲

- 地区路由：沿海大省分片热点，西部省份分片闲置

- 商家路由：头部大商家单分片打爆，小商家分片空闲

---

## 五、生产解决方案：Routing二次打散（根治分片倾斜）

### 1\. 解决方案原理

给原始业务路由拼接固定后缀，将一个热点key拆分分散到多个分片，打散热点压力；写入带后缀路由，查询一次性查询所有后缀路由。

### 2\. 可直接上线：路由二次打散工具类（完整Java代码）

```java
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

/**
 * ES Routing二次打散工具类
 * 解决：日期/地区/商家自定义routing分片倾斜
 * 适配：雪花ID主键，保证数据路由永久固定
 */
public class RoutingSplitUtil {

    /**
     * 写入：生成打散后的路由key
     * @param bizKey 原始业务key：省份/日期/品牌
     * @param splitNum 拆分份数（热点越大拆分越多，建议3-5）
     * @param snowflakeId 雪花算法唯一ID，保证路由稳定不变
     * @return 打散路由 例如：guangdong_0、2026-06-23_2
     */
    public static String getSplitRouting(String bizKey, int splitNum, Long snowflakeId) {
        // 不拆分，直接返回原始key
        if (splitNum <= 1) {
            return bizKey;
        }
        // 根据雪花ID稳定取模，更新数据路由不会漂移
        int suffix = Math.abs(snowflakeId % splitNum);
        return bizKey + "_" + suffix;
    }

    /**
     * 查询：获取当前业务key所有拆分路由，并行查询多分片
     */
    public static List<String> listAllRouting(String bizKey, int splitNum) {
        List<String> routingList = new ArrayList<>(splitNum);
        for (int i = 0; i < splitNum; i++) {
            routingList.add(bizKey + "_" + i);
        }
        return routingList;
    }

    /**
     * 兼容字符串主键重载方法
     */
    public static String getSplitRouting(String bizKey, int splitNum, String strId) {
        if (strId == null || strId.isBlank()) {
            return getSplitRouting(bizKey, splitNum, null);
        }
        long hash = strId.hashCode();
        return getSplitRouting(bizKey, splitNum, hash);
    }
}
```

### 3\. 业务写入\&查询使用示例

```java
// 常量配置：统一拆分份数
private static final int SPLIT_COUNT = 3;

// 1.写入：省份路由打散
String routing = RoutingSplitUtil.getSplitRouting("guangdong", SPLIT_COUNT, order.getSnowId());
indexRequest.routing(routing);

// 2.查询：查询全省所有数据，传入全部后缀路由
List<String> routingList = RoutingSplitUtil.listAllRouting("guangdong", SPLIT_COUNT);
searchRequest.routing(routingList.toArray(new String[0]));
```

---

## 六、存量历史数据倾斜修复：Reindex一键迁移DSL

新数据走打散路由，存量旧倾斜数据低峰期执行reindex，批量追加路由后缀，彻底全量修复倾斜。

### Reindex脚本（基于文档内雪花ID自动拆分路由）

```json
POST _reindex?slices=5&wait_for_completion=false
{
  "source": {
    "index": "order_info_old"
  },
  "dest": {
    "index": "order_info_new"
  },
  "script": {
    "source": """
      // 拆分份数3
      int splitNum = 3;
      // 获取文档内雪花ID
      long snowId = ctx._source.snowId;
      // 原始业务路由key：省份
      String bizKey = ctx._source.province;
      // 生成打散路由
      int suffix = Math.abs(snowId % splitNum);
      ctx._routing = bizKey + "_" + suffix;
    """
  }
}
```

### 参数说明

- `wait_for_completion=false`：后台异步执行，避免连接超时

- `slices=5`：并行迁移，提升重建速度

- 拆分份数必须和线上业务代码保持一致

---

## 七、分片倾斜线上排查命令（一键复用）

```http
# 1.查看分片文档数量分布，快速判断倾斜
GET _cat/shards/order_info?v&s=docs:desc

# 2.查看分片磁盘占用分布
GET _cat/shards/order_info?v&s=store:desc

# 3.定位热点routing key
GET order_info/_search
{
  "size":0,
  "aggs": {
    "hot_routing": {
      "terms": {
        "field": "_routing",
        "size": 20
      }
    }
  }
}
```

---

## 八、一页纸极简总结

1. ES分片路由采用**MurmurHash3\_x86\_32 32位哈希**，并非128位；128位哈希仅用于基数去重聚合。

2. 哈希碰撞对路由完全无害，分片内部依靠原始\_id区分文档，不会发生数据覆盖。

3. MurmurHash3核心作用是打散有序ID，解决分片倾斜，不是消除哈希碰撞。

4. **雪花ID作为默认路由是最优方案**，离散度高，分片分布绝对均匀，零倾斜。

5. 业务为了减少跨分片查询，使用日期/地区/商家作为自定义routing，会出现热点key分片倾斜，属于业务设计问题，哈希无法解决。

6. 根治方案：路由二次打散，给业务key增加后缀，拆分热点流量；存量数据通过reindex批量修复。


# ES 冷热分层完整落地方案（可直接上线）

## 一、集群节点角色划分

### 1. 节点角色配置 `elasticsearch.yml`

#### 热节点（SSD高配，负责实时写入、高频查询）

```yaml
node.name: es-hot-1
node.roles: [master,data_hot]
# 内存配比：内存50%给JVM，剩余留给系统缓存
```

#### 温节点（普通SSD/高性能机械，少写入、日常查询）

```yaml
node.name: es-warm-1
node.roles: [data_warm]
```

#### 冷节点（大容量机械盘HDD，归档低访问数据）

```yaml
node.name: es-cold-1
node.roles: [data_cold]
```

> 7.10及以上版本原生支持 `data_hot/data_warm/data_cold` 层级划分，无需自定义标签

## 二、创建统一ILM生命周期策略（全自动流转）

```json
PUT _ilm/policy/order_hot_cold_policy
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
 "allocate": {
 "require": {"data_tier": "warm"}
 },
 "forcemerge": {"max_num_segments": 1},
 "shrink": {"number_of_shards": 3},
 "set_priority": {"priority": 50}
 }
 },
 "cold": {
 "min_age": "30d",
 "actions": {
 "allocate": {
 "require": {"data_tier": "cold"}
 },
 "freeze": {},
 "set_priority": {"priority": 10}
 }
 },
 "delete": {
 "min_age": "90d",
 "actions": {
 "delete": {}
 }
 }
 }
 }
}
```

### 策略说明

1. 热阶段：数据写入7天内/单分片达45G自动滚动生成新索引

2. 温阶段：满7天迁移至温节点，合并分段、缩减分片数，降低资源占用

3. 冷阶段：满30天迁移冷节点并冻结，节省内存

4. 清理阶段：满90天自动删除，按需修改时长
   
   ## 三、索引模板绑定ILM+冷热层级
   
   ```json
   PUT _index_template/template_order
   {
   "index_patterns": ["order_*"],
   "template": {
   "settings": {
   "number_of_shards": 9,
   "number_of_replicas": 1,
   "index.lifecycle.name": "order_hot_cold_policy",
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
- 新建 `order_` 开头索引自动套用配置，默认落地热节点

- 主分片直接初始化9片，前期预留充足容量，避免后期重构
  
  ## 四、业务架构搭配方案（冷热+防倾斜双合一）
1. **热数据（7天内）**
   
   - 启用路由二次打散，`省份_0~8` 分流热点
   - 多分片部署，均衡写入压力，彻底解决大省/当日数据倾斜

2. **温数据（7-30天）**
   
   - 访问量下降，自动缩分片、合并索引段，优化查询效率
   - 无需再做打散优化，少量倾斜无影响

3. **冷数据（30天以上）**
   
   - 冻结归档存储在低成本机械盘，节约集群成本
   
   - 几乎无写入，倾斜完全不用处理
     
     ## 五、核心使用规范

4. 时序业务统一使用**滚动索引**，拒绝单索引无限膨胀

5. 新项目起步直接拉高主分片数量，预留3年数据余量

6. 热数据必须做路由二次打散，冷热分层只解决存储成本，不解决分片倾斜

7. 副本数热节点保留1副本，冷数据可酌情降为0节省空间
   
   ## 六、日常运维常用指令
   
   ```http
   # 查看所有索引生命周期状态
   GET _cat/indices?v&h=index,status,health,pri,rep,ilm
   # 手动触发索引生命周期流转
   POST _ilm/explain/索引名
   # 关闭索引自动分片均衡（业务高峰）
   PUT _cluster/settings
   {"persistent":{"cluster.routing.allocation.balance.index":"0"}}
   ```
   
   ## 七、和分库分表统一思路总结

8. MySQL分表：手动按时间拆分、自行迁移冷热数据

9. ES冷热：ILM全自动流转，节点硬件分层隔离，运维零代码

10. 通用架构思想：**热数据做均衡防倾斜，冷数据做归档控成本**

11. 前期分片宁可多不可少，配合冷热分层，彻底规避后期扩容重构风险
