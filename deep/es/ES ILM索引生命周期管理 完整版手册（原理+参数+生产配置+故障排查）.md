---
title: "ES ILM 索引生命周期管理完整手册（原理 · 参数 · 生产配置 · 故障排查）"
summary: "本文承接前文ES路由、分片分配、冷热分层全套内容，ILM是ES冷热分层唯一原生实现方案，全程无业务代码、无定时任务，配置驱动实现索引自动冷热流转；同时厘清所有人都踩过的时间误区、调…"
date: 2026-07-21
author: "小番茄程序猿"
tags:
  - Elasticsearch
  - 索引
  - 源码
  - ILM
category: deep
series: "es"
slug: "ES ILM索引生命周期管理 完整版手册（原理+参数+生产配置+故障排查）"
reading_time: 11
featured: false
draft: false
---
# ES ILM索引生命周期管理 完整版手册（原理\+参数\+生产配置\+故障排查）

## 前置说明

本文承接前文ES路由、分片分配、冷热分层全套内容，**ILM是ES冷热分层唯一原生实现方案**，全程无业务代码、无定时任务，配置驱动实现索引自动冷热流转；同时厘清所有人都踩过的时间误区、调度误区、流转误区，附带完整可用生产脚本\+全覆盖故障排障。

---

# 一、ILM核心底层原理（必背，解决你之前3个疑问）

## 1\. 最小流转单位

**以【整个索引】为最小单位，不支持单条文档冷热迁移**，不能把索引里一部分老数据挪冷节点、一部分留热节点。

## 2\. 时间判断依据（高频坑点）

**完全不看文档create\_time、update\_time业务字段！！！**

1. 普通静态索引：计时起点 = **索引创建时间**，文档无论怎么更新，索引年龄不变

2. Rollover滚动索引（生产时序业务标配）：计时起点 = **索引滚动切换时间**

> **通俗一句话**：ILM只管索引活了多久，不管里面数据改了多少次、数据是什么时间产生的。
> 
> 

## 3\. 底层调度方式（是不是定时任务？）

**不是Cron定点定时任务，是ES内置后台线程轮询巡检**

- 后台服务：`ilm-service`

- 默认轮询间隔：**10分钟检查一次**

- 全局配置参数：`indices.lifecycle.poll_interval`

- 执行逻辑：每轮遍历所有绑定ILM策略的索引 → 判断是否满足阶段条件 → 上一步未执行完毕则跳过，不重复执行

## 4\. 四阶段单向流转逻辑（不可逆）

Hot热 → Warm温 → Cold冷 → Delete删除，**只能往后走，不能自动回流热节点**

1. 热阶段：承接高并发写入、高频查询，SSD高性能节点

2. 温阶段：停止写入，合并索引段、收缩分片，降低资源占用

3. 冷阶段：归档存储，冻结索引，节省内存，大容量HDD磁盘

4. 删除阶段：自动清理过期索引，释放磁盘空间

## 5\. 底层流转本质动作

ILM不搬运数据，只**修改索引分片分配标签**：给索引打上`_tier_preference: data_warm/data_cold`强制标签，ES分片分配算法自动将分片搬迁至对应节点池，全程集群内部自愈。

---

# 二、ILM全阶段参数详解（表格速查，全覆盖）

## 通用公共参数

|参数|含义|生产建议值|
|---|---|---|
|min\_age|进入当前阶段需要等待的最小时长|热阶段固定0ms，其余按业务留存设置|
|set\_priority|索引分片搬迁优先级，数值越高越优先|热100 / 温50 / 冷10|

## 1\. Hot热阶段（写入阶段）核心参数

|参数|作用|
|---|---|
|rollover\.max\_age|索引最长写入周期，到期自动建新索引切别名|
|rollover\.max\_primary\_shard\_size|单主分片容量阈值，到达容量自动滚动（核心推荐）|
|rollover\.max\_docs|文档条数阈值，一般不推荐使用，数据量波动大|

## 2\. Warm温阶段（降配优化）核心参数

|参数|作用|
|---|---|
|allocate|强制索引分片只能分配到温节点池|
|forcemerge|强制合并索引分段，减少小段，提升查询性能，**非常耗IO**|
|shrink|收缩主分片数量，减少元数据内存占用，**不可逆**|
|readonly|索引设置只读，杜绝后续写入产生新小段|

## 3\. Cold冷阶段（归档阶段）核心参数

|参数|作用|
|---|---|
|allocate|强制分片仅存放冷节点池|
|freeze|索引冻结，大幅降低堆内存占用，冷归档标配|

## 4\. Delete删除阶段

到达指定时长，直接删除整个索引，**删除后不可恢复**，生产务必谨慎配置

---

# 三、生产可直接复制：完整ILM策略\+索引模板

## 1\. 创建ILM生命周期策略

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
          "set_priority": {
            "priority": 100
          }
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "allocate": {
            "require": {
              "_tier_preference": "data_warm"
            }
          },
          "forcemerge": {
            "max_num_segments": 1
          },
          "shrink": {
            "number_of_shards": 3
          },
          "set_priority": {
            "priority": 50
          }
        }
      },
      "cold": {
        "min_age": "30d",
        "actions": {
          "allocate": {
            "require": {
              "_tier_preference": "data_cold"
            }
          },
          "freeze": {},
          "set_priority": {
            "priority": 10
          }
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

## 2\. 索引模板绑定ILM\+冷热节点

```json
PUT _index_template/template_order
{
  "index_patterns": ["order_*"],
  "template": {
    "settings": {
      "number_of_shards": 9,
      "number_of_replicas": 1,
      "index.lifecycle.name": "order_hot_warm_cold_policy",
      "index.routing.allocation.include._tier_preference": "data_hot"
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

---

# 四、生产环境必看：ILM八大注意事项（避坑核心）

1. **冷热分层≠解决分片倾斜**：ILM只能省磁盘成本，**解决不了热数据路由热点倾斜**，热节点依然必须做路由二次打散；温、冷数据无访问，倾斜无需处理

2. **分片收缩不可逆**：warm阶段shrink缩分片后，主分片无法扩容，前期必须规划好分片数量

3. **流转存在时间误差**：默认10分钟轮询，不会精准卡点流转；加上分片搬迁、段合并耗时，延迟几十分钟属于正常现象

4. **forcemerge极度耗IO**：禁止业务高峰期执行，ILM自动合并尽量放在凌晨低峰，防止线上查询卡顿

5. **节点角色必须齐全**：集群缺少hot/warm/cold任意节点，索引会卡在当前阶段，彻底停止冷热流转

6. **禁止手动修改索引data\_tier配置**：会导致分片乱跑、主副分片跨节点异常、集群变黄

7. **索引冻结利弊**：冻结大幅节省内存，但查询性能下降，有偶尔查询需求的业务不要开启freeze

8. **删除风险极高**：ILM删除索引无回收站，上线前务必测试，核心数据提前开启快照备份

---

# 五、ILM线上故障一页排查清单（现象→原因→解决方案）

|故障现象|根因|排查命令|解决方案|
|---|---|---|---|
|索引到时间不流转，一直停留在热节点|1\.缺少对应warm/cold节点；2\.分片搬迁卡住；3\.上一阶段动作未完成|GET 索引/\_ilm/explain|补齐对应角色节点；低峰调高分片搬迁并发|
|索引滚动失败，索引持续膨胀|别名未绑定写入；rollover阈值设置过大|GET \_alias/别名|补全写入别名；合理调低分片容量阈值|
|ILM流转期间集群IO暴涨|forcemerge段合并\+分片搬迁同时执行|GET \_cat/recovery?v|高峰期临时关闭集群rebalance，低峰再放开|
|索引误流转，提前进入冷节点|误解计时长，把索引创建时间当成业务数据时间|GET 索引/\_settings|修正min\_age时长；理解ILM只认索引时间不认业务时间|
|索引冻结后查询变慢|freeze索引本身牺牲性能换内存|GET 索引/\_settings?filter\_path=\*\.frozen|有查询需求移除freeze动作，不开启索引冻结|
|不想自动删索引，但是策略一直执行删除|删除阶段min\_age设置过小|GET \_ilm/policy/策略名|拉长删除时长；临时注释delete阶段动作|

---

# 六、ILM常用运维命令（一键复制）

```http
# 1.查看单个索引生命周期流转详情（最核心排障命令）
GET /index_name/_ilm/explain

# 2.查看全局ILM轮询间隔配置
GET _cluster/settings?include_defaults=true&filter_path=indices.lifecycle*

# 3.手动触发一次ILM巡检（紧急排查用）
POST _ilm/start

# 4.临时修改ILM轮询间隔（测试改成1分钟，生产保持10分钟）
PUT _cluster/settings
{
  "transient": {
    "indices.lifecycle.poll_interval": "10m"
  }
}

# 5.查看索引当前冷热节点分配标签
GET index_name/_settings?filter_path=*.settings.index.routing.allocation.include._tier_preference
```

---

# 七、3 分钟标准要点

ES ILM是原生索引生命周期管理，用来实现冷热数据分层，全程零代码维护。首先ILM是以整个索引为最小流转单位，**不识别文档内部创建和更新时间**，只根据索引创建时间或者滚动rollover时间计时；调度层面不是定时Cron任务，而是后台线程默认10分钟轮询巡检一次。

整体分为热、温、冷、删除四个阶段，单向流转不可回流，通过修改索引节点分配标签，让集群分片自动迁移到对应冷热节点。同时需要重点区分，冷热分层只能优化存储成本，**无法解决热数据自定义路由带来的分片热点倾斜**。

生产落地需要注意，分片收缩不可逆、段合并IO压力大、索引删除不可恢复，日常排障优先使用ilm explain命令定位流转卡住原因，业务高峰期需要管控分片搬迁和段合并，避免集群IO雪崩。

