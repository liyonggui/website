---
title: "ES 运维常用命令速查手册"
summary: "1. 分片路由与倾斜排查命令 2. 集群分片均衡 & 磁盘水位配置 3. ILM 生命周期常规运维命令 4. ILM 冷热层级向上回退全套命令（Warm↔Hot / Cold↔Wa…"
date: 2026-07-29
author: "小番茄程序猿"
tags:
  - Elasticsearch
category: deep
series: "es"
slug: "ES_运维命令速查手册"
reading_time: 15
featured: false
draft: false
---
# ES 运维常用命令速查手册（独立文档，可直接复制使用）

## 目录

1. 分片路由与倾斜排查命令
2. 集群分片均衡 & 磁盘水位配置
3. ILM 生命周期常规运维命令
4. ILM 冷热层级向上回退全套命令（Warm↔Hot / Cold↔Warm / Cold→Hot）
5. Reindex 数据迁移重构常用命令
6. 索引别名管理命令（创建、原子切换、查询）
7. Rollover 滚动索引运维命令
8. 索引读写锁、冻结、只读解除配套命令
9. 分片异常排障核心命令
10. 批量回流 Shell 脚本（温→热、冷→温通用）

---

## 1. 分片路由与倾斜排查命令

### 查看文档归属分片

```http
GET /索引名/_search?routing=路由值
{
 "size": 1,
 "_source": false
}
```

### 查看索引分片分布

```http
GET _cat/shards/索引名?v
```

### 查看索引自定义路由开启状态

```http
GET 索引名/_mapping?filter_path=*.routing
```

### 统计各分片文档数量（判断倾斜）

```http
GET 索引名/_search
{
 "size": 0,
 "aggs": {
 "shard_group": {
 "terms": {
 "script": {
 "source": "doc['_shard_id'].value"
 },
 "size": 20
 }
 }
 }
}
```

---

## 2. 集群分片均衡 & 磁盘水位配置

### 设置分片搬迁并发与限速（生产常用）

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

### 临时关闭集群自动均衡（高峰期防IO抖动）

```http
PUT _cluster/settings
{
 "transient": {
 "cluster.routing.allocation.allow_rebalance": "none"
 }
}
```

### 恢复自动均衡

```http
PUT _cluster/settings
{
 "transient": {
 "cluster.routing.allocation.allow_rebalance": "indices_all_active"
 }
}
```

### 查看磁盘水位阈值配置

```http
GET _cluster/settings?include_defaults=true&filter_path=*.disk.watermark
```

---

## 3. ILM 生命周期常规运维命令

### 查看索引 ILM 流转详情、卡住原因

```http
GET 索引名/_ilm/explain
```

### 暂停单个索引 ILM

```http
POST 索引名/_ilm/stop
```

### 开启单个索引 ILM

```http
POST 索引名/_ilm/start
```

### 全局启停所有 ILM

```http
POST _ilm/stop
POST _ilm/start
```

### 查看全局 ILM 巡检间隔

```http
GET _cluster/settings?include_defaults=true&filter_path=indices.lifecycle.poll_interval
```

### 修改 ILM 全局巡检间隔

```http
PUT _cluster/settings
{
 "transient": {
 "indices.lifecycle.poll_interval": "5m"
 }
}
```

### 查询所有绑定 ILM 的索引

```http
GET _settings/index.lifecycle.name:*
```

---

## 4. ILM 冷热层级向上回退全套通用命令

> 通用前置：查看分片回迁进度统一命令

```http
GET _cat/recovery?v&h=index,shard,percent,source_node,target_node,stage
```

### 场景1：Warm → Hot 温层回退热层

```http
# 修改层级标签，触发分片回迁
PUT 索引名/_settings
{
 "index.routing.allocation.include._tier_preference": "data_hot"
}
# 暂停ILM防止再次下沉
POST 索引名/_ilm/stop
```

### 场景2：Cold → Warm 冷层回退温层

```http
PUT 索引名/_settings
{
 "index.routing.allocation.include._tier_preference": "data_warm"
}
POST 索引名/_ilm/stop
```

### 场景3：Cold → Hot 冷层跨层直接回退热层

```http
PUT 索引名/_settings
{
 "index.routing.allocation.include._tier_preference": "data_hot"
}
POST 索引名/_ilm/stop
```

### 回流完成后恢复自动生命周期

```http
POST 索引名/_ilm/start
```

### 边界说明

1. Delete 已删除索引无法直接回退，只能通过快照恢复重建
2. shrink 缩分片仅迁移节点位置，分片数量无法自动复原

---

## 5. Reindex 数据迁移重构常用命令

### 基础全量迁移

```http
POST _reindex
{
 "source": {"index": "旧索引"},
 "dest": {"index": "新索引"}
}
```

### 带 Painless 脚本：字段改名、类型转换迁移

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

### 路由重打散 Reindex（解决热点倾斜）

```http
POST _reindex?slices=auto&requests_per_second=2000
{
 "source": {"index": "order_old"},
 "dest": {"index": "order_new"},
 "script": {
 "source": """
 def suffix = Math.floorMod(ctx._source.orderId.hashCode(), 3);
 ctx._routing = ctx._source.province + "_" + suffix;
 """
 }
}
```

### 异步执行 Reindex（超大索引）

```http
POST _reindex?wait_for_completion=false
{
 "source": {"index": "旧索引"},
 "dest": {"index": "新索引"}
}
```

### 查看异步任务进度

```http
GET _tasks/任务ID
```

### 时间范围增量追赶数据

```http
POST _reindex
{
 "source": {
 "index": "旧索引",
 "query": {
 "range": {
 "update_time": {
 "gte": "迁移起始时间"
 }
 }
 },
 "dest": {"index": "新索引"}
 }
}
```

### 新旧索引数量校验

```http
GET 旧索引/_count
GET 新索引/_count
```

---

## 6. 索引别名管理命令

### 创建索引同时绑定别名（初始化最佳实践）

```http
PUT order_v1
{
 "settings": {
 "number_of_shards": 9,
 "number_of_replicas": 1
 },
 "mappings": {
 "properties": {
 "orderId": {"type":"keyword"}
 }
 },
 "aliases": {
 "alias_order": {}
 }
}
```

### 事后给已有索引添加别名

```http
POST _aliases
{
 "actions": [
 {"add": {"index": "order_v1", "alias": "alias_order"}}
 ]
}
```

### 原子切换别名（重构无感切流/回滚核心）

```http
POST _aliases
{
 "actions": [
 {"remove": {"index": "order_v1", "alias": "alias_order"}},
 {"add": {"index": "order_v2", "alias": "alias_order"}}
 ]
}
```

### 查询别名绑定关系

```http
GET _alias/alias_order
```

### 删除别名

```http
POST _aliases
{
 "actions": [
 {"remove": {"index": "*", "alias": "alias_order"}}
 ]
}
```

---

## 7. Rollover 滚动索引运维命令

### 手动触发滚动测试

```http
POST alias_order_roll/_rollover
```

### 查看滚动索引别名写入标识

```http
GET _alias/alias_order_roll
```

### 初始化首个滚动可写索引

```http
PUT order-000001
{
 "aliases": {
 "alias_order_roll": {
 "is_write_index": true
 }
 }
}
```

### 查看滚动索引模板

```http
GET _index_template/template_order_roll
```

---

## 8. 索引读写锁、冻结、只读解除配套命令

### 解除索引只读锁（Warm回流后常用）

```http
PUT 索引名/_settings
{
 "index.blocks.write": null
}
```

### 手动设置索引只读

```http
PUT 索引名/_settings
{
 "index.blocks.write": true
}
```

### 关闭索引冻结（Cold回流后常用）

```http
PUT 索引名/_settings
{
 "index.frozen": false
}
```

### 手动冻结索引

```http
PUT 索引名/_settings
{
 "index.frozen": true
}
```

### 查看索引冷热层级标签

```http
GET 索引名/_settings?filter_path=*.settings.index.routing.allocation.include._tier_preference
```

---

## 9. 分片异常排障核心命令

### 查看未分配分片详细原因

```http
GET _cluster/allocation/explain
```

### 查看集群健康状态

```http
GET _cluster/health?v
```

### 查看节点磁盘使用率

```http
GET _cat/nodes?v&h=name,disk.total,disk.used,disk.used_percent,heap*
```

### 查看未分配分片列表

```http
GET _cat/shards?v&s=state
```

### 手动修改副本数量

```http
PUT 索引名/_settings
{
 "number_of_replicas": 1
}
```

---

## 10. 批量回流 Shell 脚本（通用可修改）

### 脚本1：批量所有 Warm 索引回迁 Hot（warm_to_hot.sh）

```bash
#!/bin/bash
ES_HOST="http://127.0.0.1:9200"
# ES_USER="elastic"
# ES_PWD="你的密码"
INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.include._tier_preference" | jq -r 'to_entries[] | select((.value.settings.index.routing.allocation.include._tier_preference // "") | contains("data_warm")) | select(.key | startswith(".") | not) | .key')
if [ -z "$INDEX_LIST" ];then
 echo "无warm索引需要回迁"
 exit 0
fi
echo "$INDEX_LIST"
while read index; do
[ -z "$index" ] && continue
curl -s -X PUT "$ES_HOST/$index/_settings" -H "Content-Type:application/json" -d '{"index.routing.allocation.include._tier_preference":"data_hot"}'
curl -s -X POST "$ES_HOST/$index/_ilm/stop"
echo "$index 已设置hot并暂停ILM"
done <<< "$INDEX_LIST"
echo "执行完成，查看迁移进度：GET _cat/recovery?v"
```

### 适配 Cold 批量回流只需修改一行过滤条件

```bash
INDEX_LIST=$(curl -s "$ES_HOST/_settings/index.routing.allocation.include._tier_preference" | jq -r 'to_entries[] | select((.value.settings.index.routing.allocation.include._tier_preference // "") | contains("data_cold")) | select(.key | startswith(".") | not) | .key')
```

### 脚本使用方式

```bash
# 安装依赖
yum install jq -y
# 授权运行
chmod +x warm_to_hot.sh
./warm_to_hot.sh
```

---

文档使用说明：

1. 全文为生产可直接复制执行的运维命令，覆盖分片排查、ILM冷热回流、Reindex重构、别名切换、异常排障全场景；
2. 回流逻辑统一：修改 `_tier_preference` + 暂停 ILM，所有层级回退操作模式通用；
3. 保存为 `ES_运维命令速查手册.md`，日常运维、问题排查、版本重构可快速查阅使用。


