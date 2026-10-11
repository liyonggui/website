---
title: "Sysbench 单体 MySQL 硬件上限压测"
summary: "用 Sysbench 测出单体 MySQL（无集群、无主从）的硬件性能上限： - 读上限：oltpreadonly（纯查询） - 读写混合上限：oltpreadwrite（OLTP…"
date: 2026-03-07
author: "小番茄程序猿"
tags:
  - MySQL
  - 压测
category: deep
series: "mysql"
slug: "Sysbench单体MySQL硬件上限压测"
reading_time: 13
featured: false
draft: false
---
# Sysbench 单体 MySQL 硬件上限压测

## 一、背景与目标

用 Sysbench 测出**单体 MySQL（无集群、无主从）**的硬件性能上限：
- **读上限**：`oltp_read_only`（纯查询）
- **读写混合上限**：`oltp_read_write`（OLTP 混合）
- **找拐点**：加大并发，找出吞吐不再提升（甚至下降）的临界并发数

## 二、环境

| 项 | 值 |
|----|-----|
| MySQL | mysql:8.4（单机，无主从） |
| 配置 | max_connections=1000，innodb_buffer_pool_size=1G |
| 数据 | sbtest 库，16 表 × 100 万行 ≈ 1600 万行 |
| Sysbench | 1.0.17（Docker `docker.1ms.run/severalnines/sysbench`）|
| 账号 | sbuser（mysql_native_password）|

## 三、压测方法

```bash
# prepare 数据（16表×100万）
sysbench --db-driver=mysql --mysql-host=mysql --mysql-user=sbuser \
  --mysql-password=sbpass --mysql-db=sbtest --tables=16 \
  --table-size=1000000 --threads=16 oltp_read_write prepare

# 压测（递增并发找拐点）
sysbench --db-driver=mysql ... --threads=N --time=20 oltp_read_only run
sysbench --db-driver=mysql ... --threads=N --time=20 oltp_read_write run
```

## 四、实测结果

### 4.1 只读 (oltp_read_only)

| 线程 | TPS | QPS | avg(ms) | 95th(ms) |
|------|-----|-----|---------|----------|
| 1 | 162 | 2595 | 6.16 | 10.46 |
| 4 | 501 | 8023 | 7.97 | 16.41 |
| 8 | 763 | 12212 | 10.47 | 23.10 |
| 16 | 1469 | 23505 | 10.87 | 25.28 |
| **32** | **1684** | **26953** | 18.94 | 50.11 |
| 64 | 1615 | 25847 | 39.48 | 110.66 |
| 128 | 1579 | 25272 | 80.75 | 219.36 |
| 256 | 1555 | 24893 | 163.35 | 411.96 |

### 4.2 读写混合 (oltp_read_write)

| 线程 | TPS | QPS | avg(ms) | 95th(ms) |
|------|-----|-----|---------|----------|
| 1 | 134 | 2690 | — | — |
| 8 | 308 | 6174 | — | — |
| 16 | 308 | 6174 | — | — |
| 32 | 505 | 10104 | 63.17 | 130.13 |
| 64 | 713 | 14271 | 89.42 | 176.73 |
| **128** | **898** | **17957** | 141.41 | 277.21 |
| 256 | 861 | 17233 | 290.60 | 539.71 |

### 4.3 纯写 (oltp_write_only)

| 线程 | TPS | QPS | avg(ms) | 95th(ms) |
|------|-----|-----|---------|----------|
| 1 | 182 | 1094 | 5.47 | 8.43 |
| 4 | 367 | 2203 | 10.88 | 27.66 |
| 8 | 249 | 1497 | 32.02 | 74.46 |
| 16 | 477 | 2867 | 33.41 | 74.46 |
| 32 | 788 | 4731 | 40.46 | 84.47 |
| 64 | 1258 | 7553 | 50.73 | 106.75 |
| 128 | 1721 | 10331 | 73.77 | 164.45 |
| **256** | **2215** | **13294** | 114.28 | 244.38 |

> 纯写说明：QPS/TPS 持续增长到 256 并发（2215 TPS），无明显下降拐点——因纯写操作分散到 16 张表、锁竞争较小，高并发下吞吐仍能提升；但**延迟快速增长**（avg 5ms→114ms，95th 达 244ms），达到一定并发后收益递减。

### 4.4 写入"管道"模式提速对比（批量 vs 单条）

纯写单条事务（sysbench oltp_write_only）约 1.3万 QPS。用**批量管道**（多值 INSERT / LOAD DATA）可指数级提升写入吞吐。同一张表 `t_write_compare` 实测：

| 写入模式 | 数据量 | 耗时 | 吞吐 | 相对单条 |
|---------|--------|------|------|---------|
| 单条 INSERT（autocommit） | 1万行 | 90s | **111 行/s** | 1x |
| 多值批量 INSERT（batch 1000） | 10万行 | 1.07s | **9.3万 行/s** | **840x** |
| LOAD DATA INFILE | 100万行 | 5.27s | **19万 行/s** | **1700x** |

> **原理**：单条 INSERT 每条是独立 SQL + 独立网络往返 + 独立提交（fsync），开销极大。批量管道（多值 INSERT、LOAD DATA）把**多条写合并成一条 SQL/一次提交**，大幅减少解析、网络、fsync 开销。

**严格同基准验证（同一工具 sysbench + 同一自定义 lua 脚本，仅改 `batch_size`）**：

| batch_size | 事务/s | 每事务行数 | **行/s** |
|-----------|--------|-----------|---------|
| 1（单条） | 445 | 1 | **445** |
| 100 | 358 | 100 | **35862** |
| 1000 | 177 | 1000 | **177940** |

> 同一工具、同一协议、同一表，仅控制批量行数：**batch=1000 比 batch=1 快约 400 倍**（177940 vs 445 行/s）。注意事务数反而下降（每条 SQL 插更多行），但**总行吞吐**暴涨——这就是批量"管道"的价值。
>
> **对比 sysbench 纯写（13294 QPS）**：批量管道快 **7~14 倍**。生产高吞吐写入（日志、埋点、批量导入）应优先用 LOAD DATA 或 JDBC `rewriteBatchedStatements` 多值批量。

#### 生产写入方式选型（LOAD DATA 为什么生产很少用）

LOAD DATA INFILE 在本压测中验证了 MySQL 原生写入上限，但**生产常规写入很少用**：

| 写入方式 | 适用场景 | 是否生产常用 |
|---------|---------|-------------|
| JDBC 批量（rewriteBatchedStatements / 多值） | 常规业务批量写入（订单/流水） | ✅ 常用 |
| 消息队列异步落库（Kafka→消费端批量） | 高并发写入削峰 | ✅ 常用 |
| INSERT ... ON DUPLICATE KEY UPDATE / REPLACE | 幂等 upsert | ✅ 常用 |
| LOAD DATA INFILE | 数仓批量导入、数据迁移、离线灌库 | ⚠️ 特定场景 |
| DataX / Flink CDC / mysqldump | 数据迁移、同步 | ✅ 数据工程 |

> **LOAD DATA 生产少用的原因**：① 需服务端本地文件 + `secure-file-priv` 限制，跨机器（应用与 DB 分离）不现实；② 绕过业务校验/转换逻辑；③ 需 DBA 介入、文件落盘、权限管理，不符合应用流程。
>
> **结论**：LOAD DATA 是"批量装载专用工具"而非通用写入方案。生产常规写入优先 **JDBC 批量**，数据迁移/数仓才用 LOAD DATA 或 DataX 类工具。压测测 LOAD DATA 是为了量化 MySQL 写入理论极限。

## 五、核心结论（硬件上限 & 拐点）

1. **读上限 ≈ 26953 QPS**（并发 32 达峰值），**读拐点在并发 32**：并发超过 32 后吞吐不增反降。
2. **读写混合上限 ≈ 17957 QPS / 898 TPS**（并发 128 达峰值），**读写拐点在并发 128**：256 时降到 861 TPS。
3. **纯写上限于并发 256 时 ≈ 13294 QPS / 2215 TPS**（多表分散、锁竞争小，仍在增长），但延迟已失控（avg 114ms、95th 244ms）。
4. **性能排序（同机）**：只读 2.7万 QPS > 读写混合 1.8万 QPS > 纯写 1.3万 QPS，符合"读快写慢"的硬件特性。
5. **延迟随并发暴涨**（关键 trade-off）：
   - 读：并发 32 时 avg 19ms，256 时 avg 163ms（8.6 倍）。
   - 读写：并发 128 时 avg 141ms，256 时 avg 290ms。
   - 纯写：并发 128 时 avg 74ms，256 时 avg 114ms。
6. **硬件瓶颈表现**：低并发下吞吐近线性扩展，达到上限后增长停滞并回落，延迟快速恶化——典型的资源饱和（CPU/锁/刷盘/网络）拐点。
7. **最大连接数（max_connections=1000）**：压测到并发 256 仍未触及，说明当前瓶颈在 CPU/IO 而非连接数。

## 六、容量参考（推算）

以并发 32 读峰值 26953 QPS、128 读写 898 TPS、256 纯写 2215 TPS 为基准：
- **读场景**：单机可支撑约 **2.7 万读 QPS**（再高延迟不可接受）。
- **写场景（读写混合）**：单机可支撑约 **900 TPS**，超过则延迟显著恶化。
- **纯写场景**：单机可支撑约 **2000+ TPS**，但需接受较高延迟（avg 100ms+）。
- **并发建议**：读并发不宜超 32，读写并发不宜超 128；纯写并发可放宽但需权衡延迟。

## 七、踩坑记录

1. **Docker Hub 不可达**：`severalnines/sysbench` 无法从 docker.io 拉取，改用 `docker.1ms.run` 镜像源。
2. **MySQL 8.4 默认禁用 `mysql_native_password`**：sysbench 客户端基于 mariadb 库不支持 `caching_sha2_password`。解决：MySQL 加 `--mysql-native-password=ON`，创建 sbuser 用 native password。
3. **`max_prepared_stmt_count` 达上限（16382）**：高并发压测累积 prepared statements 报错 1461。解决：`SET GLOBAL max_prepared_stmt_count=65536`。
4. **shell 历史污染**：Windows PowerShell 会话反复重放旧命令，改用 `.ps1` 脚本文件批量执行 + 文件输出结果。

## 八、面试简述

> **单机 MySQL 硬件上限**（16 表 × 100 万行，8.4）：只读上限约 **2.7 万 QPS**（并发 32 达峰值，之后下降）；读写混合上限约 **17957 QPS / 900 TPS**（并发 128 达峰值）；纯写上限于并发 256 达 **13294 QPS / 2200 TPS**（多表分散仍在增长，但延迟失控 avg 114ms）。性能排序：只读 > 读写混合 > 纯写。拐点后延迟暴增（读 256 并发 avg 163ms），这是典型的资源饱和拐点：低并发近线性扩展，到上限后增长停滞回落。为满足更高吞吐需加索引优化、读写分离（读上限翻倍）、分库分表（横向扩容）。
>
> **写入提速（管道/批量）**：单条 INSERT 仅 111 行/s；多值批量 INSERT 提升到 9.3 万行/s（840x）；LOAD DATA INFILE 达 19 万行/s（1700x）。**同工具 sysbench 严格对比**：同一 lua 脚本仅改 batch_size，batch=1 时 445 行/s → batch=1000 时 17.8 万行/s（快约 400 倍）。生产高吞吐写入应优先批量管道。
