---
title: "MySQL 面试精华 & 脑图速记"
summary: "基于《MySQL四层架构与监控汇总》提取的面试精华版。用于快速记忆核心考点。 > 面试主攻：四层架构、连接层指标辨析、SQL执行/两阶段提交、BufferPool、长连接与僵尸连接…"
date: 2026-03-06
author: "小番茄程序猿"
tags:
  - MySQL
category: deep
series: "mysql"
slug: "MySQL面试精华与脑图速记"
reading_time: 17
featured: false
draft: false
---
# MySQL 面试精华 & 脑图速记

> 基于《MySQL四层架构与监控汇总》提取的**面试精华版**。用于快速记忆核心考点。
> 面试主攻：四层架构、连接层指标辨析、SQL执行/两阶段提交、BufferPool、长连接与僵尸连接、监控排查。

---

## 脑图（快速记忆框架）

```
MySQL 面试精华
│
├─ ① 四层架构
│    连接层→SQL层→InnoDB→文件系统
│    每层可监控，监控工具不同
│    版本差异：以 show variables/status 为准
│
├─ ② 连接层指标辨析（易混淆）
│    Threads_connected  当前连接数(含Sleep)
│    Threads_created    累计建线程(只增)
│    Aborted_connects   登录失败(未连上)
│    Aborted_clients    登录后异常断开
│    Thread_cache_hits  线程缓存命中
│    ★判定用 delta 增量
│
├─ ③ SQL 执行（正向/反向）
│    正向：连接→解析→优化→执行→InnoDB→磁盘
│    反向：磁盘→InnoDB→Server→连接→客户端
│    ★binlog属Server层，redo/undo属InnoDB层
│    ★BufferPool命中不穿透四层到磁盘
│
├─ ④ 两阶段提交(2PC)
│    Prepare(redo落盘PREPARED)→Commit(写binlog→redo标COMMIT)
│    崩溃恢复：redo COMMIT直接提交；PREPARED看binlog是否完整
│    ★commit不刷脏页，只刷redo/binlog
│    ★group commit优化，非每事务单独fsync
│
├─ ⑤ BufferPool
│    按页加载/驱逐(16KB)，缓存ibd数据页
│    3链表：Free空闲/LRU冷热/Flush脏页
│    脏页≠redo：脏页是数据页，redo是日志
│    脏页4种刷盘：后台定时/LRU淘汰/redo满/关闭
│    ★大字段(BLOB/TEXT)用溢出页，按需加载
│    ★无索引字段跟着聚簇页进BP
│
├─ ⑥ 长连接/僵尸连接
│    僵尸连接=断电断网(RST发不出)遗留的Sleep
│    kill-9发RST→不残留
│    三层保活：Hikari心跳(应用)/OS keepalive(内核)/wait_timeout(兜底)
│    ★机器死机只剩wait_timeout兜底
│    ★Nacos只管流量不管数据库连接
│
└─ ⑦ 监控/排查口诀
    连接飙=连接层；filesort多=SQL层；
    BP命中低/锁等待=InnoDB层；磁盘await高=文件层
    诊断SQL：processlist/innodb_trx/innodb_lock_waits
```

---

## ① 四层架构 & 各层监控

### 脑图
```
连接层 ─ 连接/线程/认证/权限
   ↓
SQL服务层 ─ 解析/优化器/执行器/binlog
   ↓
InnoDB ─ BufferPool/锁/事务/redo/undo/ChangeBuffer
   ↓
文件系统层 ─ 磁盘IO/文件/页刷盘/DoubleWrite
```

### 精华速记
- 四层**全部可监控**，工具不同：`show status` / `show engine innodb status` / `information_schema` / `performance_schema` / `slow log`。
- **版本差异**：不同发行版功能（线程池、事件名、状态变量名）不同，实际以 `show variables/status` 为准。

### 各层监控核心指标

| 层 | 关键指标 |
|----|---------|
| 连接层 | Threads_connected、Threads_created、Aborted_*、Thread_cache_hits |
| SQL层 | Com_*、Sort_*、Created_tmp_*、Binlog_cache_disk_use |
| InnoDB层 | Innodb_buffer_pool_*、Innodb_log_waits、Innodb_trx_* |
| 文件层 | iostat/sar、Innodb_dblwr_writes、Innodb_pages_written |

### 瓶颈口诀
```
连接飙/大量Abort → 连接层
filesort多/磁盘临时表 → SQL层
BP命中低/锁等待/log_waits>0 → InnoDB层
所有SQL慢+磁盘await高 → 文件系统层
```

---

## ② 连接层指标辨析（高频易混淆）

| 指标 | 含义 | 判定要点 |
|------|------|---------|
| **Threads_connected** | 当前已建立连接数（含 Sleep） | 不是运行中事务数；看活跃用 `processlist`/`innodb_trx` |
| **Threads_created** | 累计创建 OS 线程，只增 | 看**上涨速率**，短连接频繁重连会飙升 |
| **Aborted_connects** | 连接建立阶段失败 | 密码错/max_connections 打满/网络握手失败 |
| **Aborted_clients** | 登录后异常断开 | 含**超时被 MySQL 踢掉**（wait_timeout） |
| **Thread_cache_hits** | 线程缓存命中 | 命中率用 **delta 增量**算，不用绝对值 |

> **线程缓存 ≠ 连接池**：线程缓存缓存的是 OS 线程，不是 TCP 连接。
> **delta 原则**：`Threads_connected`、`Innodb_buffer_pool_pages_dirty` 等当前量用当前值；累计计数（Threads_created、Innodb_*_reads 等）用 delta 算速率。

---

## ③ SQL 执行顺序

### 脑图
```
正向执行：
  连接层(TCP/认证) → SQL层(解析/优化/执行) → InnoDB(BP/锁/MVCC) → 文件层(磁盘IO)
反向回传：
  文件层 → InnoDB → SQL层(组装结果) → 连接层(网络返回)
```

### 精华速记
- **binlog 属 Server 层**；**redo/undo 属 InnoDB 层**（最易考）。
- 层与层是**函数调用**，上层不直接操作下层文件。
- **不是每条 SQL 都穿透四层**：BufferPool 命中则 InnoDB 直接返回，不访问磁盘。
- 隔离级别、MVCC、行锁都发生在 **InnoDB 层**，Server 层不知道。
- 执行计划在 **SQL层优化器**；索引查找/锁/MVCC 在 **存储引擎层**；磁盘读写在 **文件系统层**。

---

## ④ 两阶段提交（2PC，关键考点）

### 脑图
```
commit:
  InnoDB Prepare: redo落盘 标记PREPARED
      ↓
  Server写binlog并刷盘(受sync_binlog)
      ↓
  InnoDB: redo标记 COMMIT
      ↓
  返回成功；脏页后台异步刷ibd
```

### 精华速记
- 目的：保证 **redo(InnoDB) 与 binlog(Server)** 一致。
- **崩溃恢复**：
  - redo=COMMIT → 直接提交。
  - redo=PREPARED → 查 binlog 是否完整：完整则 apply，否则 rollback。
- **commit 不刷脏页到 ibd**，只刷 redo/binlog；脏页后台刷。
- **undo 不参与 2PC**，只用于回滚和 MVCC。
- **group commit**：高并发小事务合并 fsync，非每事务单独刷。
- 最安全组合：`innodb_flush_log_at_trx_commit=1` + `sync_binlog=1`。
- 不是分布式 XA 的 2PC，别混淆。

---

## ⑤ BufferPool & 脏页（核心）

### 脑图
```
BufferPool(按页16KB加载/驱逐)
 ├─ Free链表: 空闲页
 ├─ LRU链表: 冷热页(young热/old冷)
 └─ Flush链表: 脏页(内存改没刷盘)
缓存: 表数据页/二级索引页/undo页/AHI页/ChangeBuffer页
不缓存: redo(独立Log Buffer)/binlog(Server层)
```

### 精华速记
- **脏页 ≠ redo**：脏页是**被改的数据页**（最终刷 ibd）；redo 是**日志**（用于恢复）。commit 刷 redo/binlog，不刷脏页。
- **脏页 4 种刷盘**：后台定时(75%)、LRU 淘汰(会阻塞)、redo 快满(强制)、数据库关闭。
- **控制参数**：innodb_max_dirty_pages_pct、innodb_max_dirty_pages_pct_lwm、innodb_page_cleaners。

### BufferPool & 无索引字段
- BP 按**页**加载，一页里所有列（含无索引字段，大字段除外）全进内存。
- 聚簇索引叶子页含整行 → 访问主键或回表会加载整页。
- 二级索引页仅索引键+主键，回表才加载聚簇页。
- **大字段(BLOB/TEXT)用溢出页，按需加载**。
- **覆盖索引**避免回表，省 IO 和 BP 占用。

---

## ⑥ 长连接 / 僵尸半开连接（实务）

### 脑图
```
僵尸连接产生：
  kill-9/正常关机 → 发RST → MySQL回收，无残留
  断电/断网/内核panic → 发不出RST → 半开Sleep僵尸连接

三层保活（互不替代，分层防护）：
  Hikari keepalive(应用层SQL心跳) → 进程活着才有效
  OS TCP keepalive(内核) → 机器活着但网络断才有效
  MySQL wait_timeout(兜底) → 任何场景生效
  ★机器死机：只剩wait_timeout兜底
  ★Nacos/Actuator：只管流量，不管数据库连接
```

### 精华速记
- **参数约束链**：`keepalive-time < idle-timeout < max-lifetime < wait_timeout`。
- **maxLifetime 到期**不打断正在跑的 SQL，仅在归还时销毁；有随机抖动防雪崩。
- **leakDetectionThreshold**：检测连接泄漏。
- 云环境靠**应用层心跳 + wait_timeout**，OS keepalive 会被 NAT/防火墙丢弃。
- **不要用 autoReconnect** 掩盖真实断连问题。

### 高频面试 Q&A
- **为什么报 Communications link failure？** wait_timeout 关闭空闲连接，连接池不知道继续复用。→ idleTimeout < wait_timeout + 心跳。
- **kill-9 会残留僵尸连接吗？** 不会（发 RST）；断电断网才会。
- **Hikari 心跳能清僵尸连接吗？** 不能，进程死了 JVM 停；靠 OS keepalive 或 wait_timeout。
- **Nacos 下线会释放 MySQL 连接吗？** 不会，只摘流量。

---

## ⑦ 监控 / 排查速查

### 脑图
```
诊断SQL：
  processlist(活跃SQL) → innodb_trx(事务) → innodb_lock_waits(阻塞链)
  events_waits_summary_global_by_event_name(等待热点)
监控告警：
  Threads_connected > 0.8*max → 报警
  delta(Threads_created) 持续高 → 短连接压力
  Aborted_connects 突增 → 认证/连接数
  磁盘临时表比率>5% → 慢SQL
  log_waits>0 → redo瓶颈
  iostat await>10ms → 磁盘IO
```

### 精华速记
- 累计计数指标用 **delta**，当前量用当前值。
- 阻塞链查询（必背）：
```sql
SELECT r.trx_id waiting, b.trx_id blocking
FROM information_schema.innodb_lock_waits w
JOIN innodb_trx b ON w.blocking_trx_id=b.trx_id
JOIN innodb_trx r ON w.requesting_trx_id=r.trx_id;
```
- 版本检查：`show variables like 'version%'` / `'thread_cache_size'` / `'innodb_flush_log_at_trx_commit'` / `'sync_binlog'`。

---

## ⑧ 单体 MySQL 硬件上限 & 写入提速（Sysbench 压测）

### 脑图
```
压测(16表×100万行≈1600万行, mysql8.4, bp=1G):
  只读 oltp_read_only     → 上限 2.7万QPS  拐点并发32
  读写混合 oltp_read_write→ 上限 1.8万QPS/900TPS 拐点并发128
  纯写 oltp_write_only    → 上限 1.3万QPS/2200TPS 仍增长但延迟失控
写入提速(批量"管道"):
  单条INSERT 111行/s → 多值批量 9.3万行/s(840x) → LOAD DATA 19万行/s(1700x)
  同工具sysbench严格对比: batch=1 445行/s → batch=1000 17.8万行/s(400x)
```

### 精华速记
- **性能排序（同机）**：只读 > 读写混合 > 纯写（读快写慢）。
- **拐点规律**：低并发近线性扩展，到上限后增长停滞回落，延迟暴增（资源饱和）。
- **读拐点并发 32**，读写拐点并发 128（延迟 avg 已 141ms）。
- **批量提速核心**：把多条写合并成一条 SQL/一次提交，减少解析、网络、fsync 次数。
- **生产写入选型**：常规业务用 JDBC 批量（rewriteBatchedStatements）；高并发用 MQ 异步落库；数据迁移/数仓才用 LOAD DATA 或 DataX。

### 高频面试 Q&A
- **LOAD DATA INFILE 生产常用吗？** 不常用。需服务端本地文件 + secure-file-priv，绕过业务校验，需 DBA 介入；是"批量装载专用工具"。
- **纯写为什么比读慢？** 写要落盘/刷 redo、页分裂、索引维护；读只需查缓存页。
- **拐点后吞吐为什么下降？** CPU/锁/刷盘/网络饱和，延迟加剧导致有效吞吐回落。

---

## ⑨ 数据同步工具对比：mysqldump / DataX / Flink CDC

### 脑图
```
mysqldump = 备份全量（离线单机，兜底）
DataX     = 离线批量异构同步（搬数据，并发分片吞吐高）
Flink CDC = 实时 binlog 增量同步（实时抄数据，秒级延迟）
选型: 备份用mysqldump / 离线搬数用DataX / 实时同步用Flink CDC
```

### 精华速记
- **mysqldump**：全量逻辑备份，`--single-transaction` 靠 MVCC 避免锁表，`--master-data` 记 binlog 位点配合建从库。
- **DataX**：读插件→并发 Channel→写插件，支持断点续传，异构（Oracle→MySQL→HDFS），离线需定时调度。
- **Flink CDC**：伪装从库解析 binlog（Debezium 内核），秒级实时、精确一次，需 ROW 格式 + GTID。
- **数据一致性分级**：mysqldump 全量一致性快照；DataX 批式（配合时间戳增量）；Flink CDC 流式精确一次。

### 高频面试 Q&A
- **三个工具怎么选？** 备份兜底用 mysqldump；跨库/数仓离线批量用 DataX；实时数仓/实时同步用 Flink CDC。
- **Flink CDC 依赖什么？** binlog（ROW 格式 + GTID），否则无法捕获行级变更。
- **mysqldump 大表会锁库吗？** InnoDB + `--single-transaction` 不锁；MyISAM 会锁表。

---

## 面试一句话总结

> **四层全可监控**；**binlog 归 Server、redo/undo 归 InnoDB**；**2PC 保 redo/binlog 一致**；**BP 按页缓存**；**commit 刷 redo 不刷脏页**；**僵尸连接靠 wait_timeout 兜底**；**累计指标用 delta**；**单机读上限 2.7万QPS 拐点并发32**；**写比读慢**；**批量管道提升写入 400x**；**备份用 mysqldump、离线搬数用 DataX、实时同步用 Flink CDC**。
