---
title: "MySQL（InnoDB）事务与锁排查治理手册"
summary: "适用环境：生产 MySQL 8.4.10（经实测为裁剪/云版本——标准 8.4 应有的 idletransactiontimeout 在本实例报 1193 Unknown syst…"
date: 2026-02-23
author: "小番茄程序猿"
tags:
  - MySQL
  - 事务
  - 锁
category: deep
series: "mysql"
slug: "MySQL-事务与锁排查治理手册"
reading_time: 38
featured: false
draft: false
---
## MySQL（InnoDB）事务与锁排查治理手册

> 适用环境：生产 **MySQL 8.4.10**（经实测为**裁剪/云版本**——标准 8.4 应有的 `idle_transaction_timeout` 在本实例报 `1193 Unknown system variable`，故**所有参数务必先探针确认存在，再 `SET PERSIST`**）。
> 本手册覆盖：生产可执行配置基线、事务与锁原理、排查工具箱（可直接粘贴的 SQL）、治理与处置、预防最佳实践。
> 整理日期：2026-09-22

---

### 目录

1. 使用说明与环境判定
2. 生产常用配置基线（可执行 SQL）
3. 事务、MVCC 与 undo/purge
4. InnoDB 锁体系（含 MDL）
5. 排查工具箱（可执行 SQL）
6. 治理与处置（KILL / MDL / 死锁 / 生产红线）
7. 预防与最佳实践（开发侧 + DBA 侧）
8. 常见坑与注意事项
9. 附录：与 KingbaseES/PostgreSQL 对照

---

## 1. 使用说明与环境判定

**三条使用纪律**：

1. **先探针，再设置**。本实例是裁剪版，别假设「标准 8.4 参数都在」。任何 `SET PERSIST` 前，先用 `SHOW GLOBAL VARIABLES LIKE 'xxx';` 或 `SELECT @@xxx;` 确认存在，否则报 `1193`。
2. **动态 vs 只读**。多数超时/日志/事务参数是**动态**的，可 `SET PERSIST`（写 `mysqld-auto.cnf`，重启保留）；`performance_schema`、`innodb_buffer_pool_size` 之外的部分底层参数、复制架构参数需改 **my.cnf + 重启**。
3. **生产红线**：为处理单个卡死会话，**绝不重启整库**；`SET GLOBAL`/`SET PERSIST` 会影响**新建连接**，改前评估、改后观察。

**权限**：`SET PERSIST`/`SET GLOBAL` 需 `SYSTEM_VARIABLES_ADMIN`（或 SUPER）。云库通常去**控制台「参数设置」**改最稳，不受此限。

**一键体检**（先看现状，再决定配什么）：

```sql
SELECT d.name AS 参数,
       IFNULL(gv.VARIABLE_VALUE,'(不存在)') AS 当前值,
       d.default_value AS 默认值,
       CASE WHEN gv.VARIABLE_VALUE IS NULL THEN '✗ 本build无此参数'
            WHEN gv.VARIABLE_VALUE <=> d.default_value THEN '⚠ 未配置(=默认)'
            ELSE '✓ 已配置' END AS 判定
FROM (
  SELECT 'lock_wait_timeout' name,'31536000' default_value UNION ALL
  SELECT 'innodb_lock_wait_timeout','50' UNION ALL
  SELECT 'max_execution_time','0' UNION ALL
  SELECT 'wait_timeout','28800' UNION ALL
  SELECT 'interactive_timeout','28800' UNION ALL
  SELECT 'slow_query_log','OFF' UNION ALL
  SELECT 'long_query_time','10' UNION ALL
  SELECT 'innodb_print_all_deadlocks','OFF' UNION ALL
  SELECT 'innodb_deadlock_detect','ON' UNION ALL
  SELECT 'transaction_isolation','REPEATABLE-READ' UNION ALL
  SELECT 'autocommit','ON'
) d
LEFT JOIN performance_schema.global_variables gv ON gv.VARIABLE_NAME = d.name
ORDER BY 判定, d.name;
```

> 说明：`idle_transaction_timeout` 系列本 build 无（`✗ 本build无此参数`），改用第 7 节的「连接池 + 定时 KILL」防空闲事务。

---

## 2. 生产常用配置基线（可执行 SQL）

> 分「安全兜底类（建议直接上）」和「按业务评估类（别盲改）」。数值为通用 OLTP 起点，按实际压测微调。

### 2.1 安全兜底类（超时 + 可观测，建议直接 `SET PERSIST`）

```sql
-- ① 锁等待超时：防「一个长事务/DDL 把后面全拖死」
SET PERSIST lock_wait_timeout = 60;              -- 元数据锁(MDL)等锁上限；默认 1 年，必改
SET PERSIST innodb_lock_wait_timeout = 15;       -- 行锁等待上限(秒)；默认 50 偏长

-- ② 慢查询日志：跑飞/慢 SQL 能被发现的【前提】
SET PERSIST slow_query_log = ON;
SET PERSIST long_query_time = 1;                 -- 超过 1s 记为慢查询
SET PERSIST log_output = 'FILE';                 -- 或 'TABLE' 写 mysql.slow_log 便于 SQL 查询
-- SET PERSIST slow_query_log_file = '/var/log/mysql/slow.log';  -- 路径按环境，需可写

-- ③ 死锁留痕：事后能查死锁现场
SET PERSIST innodb_print_all_deadlocks = ON;     -- 所有死锁写入 error log
SET PERSIST innodb_deadlock_detect = ON;         -- 保持开启（默认 ON）

-- ④ 空闲连接超时：与连接池 maxLifetime 对齐（本 build 无 idle_transaction_timeout，靠它+池回收兜底）
SET PERSIST wait_timeout = 600;                  -- 非交互连接空闲 10min 断开
SET PERSIST interactive_timeout = 600;           -- 交互连接同上
```

### 2.2 按业务评估类（改前务必评估，别盲上）

```sql
-- 隔离级别：RR→RC 可显著减少间隙锁/死锁，但改变幻读与复制语义，需业务确认
-- SET PERSIST transaction_isolation = 'READ-COMMITTED';
-- 注意：用 RC 时 binlog_format 必须为 ROW（默认即 ROW）。

-- SELECT 超时（毫秒）：只对 SELECT 生效，会砍掉合法长报表/导出，建议给宽值或只在应用会话设
-- SET PERSIST max_execution_time = 300000;

-- autocommit 保持 ON；应用显式开事务时再 SET autocommit=0，别全局关。
```

### 2.3 持久化与复制安全（my.cnf 为主，生产强相关，按需核对）

> 这些多为**只读/建议 my.cnf** 项，用于「数据不丢 + 主从一致」，不是锁治理，但属生产常用基线，列出供核对：

```ini
# my.cnf [mysqld]
innodb_flush_log_at_trx_commit = 1   # 1=每次提交刷盘，最安全（双 1 之一）
sync_binlog                = 1       # 每次事务刷 binlog（双 1 之一）
binlog_format              = ROW     # 行格式，配 RC/恢复友好
gtid_mode                  = ON      # 若用主从/多源，建议 GTID
enforce_gtid_consistency   = ON
log_slave_updates          = ON
max_connections            = 1000    # 按实例规格，防连接打满
performance_schema         = ON      # 排查锁/等待依赖它（改此项需重启）
```

### 2.4 开启 MDL 监控（排查「改表卡死」必备，一次性）

```sql
-- 启用元数据锁采集 instrument，之后才能用 performance_schema.metadata_locks 查 MDL
UPDATE performance_schema.setup_instruments
SET ENABLED = 'YES', TIMED = 'YES'
WHERE NAME = 'wait/lock/metadata/sql/mdl';
```

### 2.5 一键落地脚本（复制即用，先跑 1 的体检确认无 1193）

```sql
SET PERSIST lock_wait_timeout = 60;
SET PERSIST innodb_lock_wait_timeout = 15;
SET PERSIST slow_query_log = ON;
SET PERSIST long_query_time = 1;
SET PERSIST innodb_print_all_deadlocks = ON;
SET PERSIST wait_timeout = 600;
SET PERSIST interactive_timeout = 600;
UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES'
  WHERE NAME='wait/lock/metadata/sql/mdl';
```

## 2.6 参数详解（逐个说明含义）

### A. 动态参数（可 `SET PERSIST`，影响新建连接）

| 参数 | 含义 | 默认 | 建议 | 作用 / 注意 |
|---|---|---|---|---|
| `lock_wait_timeout` | 等待**元数据锁(MDL)/表级锁**的最长时间（秒），超时语句报错退出 | 31536000（1年） | 60（DDL 会话设 5） | 防「ALTER 无限期卡住」的头号开关。过小会误杀正常等锁语句，过大则卡死无界 |
| `innodb_lock_wait_timeout` | 等待 **InnoDB 行锁**的最长时间（秒），超时报 `1205` | 50 | 15 | 行锁排队上限；配合应用重试 |
| `slow_query_log` | 是否开启**慢查询日志** | OFF | ON | 发现慢/跑飞 SQL 的前提，不开则一切白搭 |
| `long_query_time` | 执行超过该值（秒，可小数）的语句记入慢日志 | 10 | 1~2 | 阈值越低越敏感，但日志量大 |
| `log_output` | 日志输出目标：`FILE`/`TABLE`/`NONE` | FILE | FILE 或 TABLE | `TABLE` 写 `mysql.slow_log`，可直接用 SQL 查分析 |
| `slow_query_log_file` | `FILE` 模式下的慢日志文件路径 | (编译默认) | 按环境 | 需对 mysql 用户可写 |
| `innodb_print_all_deadlocks` | ON 时把**所有**死锁写入 error log（默认只留最后一个） | OFF | ON | 事后定位死锁现场的关键 |
| `innodb_deadlock_detect` | ON 时 InnoDB 主动检测死锁并回滚牺牲者；OFF 则靠锁等待超时 | ON | ON | 一般保持；仅极端热点行场景才评估关闭省开销 |
| `wait_timeout` | **非交互连接**空闲多久被服务端断开（秒） | 28800（8h） | 600 | 与连接池 `maxLifetime` 协调；过小会断正常空闲连接 |
| `interactive_timeout` | **交互式客户端连接**的空闲断开时间（秒） | 28800 | 600 | 同上，针对交互式会话 |
| `transaction_isolation` | 事务隔离级别 | REPEATABLE-READ | 按业务（RC 可减间隙锁） | 改 RC 会改变幻读/锁语义，**非纯安全项，先评估** |
| `max_execution_time` | **SELECT** 最大执行时间（毫秒），超时中断 | 0（不限） | 宽值或会话级 | **只对只读 SELECT 生效**，会砍长报表，谨慎 |
| `autocommit` | 每条语句是否自动提交 | ON | ON | 保持 ON；显式事务用 `BEGIN` |
| `event_scheduler` | 是否启用事件调度器（跑定时巡检/自动 KILL 用） | OFF | ON | 第 7.3 的自动 KILL EVENT 依赖它 |

### B. 只读 / my.cnf 参数（多数需重启，属生产基线核对）

| 参数 | 含义 | 默认 | 建议 | 作用 / 注意 |
|---|---|---|---|---|
| `innodb_flush_log_at_trx_commit` | redo log 刷盘策略：1=每次提交刷盘，2=每秒刷，0=每秒写+刷 | 1 | 1 | 「双 1」之一，=1 最安全（宕机不丢已提交事务） |
| `sync_binlog` | binlog 刷盘：1=每次提交刷，N=每 N 次，0=交给 OS | 1 | 1 | 「双 1」之二，与上者共同保证不丢数据 |
| `binlog_format` | binlog 格式 ROW/MIXED/STATEMENT | ROW | ROW | ROW 恢复/复制最安全；**用 RC 隔离级别必须 ROW** |
| `gtid_mode` | 全局事务标识，便于主从切换/故障转移 | OFF | 用主从则 ON | 需配 `enforce_gtid_consistency=ON` |
| `enforce_gtid_consistency` | 禁止与 GTID 不兼容的语句 | OFF | ON | 配合 gtid_mode |
| `log_slave_updates` | 从库把自己应用的变更也写进自身 binlog | OFF | 级联/切换则 ON | 主主/级联复制、故障切换需要 |
| `max_connections` | 最大并发连接数 | 151 | 按实例规格（如 1000） | 配合连接池上限，防连接打满 |
| `performance_schema` | 性能/锁/等待采集总开关（本手册 5.x 依赖它） | ON | ON | **改此项需重启**；关了则 MDL/锁等待视图查不到 |

> 说明：`SET PERSIST` 写入 `mysqld-auto.cnf`，重启后仍生效；`SET GLOBAL` 只在本次运行期有效、重启丢失。B 组多为只读参数，改 my.cnf 后重启。

---

## 3. 事务、MVCC 与 undo/purge

InnoDB 用 **MVCC**：每行隐藏 `trx_id`（最后修改它的事务）和 `roll_pointer`（指向 undo log 里的旧版本）。一致性读靠 **ReadView**（快照）判断「哪个版本可见」。

- **RC（Read Committed）**：每条 `SELECT` 生成新 ReadView → 能看见别事务已提交的新改动。
- **RR（Repeatable Read，默认）**：事务内**第一条快照读**生成 ReadView 并复用 → 全程可重复读；配合**间隙锁/next-key 锁**防幻读。
- **当前读**（`SELECT ... FOR UPDATE / LOCK IN SHARE MODE`、`UPDATE`、`DELETE`）读最新版本并**加锁**；**快照读**（普通 `SELECT`）不加锁、读 undo 里的历史版本。

**undo / purge 与长事务**：修改产生的旧版本放 undo log，由 **purge 线程**在「没有任何 ReadView 还需要它」时回收。**一个长事务会把 ReadView 钉在很老的位置**，导致：

- undo 无法清理 → **History List Length 飙升**、回滚段膨胀、purge 追不上；
- 主从架构下放大**复制延迟**；
- 极端情况影响性能与空间。

> 治理要点同 PG：**长事务是万恶之源**，靠「短事务 + 及时提交 + 超时兜底 + 定时巡检 KILL」。本 build 无 `idle_transaction_timeout`，见第 7 节替代方案。

**两类「事务挂着」**（处置与预防都不同）：

| 现象 | 特征 | 危害 | 兜底手段 |
|---|---|---|---|
| 跑飞的查询 | `innodb_trx.trx_query` 有内容、`processlist` 非 Sleep | 占资源、钉住 ReadView | `max_execution_time`(仅SELECT) + 慢日志发现 + `KILL` |
| 空闲未提交事务 | `innodb_trx` 有记录但 `trx_query` 为 NULL、`trx_started` 很旧、连接 `Sleep` | 钉住 ReadView、占连接 | 连接池 `maxLifetime` + `wait_timeout` + 定时 `KILL` |

---

## 4. InnoDB 锁体系（含 MDL）

### 4.1 锁的分类（由粗到细）

- **全局锁**：`FLUSH TABLES WITH READ LOCK`（全库只读，做逻辑备份用）、`LOCK INSTANCE FOR BACKUP`。
- **元数据锁 MDL**：**任何访问表的语句自动加**，无需显式声明。DML/查询加 **MDL 读锁（SHARED_READ/WRITE）**，DDL 加 **MDL 写锁（EXCLUSIVE）**。读写互斥 → **一条未提交的长事务/长查询会持有 MDL 读锁，把 `ALTER/DROP` 挡在 `Waiting for table metadata lock`**，且 MDL 有排队优先，会连带堵死后续所有访问该表的语句（**「改不了表」的头号原因**）。
- **表级锁**：`LOCK TABLES ... READ/WRITE`、意向锁 **IS/IX**（InnoDB 行锁与表锁共存的协议）、AUTO-INC 锁（自增分配，受 `innodb_autoinc_lock_mode` 影响）。
- **InnoDB 行级锁**（加在索引记录上）：
  - **记录锁 Record Lock**：锁单行（S/X）。
  - **间隙锁 Gap Lock**：锁索引记录之间的**间隙**（仅 RR），防插入幻读。
  - **Next-Key Lock**：记录锁 + 其前间隙，RR 默认加锁单位。
  - **插入意向锁 Insert Intention**：`INSERT` 前的特殊间隙锁，与 Gap/Next-Key 冲突 → 这是「**没命中索引的 UPDATE/DELETE 会锁很大范围甚至近似锁表**」的根源。

### 4.2 谁与谁冲突（速记）

- **MDL**：读 vs 读共存；读 vs 写互斥；写 vs 一切互斥。→ 长事务(持读) 挡 DDL(要写)。
- **行锁 S/X**：S 与 S 兼容；S 与 X、X 与 X 互斥。
- **间隙锁**：只与「插入意向锁」冲突；**间隙锁之间互相兼容**（两个事务可锁同一间隙，为死锁埋雷）。
- **隔离级别影响**：RC 基本无间隙锁（死锁更少、并发更高）；RR 用 next-key/间隙锁防幻读（更易死锁）。

### 4.3 死锁与等待

- **死锁**：两事务互相持有对方要的锁成环。InnoDB 靠 `innodb_deadlock_detect`（默认 ON）**即时检测**并回滚代价小的一个，报 `ERROR 1213 (40001) Deadlock found`。
- **锁等待超时**：行锁 `innodb_lock_wait_timeout`（默认 50s，报 `ERROR 1205 Lock wait timeout exceeded`）；MDL/表锁 `lock_wait_timeout`。
- 开启 `innodb_print_all_deadlocks=ON` 后，死锁现场写入 error log，可事后定位是哪两条 SQL。

---

## 5. 排查工具箱（可执行 SQL）

> 8.4 用 `performance_schema.data_locks / data_lock_waits`（旧的 `information_schema.INNODB_LOCKS/WAITS` 已移除）。`sys` 库视图最直观，若没装 sys 则用底表版。

**5.1 看会话 / 找卡住的语句**

```sql
-- 非 Sleep 或执行超 60s 的会话
SELECT id, user, host, db, command, time, state, LEFT(info,100) AS query
FROM information_schema.processlist
WHERE command <> 'Sleep' OR time > 60
ORDER BY time DESC;

-- 直接揪出被 MDL 卡住的 DDL
SELECT id, user, time, state, LEFT(info,100) AS query
FROM information_schema.processlist
WHERE state = 'Waiting for table metadata lock';
```

**5.2 看运行中的事务（长事务定位）**

```sql
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_s,
       trx_mysql_thread_id AS processlist_id,
       trx_rows_modified, trx_isolation_level, LEFT(trx_query,100) AS query
FROM information_schema.innodb_trx
ORDER BY trx_started;   -- 最老的在最上；trx_query 为 NULL 多为「空闲未提交事务」
```

**5.3 行锁等待链：谁挡谁**

```sql
SELECT r.trx_mysql_thread_id AS waiting_pid,
       LEFT(r.trx_query,80)  AS waiting_query,
       b.trx_mysql_thread_id AS blocking_pid,
       LEFT(b.trx_query,80)  AS blocking_query,
       b.trx_started         AS blocking_started
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_engine_transaction_id
JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_engine_transaction_id;

-- 或用 sys 视图（更直观，含建议的 kill 语句）
SELECT * FROM sys.innodb_lock_waits;
```

**5.4 看具体锁与持锁对象**

```sql
-- 某事务持/等的锁（8.4）
SELECT engine_transaction_id, thread_id, object_schema, object_name,
       index_name, lock_type, lock_mode, lock_status, lock_data
FROM performance_schema.data_locks
WHERE object_name = '你的表名';
```

**5.5 元数据锁 MDL 全景（需先执行 2.4 启用 instrument）**

```sql
SELECT object_schema, object_name, lock_type, lock_duration, lock_status, owner_thread_id
FROM performance_schema.metadata_locks
WHERE object_type = 'TABLE'
ORDER BY object_schema, object_name, lock_status;

-- 找出「持 MDL 读锁不放、正挡住 DDL」的元凶线程
SELECT ml.object_name, ml.lock_type, t.processlist_id, t.processlist_user,
       t.processlist_command, t.processlist_time, LEFT(t.processlist_info,80) AS query
FROM performance_schema.metadata_locks ml
JOIN performance_schema.threads t ON t.thread_id = ml.owner_thread_id
WHERE ml.object_type='TABLE' AND ml.lock_status='GRANTED'
  AND t.processlist_id IS NOT NULL;
```

**5.6 死锁现场**

```sql
SHOW ENGINE INNODB STATUS\G   -- 看 LATEST DETECTED DEADLOCK 段
-- 或（开启 innodb_print_all_deadlocks 后）直接查 error log
```

**5.7 一键巡检（长事务 + 空闲事务 + 锁等待 汇总）**

```sql
SELECT '长事务' AS kind, trx_mysql_thread_id AS pid, trx_started,
       TIMESTAMPDIFF(SECOND,trx_started,NOW()) AS age_s,
       IF(trx_query IS NULL,'空闲未提交','执行中') AS sub, LEFT(trx_query,80) AS query
FROM information_schema.innodb_trx
WHERE TIMESTAMPDIFF(SECOND,trx_started,NOW()) > 300
UNION ALL
SELECT '锁等待', r.trx_mysql_thread_id, r.trx_started,
       TIMESTAMPDIFF(SECOND,r.trx_started,NOW()), '被阻塞', LEFT(r.trx_query,80)
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx r ON r.trx_id=w.requesting_engine_transaction_id
ORDER BY age_s DESC;
```

## 6. 治理与处置（KILL / MDL / 死锁 / 生产红线）

**6.1 终止会话**

```sql
KILL 12345;        -- 断开连接并回滚其事务（相当于 pg_terminate_backend）
KILL QUERY 12345;  -- 只中断当前语句，保留连接（相当于 pg_cancel_backend）
```

- 杀自己的连接无需特权；杀别人的需 `CONNECTION_ADMIN`（或 SUPER）。
- **`KILL` 也可能「杀不动」**：若线程正卡在引擎底层 I/O 或大事务回滚中，`processlist` 会显示 `Killed` 状态但迟迟不退出——这是回滚在进行，**别反复 KILL、别重启**，等回滚完成。大事务回滚耗时与其改动量成正比。

**6.2 「改不了表」= MDL 阻塞的标准处置流程**

1. 用 5.1 找到 `Waiting for table metadata lock` 的那条 DDL（它是受害者）。
2. 用 5.5 找出**持有该表 MDL 读锁**的元凶 `processlist_id`（通常是某个长查询或未提交事务）。
3. `KILL <元凶id>;` → MDL 释放 → 排队的 DDL 立即继续。
4. 若 DDL 后面已堆了一串查询（MDL 排队雪崩），杀掉元凶后整条链会瞬间疏通。

**6.3 死锁处置**

- 偶发死锁：InnoDB 已自动回滚一方，应用**捕获 1213 做退避重试**即可，不必人工。
- 频发死锁：开 `innodb_print_all_deadlocks`，从 error log 看是哪两条 SQL 成环 → 通常靠「**统一加锁顺序**、让 `UPDATE/DELETE` **走索引精确命中**缩小锁范围、必要时 RR→RC」根治。

**6.4 生产红线**

- **绝不为杀一个会话重启整库**（打断真实业务）。重启只用于可丢数据/无业务场景。
- `KILL` 前用 5.2/5.5 核对来源（`user`/`host`/`db`/`query`），确认是元凶而非误伤。
- 强杀**大写入事务**会触发回滚，回滚期间负载可能短暂升高——只读事务无此顾虑。
- 别在业务高峰跑 `SHOW ENGINE INNODB STATUS` 之外的重诊断；`performance_schema` 查询相对轻。

---

## 7. 预防与最佳实践

**7.1 开发侧（Java 后端重点）**

- **事务尽量短**：`@Transactional` 方法里**不要夹 RPC / HTTP / Redis / MQ / 文件 IO / 慢计算**——这些把事务撑长，直接放大锁持有与 undo 无法回收。把外部调用挪到事务外。
- **及时提交/回滚**：异常路径别把连接留在「开事务未提交」；确保 try/finally 或框架代理一定走到 commit/rollback。
- **`UPDATE/DELETE` 必须走索引**：没命中索引会退化成大范围 next-key 锁甚至近似锁表，是死锁与「改不动」的高发原因。上线前 `EXPLAIN` 确认 `key` 命中。
- **`SELECT ... FOR UPDATE` 谨慎**：锁行且持到事务结束，务必索引命中 + 短事务；考虑用乐观锁（版本号）替代部分悲观锁场景。
- **批量写分批提交**：百万级 `INSERT/UPDATE` 拆小批、每批一提交，别在一个事务里跑到底（undo 暴涨 + 回滚代价大）。
- **连接池配置**：HikariCP `maxLifetime` **小于** MySQL `wait_timeout`（如 600s → 池设 10min 内），配 `idleTimeout`、`connectionTimeout`；避免池里连接被服务端单方面 kill 后仍被复用报「通信链路失败」。
- **死锁/锁超时重试**：对 `1213`(deadlock)、`1205`(lock wait timeout) 做**有限次指数退避重试**。

**7.2 DDL / 变更**

- 改结构前先 `SET SESSION lock_wait_timeout = 5;`，拿不到 MDL 快速失败、错峰重试，**别让它排队堵死全表**（本次教训核心）。
- 大表结构变更用 **`gh-ost` 或 `pt-online-schema-change`** 在线改，避免 `ALTER` 长时间持 MDL 写锁。
- 建索引用 `ALGORITHM=INPLACE, LOCK=NONE` 的在线 DDL（8.0 多数场景默认支持），仍会有短暂的 MDL 切换，避开高峰。

**7.3 DBA / 监控侧**

- **定时巡检长事务/空闲事务**（第 5.7 的 SQL），超阈值（如 >5min）告警到运维群，把「挂两天」扼杀在「挂十分钟」。
- **本 build 无 `idle_transaction_timeout` 的替代方案**——用 MySQL Event（或外部 crontab）定期自动 KILL 空闲未提交事务：

```sql
-- 需 event_scheduler=ON（SET PERSIST event_scheduler=ON; 或 my.cnf）
DELIMITER $$
CREATE EVENT IF NOT EXISTS ev_kill_idle_trx
ON SCHEDULE EVERY 1 MINUTE
DO BEGIN
  DECLARE done INT DEFAULT 0; DECLARE v_id BIGINT;
  DECLARE c CURSOR FOR
    SELECT trx_mysql_thread_id FROM information_schema.innodb_trx
    WHERE trx_query IS NULL
      AND TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 300;  -- 空闲事务超 5min
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;
  OPEN c; read_loop: LOOP
    FETCH c INTO v_id; IF done=1 THEN LEAVE read_loop; END IF;
    SET @s = CONCAT('KILL ', v_id); PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
  END LOOP; CLOSE c;
END$$
DELIMITER ;
```

> ⚠ 自动 KILL 有误杀风险：先只「告警」观察一段时间，确认阈值合理再启用自动 KILL；只读快照事务通常不在 `innodb_trx` 里，影响面小。
- **慢查询定期分析**：`pt-query-digest` 跑慢日志，抓 Top SQL 优化。
- **autovacuum 类比项**：InnoDB 无 autovacuum，但要盯 **History List Length**（`SHOW ENGINE INNODB STATUS` 的 TRANSACTIONS 段），持续升高＝有长事务拖住 purge。

---

## 8. 常见坑与注意事项

- **本实例是裁剪/云版**：`idle_transaction_timeout` 等标准参数可能缺失（`1193`）。**任何参数先探针再设**，别照搬标准 8.4 清单。
- **`max_execution_time` 只管 `SELECT`**，**拦不住跑飞的 `UPDATE/DELETE`**——大 DML 无语句级超时兜底，只能靠「走索引 + 分批 + `KILL`」。
- **`lock_wait_timeout` 默认 1 年**＝「ALTER 无限期卡住」的元凶，务必调小（如 60s，DDL 会话级设 5s）。
- **MDL 排队优先**：一个等独占锁的 DDL 会把后续所有该表查询堵死，雪崩比 PG 更突然——先杀元凶再谈优化。
- **间隙锁 / next-key**（RR 特有）是死锁高发源，PG 基本没有；`UPDATE` 不走索引会锁一大片。
- **`KILL` 显示 `Killed` 却不退**＝大事务正在回滚，耐心等，别反复 KILL、别重启。
- **长事务 × 复制**：主从下长事务/大 DDL 放大复制延迟，且从库 `START SLAVE UNTIL`/GTID 恢复更麻烦。
- **`performance_schema` 关闭时**很多锁/等待视图查不到（本手册 5.x 依赖它），确认 `performance_schema=ON`。
- **`SHOW PROCESSLIST` 默认截断 `info`**，看完整 SQL 用 `SHOW FULL PROCESSLIST` 或查 `information_schema.processlist` / `performance_schema.threads`。
- **权限受限账号**：改 `SET PERSIST`/`GLOBAL`、`KILL` 别人的连接都需相应权限；云库优先走控制台参数与会话管理。

---

## 9. 附录：与 KingbaseES / PostgreSQL 对照

| 维度 | MySQL (InnoDB) | KingbaseES / PostgreSQL |
|---|---|---|
| 默认隔离级别 | REPEATABLE-READ | READ COMMITTED |
| 普通 SELECT | 不加行/表锁，但持 **MDL 读锁** | 加 `AccessShareLock`（挡 DDL） |
| 锁体系 | 意向锁 + 记录/间隙/next-key 行锁 + MDL | 8 级表锁 + 行锁(xmax) + 咨询锁 |
| 长事务危害 | 钉住 ReadView → undo/purge 膨胀、HLL 升高、复制延迟 | 拖住 xmin → autovacuum 清不动 → 表膨胀、xid 回卷、复制槽 WAL 堆积 |
| 看会话 | `SHOW PROCESSLIST` / `information_schema.processlist` | `pg_stat_activity` |
| 看事务 | `information_schema.innodb_trx` | `pg_stat_activity.xact_start` |
| 看锁等待 | `performance_schema.data_lock_waits` / `sys.innodb_lock_waits` | `pg_locks.granted=false` |
| 看 MDL | `performance_schema.metadata_locks` | （PG 无 MDL，用表锁 `AccessShareLock` 表达同类阻塞） |
| 看死锁 | `SHOW ENGINE INNODB STATUS` + error log | 日志 + `pg_stat_activity` |
| 杀会话 | `KILL <id>` / `KILL QUERY <id>` | `pg_terminate_backend` / `pg_cancel_backend` |
| SELECT 超时 | `max_execution_time`(仅SELECT) | `statement_timeout`(含DML/DDL) |
| 行锁等待 | `innodb_lock_wait_timeout` | `lock_timeout` |
| MDL 等待 | `lock_wait_timeout` | `lock_timeout` |
| 空闲事务超时 | `idle_transaction_timeout`(本build无) | `idle_in_transaction_session_timeout` |
| 后台回收 | InnoDB purge；收缩靠 `OPTIMIZE TABLE`/重建 | autovacuum 自动 |

> 详细 PG/KingbaseES 侧见同目录《KingbaseES-锁与事务排查手册.md》。

---

> 维护说明：参数取值按实际业务量级压测后微调，先分档、后迭代；本 build 缺失的参数以探针结果为准。
