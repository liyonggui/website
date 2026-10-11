---
title: "一、ES分片倾斜线上应急处理步骤（可直接照着操作）"
summary: "先定位、再评估影响、先应急降压、再根治修复，禁止高峰期大力度迁移分片、大批量 reindex。"
date: 2026-08-10
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 分库分表
category: deep
series: "es"
slug: "一、ES分片倾斜线上应急处理步骤（可直接照着操作）"
reading_time: 11
featured: false
draft: false
---
# 一、ES分片倾斜线上应急处理步骤（可直接照着操作）

## 前置原则

先定位、再评估影响、先应急降压、再根治修复，禁止高峰期大力度迁移分片、大批量 reindex。

### 步骤1：快速确认是否发生分片倾斜

```http
# 查看各分片文档数量排序
GET _cat/shards/你的索引名?v&s=docs:desc
# 查看分片磁盘占用排序
GET _cat/shards/你的索引名?v&s=store:desc
```

判定倾斜：分片文档/磁盘相差 **1.5倍以上警惕，2倍以上确定倾斜**

### 步骤2：定位倾斜根因

1. 如果索引使用了 `routing` 自定义路由，大概率热点Key导致
   
   ```http
   GET 索引名/_search
   {
   "size": 0,
   "aggs": {
   "hot_routing_top50": {
   "terms": {
   "field": "_routing",
   "size": 50
   }
   }
   }
   }
   ```
   
   观察某个 routing 文档量远超其他，确定热点路由倾斜。
2. 未自定义 routing：查看节点硬件、磁盘水位、分片分配约束
   
   ```http
   # 查看磁盘水位是否触发分片搬迁冻结
   GET _cluster/settings?include_defaults=true&filter_path=*.disk*
   # 查看分片无法均衡原因详情
   GET _cluster/allocation/explain
   ```
   
   ### 步骤3：应急缓解（高峰期快速降压力，治标）
3. 调高集群分片迁移并发，加速自动均衡
   
   ```http
   PUT _cluster/settings
   {
   "transient": {
   "cluster.routing.allocation.cluster_concurrent_rebalance": 4,
   "cluster.routing.allocation.node_concurrent_recoveries": 4
   }
   }
   ```
   
   > 业务低谷可改更大，高峰期不要超过4，避免IO打满。
4. 检查并合理设置磁盘水位，防止节点塞满拒绝分片迁入
   
   ```http
   PUT _cluster/settings
   {
   "transient": {
   "cluster.routing.allocation.disk.watermark.low": "85%",
   "cluster.routing.allocation.disk.watermark.high": "90%",
   "cluster.routing.allocation.disk.watermark.flood_stage": "95%",
   "cluster.routing.allocation.disk.include_relocations": true
   }
   }
   ```
5. 热点分片段合并限流，防止CPU/IO打垮节点
   
   ```http
   PUT 索引名/_settings
   {
   "index.merge.scheduler.max_thread_count": 1,
   "indices.store.throttle.max_bytes_per_sec": "20mb"
   }
   ```
   
   ### 步骤4：根治方案（业务低峰执行）
   
   #### 场景A：自定义 routing 热点倾斜（最常见）
6. 设计路由打散策略（热点key增加后缀分流）
7. 新建规划合理分片数的索引
8. reindex 迁移数据
   
   ```http
   POST _reindex
   {
   "source": {"index": "旧索引"},
   "dest": {"index": "新索引"}
   }
   ```
9. 别名切换流量，下线旧索引
   
   #### 场景B：原生_id轻微不均匀、分片规划不合理
   
   直接 reindex 重建，重新预估分片数量：
   `建议分片数 ≈ 总数据量 ÷ 40GB`（单分片最优30~50G）
   
   #### 场景C：节点硬件/机架策略导致摆放不均
10. 统一节点内存、磁盘类型、磁盘容量
11. 按需修改机架感知策略，解除不合理分配约束
12. 等待集群自动均衡完成
    
    ### 步骤5：事后预防配置
13. 时序日志启用 ILM 滚动索引，避免单索引无限膨胀
14. 上线前预估数据量，提前算好分片数量
15. 非业务强聚合场景，禁止滥用自定义 routing

---

# 二、MurmurHash3 完整知识点汇总（原理、版本、ES源码、场景、碰撞、要点全集）

## 1. 基础概述

MurmurHash 是**非加密型哈希函数**，由 Austin Appleby 设计；
特点：运算速度极快、雪崩性好、随机性强、低碰撞，适合分片打散、哈希表、基数统计场景；
**不是加密哈希（MD5/SHA），不可逆、不能用于加密场景**。
MurmurHash3 是第三代最终版本，废弃 MurmurHash1/2。

## 2. MurmurHash3 两大正式版本（核心区分）

| 版本                  | 输出位数                 | 适用场景        | ES使用位置                    |
| ------------------- | -------------------- | ----------- | ------------------------- |
| MurmurHash3_x86_32  | 32bit 有符号int         | 分片路由、轻量哈希映射 | ES默认分片路由算法                |
| MurmurHash3_x64_128 | 128bit（两个long：h1、h2） | 低碰撞需求、基数统计  | cardinality聚合、murmur3字段类型 |

### 关键结论

MurmurHash3 **本身不是固定128位**，有两个输出规格，ES根据业务需求分别选用。

## 3. MurmurHash3_x86_32（ES分片路由核心）

### 运算流程

1. 输入字符串 `_id/routingKey` 转为 UTF-16 字节数组
2. 设置固定种子 seed=0（保证集群所有节点哈希结果一致）
3. 分块混合运算、位旋转、乘法扰动、末尾混淆（fmix雪崩）
4. 输出 32 位有符号整型
   
   ### ES路由完整公式
   
   ```
   hashVal = Murmur3HashFunction.hash(routing/_id)
   shardId = Math.abs(hashVal) % number_of_primary_shards
   ```
   
   源码位置：
   `server/src/main/java/org/elasticsearch/cluster/routing/Murmur3HashFunction.java`
   底层调用 Lucene `StringHelper.murmurhash3_x86_32`
   
   ### 设计目的（重点）
5. **核心目标：打散数据，避免分片数据倾斜**
   消除自增ID、前缀重复ID的规律性，让数据均匀分布在所有主分片
6. 不是为消除哈希碰撞，碰撞无害
   
   ## 4. MurmurHash3_x64_128（128位完整版）
   
   ### 结构
   
   输出封装 `Hash128` 对象，包含 `h1`、`h2` 两个64位long，合计128bit哈希空间。
   源码位置：`org.elasticsearch.common.hash.MurmurHash3`
   
   ### ES使用场景
7. `cardinality` 基数去重聚合（UV统计、唯一值估算）
   - 该逻辑依靠哈希判断元素是否重复，碰撞会导致统计偏小
   - 128位超大哈希空间，极大降低碰撞概率，保证估算准确度
8. `murmur3` 字段类型，预计算字段哈希值，减少存储、加速去重判断
   
   ## 5. 哈希碰撞详细解读
   
   ### （1）路由哈希碰撞定义
   
   两个不同字符串，经过哈希运算得到相同hash值，取模后进同一个分片。
   例：`idA ≠ idB`，`hash(idA)=hash(idB)` → 落到同一个shard。
   
   ### （2）路由碰撞有没有危害？
   
   **完全无害**
- 哈希仅用来定位分片；
- 分片内部以原始 `_id` 作为文档唯一主键区分两条数据；
- 不会发生文档覆盖、更新错乱，仅少量数据扎堆，对整体均衡几乎无影响。
  
  ### （3）为什么路由不用128位进一步降低碰撞？
1. 32位int运算更快、CPU开销更低，海量写入更友好
2. 32位哈希空间足够巨大，自然碰撞概率极低，满足分片打散需求
3. 碰撞无业务负面影响，没必要引入128位的性能开销
   
   ### （4）什么场景不能容忍碰撞，必须用128位版本？
   
   基数统计 cardinality，碰撞会导致唯一值统计失真，必须更大哈希空间压低碰撞率。
   
   ## 6. 雪崩效应（哈希核心特性）
   
   优秀哈希必备雪崩性：输入微小改动，哈希结果发生巨大变化
   示例：
   `user1001` 与 `user1002` 仅末尾数字不同，哈希值完全离散，避免规律性扎堆，是解决分片倾斜的底层基础。
   
   ## 7. 自定义 routing 与 MurmurHash 关系
4. 不指定 routing：`routing = _id`，哈希打散均衡性最好
5. 指定 routing=业务维度（商家ID、用户ID）：
   路由公式变为 `shardId = hash(业务key) % 分片数`
   若存在热点业务key，MurmurHash再均匀也无法避免分片倾斜，这是业务设计问题，不是哈希算法缺陷。
   
   ## 8. 常见误区汇总
6. ❌ 误区：32位哈希代表分片最多存储2^32条数据
   ✅ 纠正：32位只是哈希中间运算结果，和分片存储容量无任何限制，分片容量由磁盘/配置决定
7. ❌ 误区：ES分片路由使用128位MurmurHash3
   ✅ 纠正：路由固定32位版本；128位仅用于基数统计等低碰撞场景
8. ❌ 误区：哈希碰撞会造成ES文档覆盖
   ✅ 纠正：碰撞仅同分片扎堆，内部靠原始_id区分，不会覆盖
9. ❌ 误区：MurmurHash用来加密敏感ID
   ✅ 纠正：非加密哈希，可逆推导特征，不能做加密
   
   ## 9. 一句话精简总结
   
   MurmurHash3分为32位、128位两个版本；ES分片路由采用MurmurHash3_x86_32，核心作用打散数据防止分片倾斜，哈希碰撞无害；cardinality去重等对碰撞敏感场景使用MurmurHash3_x64_128扩大哈希空间降低碰撞概率。
   