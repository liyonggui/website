---
title: "KingbaseES / PostgreSQL 锁与长事务排查手册"
summary: "适用环境：KingbaseES（金仓，PostgreSQL 兼容）生产库 zhcl，运维账号 dgclylf，客户端 KStudio，经 aTrust 零信任 VPN 连接。 > …"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - MySQL
  - Elasticsearch
  - 事务
  - 锁
category: deep
series: "mysql"
slug: "KingbaseES-锁与事务排查手册"
reading_time: 42
featured: false
draft: false
---
## KingbaseES / PostgreSQL 锁与长事务排查手册

> 适用环境：KingbaseES（金仓，PostgreSQL 兼容）生产库 `zhcl`，运维账号 `dgcl_ylf`，客户端 KStudio，经 aTrust 零信任 VPN 连接。
> 本手册既是本次「卡死事务」事件的复盘，也是可长期复用的锁/事务排查 runbook。
> 整理日期：2026-09-22

---

### 目录

1. 案例回顾：一个挂了 2 天的卡死事务
2. 事务、快照与 MVCC —— 为什么「长事务」是万恶之源
3. 锁体系（8 级表锁 + 冲突矩阵 + 其它锁）
4. 排查工具箱（可直接粘贴的 SQL）
5. 处置手册（从温和到强制，含生产红线）
6. 预防：三个超时参数的推荐配置
7. 实际开发常用设置与最佳实践
8. 注意事项 / 常见坑
9. 本次案例待办 Checklist
10. 附录：MySQL（InnoDB）对照速查

---

## 1. 案例回顾：一个挂了 2 天的卡死事务

**现象**：在 KStudio 里想编辑某张表（改结构 / DDL），一直执行不了、卡住。

**定位过程**（每一步用的关键 SQL 见第 4 节）：

1. 查 `pg_stat_activity` 发现一个 `pid=21634`、`usename=dgcl_ylf`、`state=active` 的会话，**事务时长已达 2 天**，`wait_event_type` 为空。
2. 查 `pg_locks` 发现它持有的一堆锁**全是 `AccessShareLock`**（普通 SELECT 的锁），外加一条 `virtualxid` 的 `ExclusiveLock`（正常现象，非元凶）。
3. 查阻塞链 `pg_blocking_pids()` 返回**空** → 说明它当前**没有阻塞任何其它会话**，业务读写不受影响。
4. 查会话详情：`application_name = KStudio - SQLEditor <Script-30.sql>`、`client_addr = 19.104.4.128`（aTrust VPN 出口 IP，不是本机局域网 IP）。SQL 全文是一条查 `information_schema` 的**表结构校验脚本**（验证三张表 / 三个视图 / 九个索引是否齐全）。

**根因**：有人（部署这套尚未上线的新表结构时）在 KStudio 里跑了一条元数据校验 `SELECT`，窗口一直没关，查询**跑飞 / 卡死**（`state=active` 且 `wait_event` 空 = 进程在 CPU 上却出不来，属于 wedged 状态）。一条查字典视图的 SQL 正常几秒就该结束，能挂 2 天说明后端进程已卡死、且**对终止信号不响应**。

**影响判读**：

- 对**业务**：几乎无即时影响——只读、未阻塞他人、只挡你对那三张「未上线」表做 DDL。
- 对**库**：作为**长事务**，它抱着 2 天前的快照，**挡住 autovacuum 回收全库死亡元组**（热表持续膨胀），并**白占一个 CPU 核**；长期放任还有事务号（xid）回卷风险。

**处置结论**：这是生产库，**不能为杀一个会话而重启整库**；应让有权限的人**精准 `kill -9` 这一个 backend**（只断这一条连接，安全）。个人账号权限受限（改不了库、改不了角色、SQL 杀不动），实际推进方式是「用新表名继续干活 + 把 kill 请求连同量化证据发给运维」。

**教训**：一次「跑飞不关的只读查询」在**生产**上就是慢性毒药；根治靠**超时参数**（第 6 节）+ **部署完关掉 KStudio 校验窗口**的纪律。

---

## 2. 事务、快照与 MVCC —— 为什么「长事务」是万恶之源

KingbaseES/PostgreSQL 用 **MVCC**（多版本并发控制）：每行数据有多个版本，事务靠一个**快照（snapshot）**决定「看得见哪些版本」。快照由几个事务号边界界定，其中关键的是 **xmin**（这个事务能看到的最老事务边界）。

**oldest xmin horizon（最老事务边界）**：整个数据库里，autovacuum 能清理死亡元组的「进度条」，被**当前所有活跃事务中最老的那个快照**卡住。只要有一个长事务开着，它把 horizon 死死拖在 2 天前，于是：

- 这 2 天里所有被 `UPDATE/DELETE` 产生的**死亡元组（dead tuples）都无法回收** → 表和索引**持续膨胀** → 扫描变慢、缓存命中率下降、`last_autovacuum` 追不上。
- 事务号（32 位）会回卷。系统靠 `age(datfrozenxid)` 逼近 `autovacuum_freeze_max_age`（默认 20 亿）时强制 vacuum；长事务会加速逼近，极端情况触发回卷保护、库变只读甚至拒启。
- 若有**逻辑复制槽 / 物理复制**，长事务还会**拖住 WAL 无法回收** → WAL 堆积**撑爆磁盘**（生产重大事故常见诱因）。

**两种「事务挂着」要分清，治理参数也不同**：

| 状态 | 含义 | 危害 | 治理参数 |
|---|---|---|---|
| `state=active` | 语句**正在执行**（跑飞/卡死） | 占 CPU、抱快照 | **`statement_timeout`** |
| `idle in transaction` | 开了事务、**人走了/忘了提交** | 抱快照、占连接 | **`idle_in_transaction_session_timeout`** |

> 本次案例是 `active`（跑飞），所以真正能自动治它的是 `statement_timeout`；`idle_in_transaction_session_timeout` 治的是另一种更常见的「BEGIN 不 COMMIT」。两个都要配。

---

## 3. 锁体系

### 3.1 八级表锁与冲突矩阵

行 = 已持有的锁，列 = 别人想申请的锁；**✕ = 冲突（需排队等待），· = 兼容（可共存）**。矩阵对称。

| 已持有 \ 想申请 | AS | RS | RX | SUE | S | SRE | E | AE |
|---|---|---|---|---|---|---|---|---|
| **AS** AccessShare（`SELECT`） | · | · | · | · | · | · | · | ✕ |
| **RS** RowShare（`SELECT FOR UPDATE`） | · | · | · | · | · | · | ✕ | ✕ |
| **RX** RowExclusive（`INSERT/UPDATE/DELETE`） | · | · | · | ✕ | ✕ | ✕ | ✕ | ✕ |
| **SUE** ShareUpdateExclusive（`VACUUM/ANALYZE/CREATE INDEX CONCURRENTLY`） | · | · | ✕ | ✕ | · | ✕ | ✕ | ✕ |
| **S** Share（`CREATE INDEX` 非并发） | · | · | ✕ | · | · | ✕ | ✕ | ✕ |
| **SRE** ShareRowExclusive（加外键/唯一约束） | · | · | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ |
| **E** Exclusive（`REFRESH MATVIEW CONCURRENTLY` 等） | · | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ |
| **AE** AccessExclusive（`DROP/TRUNCATE/ALTER/VACUUM FULL/REINDEX/CLUSTER`） | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ | ✕ |

**秒判规律**：

- `AS` 那一行只有最右列是 ✕ → **读操作只被 DDL（AE）挡住**，与一切读写共存。这就是本次卡死 SELECT 挡不住业务、只挡改表结构的原因。
- `AE` 那一整行全 ✕ → 一个 `ALTER/DROP` 拿到锁会停全表读写；反过来它也要等所有人放手才能拿到，**最容易被长事务拖住**。
- `RX` 之间互相 · → 多个增删改可并发（行级另论）。
- 越靠右下越霸道。

### 3.2 其它锁类型（`pg_locks.locktype` 维度）

- **行级锁（tuple lock）**：`UPDATE/DELETE/SELECT FOR UPDATE` 锁到具体行，记在行的 `xmax` 上，不单独按表显示；别人等同一行时表现为 `transactionid` 锁等待。
- **咨询锁 advisory lock**：`pg_advisory_lock(key)`，由应用自定义含义、数据库只保证互斥；分会话级/事务级。排查时易忽略。
- **事务号锁 `transactionid` / `virtualxid`**：内部事务标识锁，每个活跃事务都有一条 `virtualxid` 的 `ExclusiveLock`，**正常、不锁表、不是元凶**。
- **对象/元数据锁**：`database`、`relation`、`partition`、`index`、`object`、`extension`、`foreign_key`、`server` 等，锁库/分区/扩展/外键等对象。
- **`fastpath`**：轻量函数调用锁，一般不用管。

### 3.3 死锁与等待超时

- **死锁检测**：两事务互相等对方持锁即死锁，由 `deadlock_timeout`（默认 1s）自动检测，牺牲一方报 `deadlock detected`。应用侧应对死锁做**重试**。
- **锁等待超时 `lock_timeout`**：单条语句等锁最长时间，超时报错、快速失败。防「一个长事务把后面一排会话拖成排队雪崩」。

## 4. 排查工具箱（可直接粘贴的 SQL）

**4.1 找长事务 / 卡死会话（最常用）**

```sql
SELECT pid, usename, application_name, client_addr, state,
       now() - xact_start  AS xact_age,     -- 事务开了多久
       now() - query_start AS query_age,    -- 当前语句跑了多久
       wait_event_type, wait_event,          -- 空+active = 正在耗 CPU
       left(query, 80) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
  AND now() - xact_start > interval '5 minutes'
ORDER BY xact_age DESC;
```

**4.2 看某个会话持有哪些锁**

```sql
SELECT l.locktype, c.relname AS object, l.mode, l.granted
FROM pg_locks l
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE l.pid = 21634          -- 换成目标 pid
ORDER BY l.mode DESC;
```

**4.3 阻塞链：谁挡住了谁（空 = 当前没人被挡）**

```sql
SELECT w.pid AS waiting_pid, w.usename AS waiting_user,
       now() - w.query_start AS waiting_for, left(w.query,80) AS waiting_query,
       b.pid AS blocking_pid, b.usename AS blocking_user,
       b.state AS blocking_state, now() - b.xact_start AS blocking_xact_age,
       b.application_name AS blocking_app, left(b.query,80) AS blocking_query
FROM pg_stat_activity w
CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS x(blocking_pid)
JOIN pg_stat_activity b ON b.pid = x.blocking_pid
WHERE cardinality(pg_blocking_pids(w.pid)) > 0
ORDER BY waiting_for DESC;
```

**4.4 谁在排队等锁、等什么对象（`granted=false` = 想拿没拿到）**

```sql
SELECT l.pid, l.locktype, c.relname AS object, l.mode AS wanted_lock, l.granted,
       now() - a.query_start AS waiting_for, left(a.query,60) AS query
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE NOT l.granted
ORDER BY waiting_for DESC;
```

**4.5 量化长事务把 autovacuum 拖住了多远（判断紧迫度）**

```sql
-- 每个活跃事务把回收点往回拖了多少个事务号，越大越危险
SELECT pid, usename, application_name,
       now() - xact_start AS xact_age,
       age(backend_xmin)  AS holds_back_xacts
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY holds_back_xacts DESC NULLS LAST
LIMIT 10;
```

**4.6 膨胀与回卷余量**

```sql
-- 哪些表死亡元组堆最多（膨胀直观证据）
SELECT relname, n_live_tup, n_dead_tup,
       round(n_dead_tup*100.0/nullif(n_live_tup+n_dead_tup,0),1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 10;

-- 各库离强制回卷保护线还有多远
SELECT datname, age(datfrozenxid) AS xid_age,
       current_setting('autovacuum_freeze_max_age')::bigint AS freeze_limit
FROM pg_database ORDER BY xid_age DESC;
```

**4.7 查超时参数当前值与来源**

```sql
SELECT name, setting, unit, source, boot_val, reset_val
FROM pg_settings
WHERE name IN ('statement_timeout','lock_timeout',
               'idle_in_transaction_session_timeout','transaction_timeout')
ORDER BY name;

-- 有没有人配过角色/库级默认值（空/无相关项 = 从没设过，全靠默认 0）
SELECT coalesce(d.datname,'(所有库)') AS database,
       coalesce(r.rolname,'(所有角色)') AS role, s.setconfig
FROM pg_db_role_setting s
LEFT JOIN pg_database d ON d.oid = s.setdatabase
LEFT JOIN pg_roles r   ON r.oid = s.setrole
ORDER BY database, role;
```

> 读法：`setting=0` 表示不限时；`source` 为 `default`＝从没设过，`session`＝你刚 `SET` 的（仅本窗口），`database`/`user`＝库级/角色级。

**4.8 CPU 占用的说明**：`pg_stat_activity` **不暴露按 pid 的 CPU%**。要看实时 CPU 只能上服务器 `top -p <pid>`，或看云数据库控制台监控曲线。SQL 侧只能用 `state=active` + `wait_event` 空 定性判断「它正在耗 CPU」。

---

## 5. 处置手册（从温和到强制，含生产红线）

**5.1 两个终止函数**

```sql
SELECT pg_cancel_backend(pid);     -- 发 SIGINT：只中断当前语句，连接/事务保留
SELECT pg_terminate_backend(pid);  -- 发 SIGTERM：断连接、回滚事务、释放锁
```

- 杀自己的会话不需要特权；杀别人的需要**超级用户**、或与其**同角色/成员**、或被授予 **`pg_signal_backend`**。
- 返回 `f` = 没权限（静默失败）；返回 `t` 但进程还在 = 后端 **wedged，不响应信号**。

**5.2 客户端侧关闭（本次案例首选）**

卡死连接背后是 KStudio 一个没关的窗口时，**从客户端断最干净**：点窗口「停止/取消」→ 关标签页 → 右键连接「断开」→ 退出 KStudio。客户端 socket 一断，后端通常随之退出、锁释放。

**5.3 OS 层强杀（SQL 杀不动时）**

对 `t` 仍不死的僵死 backend，由能进服务器的人执行 `kill -9 <pid>`（`pg_stat_activity` 里的 pid 就是后端 OS 进程号）。**这只杀单个后端进程，postmaster 自动回收该连接，不影响实例和其它会话——生产安全。**

**5.4 生产红线**

- **绝不为杀一个会话去重启整个生产实例**（会打断真实业务）。重启只用于「dev/可丢数据/无业务」场景。
- `kill` 前用 4.3 确认它**没有正被别的会话依赖**、并核对 `application_name`/`client_addr` 找对来源。
- 强杀会**回滚该事务**；若被杀的是**大写入事务**，回滚本身可能耗时且短暂加重负担（只读 SELECT 无此顾虑）。

**5.5 权限受限时（本次即如此）**

个人账号改不了库/角色、SQL 也杀不动时：① **用新表名继续开发**，别被旧表的锁卡住；② 把「会话信息 + 完整 SQL + 事务挂了多久 + 持有的锁 + 是否连带阻塞 + 4.5 的 `holds_back_xacts`」整理成证据，交给能进服务器/云控制台的人做**精准 kill**；③ 或等它随客户端/VPN/机器断开自愈，再定期用 4.1 复查。

## 6. 预防：三个超时参数的推荐配置

**作用速记**：

- `statement_timeout`：单条 SQL 最长执行时间，超时报错中断。治「跑飞/慢查询」。
- `lock_timeout`：单条语句等锁最长时间，超时快速失败。治「锁排队雪崩」。
- `idle_in_transaction_session_timeout`：事务开着却空闲的最长时间，超时断连。治「BEGIN 不 COMMIT」。

**分档推荐值**：

| 参数 | 业务/应用账号 | 运维/开发账号（KStudio 等） | 迁移/DDL 会话 |
|---|---|---|---|
| `statement_timeout` | `30s`～`60s` | `5min`～`10min` | `0`（临时不限） |
| `lock_timeout` | `5s` | `30s` | `5s`（配合重试） |
| `idle_in_transaction_session_timeout` | `60s` | `5min` | `5min` |

**取值权衡**：

- OLTP 单条查询超几十秒基本是异常（没走索引或跑飞），60s 够用；但**报表/ETL/大导出**要放宽，**迁移/建索引/VACUUM/备份**要临时设 `0`，否则误杀。别一刀切设太小。
- `lock_timeout` 核心是**快速失败 + 应用重试**，设太大（几分钟）就失去防雪崩意义。
- `idle_in_transaction` 正常应用几秒内该提交，60s 已很宽松；超过多半是漏 commit 或连接池异常，直接断最干净。

**落地方式**（个人账号权限不够，需运维/DBA 执行角色/库级）：

```sql
-- 角色级（推荐，按用途分账号）
ALTER ROLE app_user  SET statement_timeout = '60s';
ALTER ROLE app_user  SET lock_timeout = '5s';
ALTER ROLE app_user  SET idle_in_transaction_session_timeout = '60s';

ALTER ROLE dgcl_ylf  SET statement_timeout = '10min';
ALTER ROLE dgcl_ylf  SET lock_timeout = '30s';
ALTER ROLE dgcl_ylf  SET idle_in_transaction_session_timeout = '5min';

-- 库级（该库所有连接默认）
ALTER DATABASE zhcl  SET idle_in_transaction_session_timeout = '5min';
-- 实例级（超级用户，需 reload 或重启）
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
```

- **云数据库**：直接在控制台「参数设置」里改这些 GUC 最省事，不用 SQL、不受 owner 限制。
- **会话级临时**（无权限也能用，仅当前连接/当前窗口有效）：`SET statement_timeout='10min';`。

### 参数详解（逐个说明含义）

> 单位：这几个超时参数裸数字按**毫秒**解释，也可直接写 `'5min'` 这类带单位串。`0` 一律表示**不限**。

**A. 超时防护类（动态，可 `SET` / `ALTER ROLE` / `ALTER DATABASE` / `ALTER SYSTEM`）**

| 参数 | 含义 | 默认 | 建议 | 作用 / 注意 |
|---|---|---|---|---|
| `statement_timeout` | 单条语句（含 SELECT/DML/DDL）最长执行时间，超时中断并报 `canceling statement due to statement timeout` | 0（不限） | 业务 30~60s / 运维 5~10min / 迁移 0 | 治**跑飞/慢查询**（本次 active 卡死就归它管）。会砍合法长报表/迁移/备份，需临时设 `0` |
| `lock_timeout` | 单条语句**等待锁**（表锁/行锁/advisory 等）的最长时间，超时放弃并报 `lock timeout` | 0（不限） | 业务 5s / 运维 30s / DDL 5s+重试 | 防「一个长事务把后面拖成排队雪崩」；DDL 用短值快速失败错峰重试 |
| `idle_in_transaction_session_timeout` | 事务已开但会话空闲（`idle in transaction`）的停留上限，超时**断连并回滚** | 0（不限） | 业务 60s / 运维 5min | 治「BEGIN 不提交」。**对 `active`（正在执行）查询无效**，那靠 `statement_timeout` |
| `transaction_timeout` | 整个事务（含 active+idle）**总时长**上限，超时中止事务（PG14+，KingbaseES 视版本） | 0（不限） | 视情况 10~30min | 长事务的**最后兜底**；查不到即本版本不支持 |
| `deadlock_timeout` | 等锁超过该时长才触发**死锁检测**（避免频繁检测开销） | 1s | 保持 1s | 一般不动；设 0 则每次等锁立即检测（开销大） |

> 关系：一条语句总耗时 = 执行 + 等锁。`statement_timeout` 管**总时长**，`lock_timeout` 只管**等锁那一段**，二者独立、可同时设。

**B. autovacuum 与回卷防护（`postgresql.conf`/`ALTER SYSTEM`，部分需重启）**

| 参数 | 含义 | 默认 | 建议 | 作用 / 注意 |
|---|---|---|---|---|
| `autovacuum` | 是否启用自动清理（回收死亡元组、冻结防回卷） | on | on | 关了就得手动 `VACUUM`，别关 |
| `autovacuum_max_workers` | 并发 autovacuum 工作进程数 | 3 | 按库数量/负载 | 改需重启；库多、膨胀快可调大 |
| `autovacuum_naptime` | autovacuum 唤醒检查间隔 | 1min | 保持 | 越小越及时但越耗资源 |
| `autovacuum_vacuum_scale_factor` | 死亡元组占活元组比例达阈值即触发 vacuum | 0.2（20%） | 热表调 0.05~0.02 | 大表 20% 太多，可按表 `ALTER TABLE ... SET` 调小让它更勤 |
| `autovacuum_vacuum_cost_limit` / `_cost_delay` | autovacuum 的 I/O 限速预算/延迟 | 200 / 2ms | 高配可调宽 | 限速防 vacuum 抢业务 I/O；调大加速回收 |
| `autovacuum_freeze_max_age` | 表年龄（xid）超此值**强制** vacuum 防事务号回卷 | 20 亿 | 保持/按量评估 | 逼近会强制 vacuum、极端触发回卷保护（库变只读） |
| `log_autovacuum_min_duration` | autovacuum 动作耗时超阈值则记日志 | -1（不记） | 0（全记）便于观察 | 排查膨胀/回收是否跟得上的关键 |
| `maintenance_work_mem` | vacuum/建索引等维护操作可用内存 | 64MB | 256MB~1GB | 调大可显著加速 vacuum 与 `CREATE INDEX` |
| `max_connections` | 最大并发连接数 | 100 | 按规格 | 改需重启；配合连接池，防连接打满 |

> 说明：`ALTER ROLE`/`ALTER DATABASE` 设的超时对**之后新建连接**生效；`ALTER SYSTEM` 写 `postgresql.auto.conf`，部分参数需 `SELECT pg_reload_conf();` 或重启。云库走控制台「参数设置」最稳。

---

## 7. 实际开发常用设置与最佳实践

**应用侧（Java 后端尤其注意）**

- **事务尽量短**：`@Transactional` 方法里**不要夹 RPC / HTTP / 文件 IO / 慢计算**——这些会把事务撑长，直接放大膨胀与锁持有时间。把外部调用挪到事务外。
- **及时提交**：确保每条链路 commit/rollback 闭环，异常路径别把连接留在 `idle in transaction`。
- **连接池配置**：HikariCP 设 `maxLifetime` < DB 侧超时；配 `connectionTimeout`；避免池里连接被服务端单方面 kill 后仍被复用。用 **PgBouncer 等中间件时注意**：事务级池化（transaction pooling）与「长事务 / session 级 `SET` / advisory lock / 游标」不兼容，需按 session 模式或排除这类连接。
- **批量写分批提交**：百万级 `INSERT/UPDATE` 拆成小批次、每批一提交，别在一个事务里跑到底。
- **`SELECT ... FOR UPDATE` 谨慎**：锁行且持到事务结束，务必配短事务 + 索引命中，避免锁范围扩大或升级为表扫描。

**DDL / 变更**

- 改结构前 `SET lock_timeout='5s';`，拿不到锁快速失败、错峰重试，别被长事务无限顶住（正是本次教训）。
- 建索引优先 `CREATE INDEX CONCURRENTLY`（不长时间锁写），但注意它不能在事务块内跑、失败会留 INVALID 索引需重建。
- 大表 `VACUUM FULL`/`CLUSTER` 会拿 `AccessExclusiveLock` 锁全表，生产慎用，优先普通 autovacuum + 调参。

**查询与性能**

- 上线前 `EXPLAIN (ANALYZE, BUFFERS)` 看执行计划，防全表扫/笛卡尔积跑飞。
- 报表/大分析走**只读副本**，别在生产主库跑重查询。
- 复杂查询显式带 `statement_timeout`，宁可自己设也不要挂到天级。

**autovacuum 调优要点**

- 热表膨胀快时，调低该表的 `autovacuum_vacuum_scale_factor`（如 0.05→0.02）让它更勤快。
- 监控 `last_autovacuum` 是否长期落后、`n_dead_tup` 是否持续堆积——往往是**有长事务拖住**，先清长事务再谈调参。

**死锁与监控**

- 应用对 `deadlock detected`（SQLSTATE 40P01）和 `lock_timeout`（55P03）做**有限次退避重试**。
- 建**定时巡检**：每 5～10 分钟跑一次 4.1，发现 `xact_age` 超阈值（如 >10min）的会话即告警到运维群，把「挂两天」扼杀在「挂十分钟」。

---

## 8. 注意事项 / 常见坑

- **`statement_timeout` 会误杀**合法长任务：数据迁移、备份、大报表、`CREATE INDEX` 前记得临时设 `0` 或调大。
- **`idle_in_transaction_session_timeout` 对 `active` 查询无效**——本次跑飞是 active，得靠 `statement_timeout` 才拦得住。两者别混为一谈。
- **KStudio 每个 SQL 编辑器窗口 = 独立连接**：会话级 `SET` 只在同窗口有效，跨窗口查 `SHOW` 会回到默认值（本次就出现过「设了却查回 0」）。
- **`client_addr` 是 VPN 出口 IP，不是本机局域网 IP**：经 aTrust 连库时，库里看到的来源是隧道/网关地址（如 `19.104.4.128`），别拿它去 `ipconfig` 里比对。
- **`pg_stat_activity` 不暴露按 pid 的 CPU%**：精确 CPU 只能 OS `top` 或云控制台监控。
- **长事务 × 复制槽 = WAL 堆积**：有逻辑复制/物理复制槽时，长事务会阻止 WAL 回收，可能撑爆磁盘，比单纯膨胀更危险。
- **生产 `terminate`/`kill` 前先确认**：核对来源、确认无业务依赖、评估被杀事务是否为需回滚的大写事务。
- **权限受限的运维账号**（如 `dgcl_ylf`）：改库报 `must be owner`、`ALTER ROLE` 无权限、杀不掉僵死会话都属正常——这类根治动作本就该由 DBA/运维或云平台管理账号做。
- **`transaction_timeout`**（PG 17+）可限制整个事务（含 active+idle）总时长，作为长事务的最后兜底；KingbaseES 视版本而定，查不到即不支持。

---

## 9. 本次案例待办 Checklist

- [ ] 让运维/DBA 对 `pid=21634` **精准 `kill -9`**（或超级用户 `pg_terminate_backend`），**不重启实例**。
- [ ] 附证据推动：会话详情 + 完整 SQL + `xact_age≈2 天` + `holds_back_xacts`（见 4.5）+ 是否连带阻塞（4.3）。
- [ ] 由 DBA 给**角色级**设 `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout`（按第 6 节分档）。
- [ ] 部署/校验完成后，**养成关闭 KStudio 查询窗口的习惯**，别在生产留不关的会话。
- [ ] 建立**长事务定时巡检 + 告警**（4.1，阈值如 xact_age > 10min）。
- [ ] 被锁的三张未上线旧表：先**用新表名继续开发**；待 21634 清掉、锁释放后再统一处理旧表与依赖视图。
- [ ] 清掉后观察 `n_dead_tup` 是否回落、`last_autovacuum` 是否追平，必要时对膨胀表做一次 `VACUUM (ANALYZE)`。

---

> 维护说明：本手册随 KingbaseES/PostgreSQL 版本略有差异（如 `transaction_timeout` 需 PG17+）。参数取值请按实际业务量级压测后微调，先分档、后迭代。

---

## 10. 附录：MySQL（InnoDB）对照速查

> 结论：**思路通用**（短事务、及时提交、超时兜底、精准杀会话、别为杀会话重启整库），但**锁的名字、排查视图、命令、超时参数**都要换成 MySQL 这套，不能照搬 PG 语法。

### 10.1 概念对照总表

| 维度 | KingbaseES / PostgreSQL | MySQL (InnoDB) |
|---|---|---|
| 默认隔离级别 | Read Committed | **Repeatable Read**（间隙锁防幻读） |
| 普通 `SELECT` 加不加锁 | 加 `AccessShareLock`（会挡 DDL） | 不加行/表锁（MVCC 快照读），但持 **MDL 元数据锁** |
| 锁体系 | 8 级表锁 + 行锁(xmax) + 咨询锁 | 意向锁(IS/IX) + 行锁(记录/间隙/next-key) + **MDL** |
| 长事务危害 | 拖住 xmin → autovacuum 清不动 → 表膨胀、xid 回卷、复制槽 WAL 堆积 | 拖住 purge → undo 膨胀、History List Length 升高、主从延迟 |
| 谁在等锁 | `pg_locks.granted=false` | `performance_schema.data_lock_waits` / `sys.schema_table_lock_waits` |
| 看会话 | `pg_stat_activity` | `SHOW FULL PROCESSLIST` / `information_schema.processlist` |
| 看长事务 | `xact_start` | `information_schema.innodb_trx.trx_started` |
| 杀会话 | `pg_terminate_backend(pid)` | `KILL <id>` / `KILL QUERY <id>` |
| 单条超时 | `statement_timeout`（含 DML/DDL） | `max_execution_time`（**仅 SELECT**，单位 ms） |
| 锁等待超时 | `lock_timeout` | `innodb_lock_wait_timeout`(行锁,默认50s) + `lock_wait_timeout`(**MDL,默认1年**) |
| 空闲事务超时 | `idle_in_transaction_session_timeout` | `idle_transaction_timeout`(8.0.20+,默认0关) / `wait_timeout` |
| 后台回收 | autovacuum 自动 | InnoDB purge；收缩空间要 `OPTIMIZE TABLE`/重建 |

### 10.2 「改不了表」在 MySQL 里多半是 MDL

一条长 `SELECT` 或**未提交的事务**会对表持 **MDL 共享读锁**；`ALTER/DROP` 要拿 **MDL 独占锁** → 被挡住，`SHOW PROCESSLIST` 里显示 **`Waiting for table metadata lock`**。这与 PG 里「SELECT 的 AccessShareLock 挡住 DDL」是**同一剧情**，只是换了名字。更危险的是 **MDL 排队优先**：一旦有 DDL 在等独占锁，后续所有新查询被连带堵死，瞬间雪崩。

### 10.3 排查工具箱（MySQL 语法）

```sql
-- ① 找长事务（按开始时间，最老的在最上）
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_s,
       trx_mysql_thread_id AS processlist_id, trx_rows_modified, trx_query
FROM information_schema.innodb_trx
ORDER BY trx_started;

-- ② 进程列表：找卡住的 DDL / 长时间 Sleep 但事务未提交
SELECT id, user, host, db, command, time, state, LEFT(info,80) AS query
FROM information_schema.processlist
WHERE command <> 'Sleep' OR time > 60
ORDER BY time DESC;

-- ③ 行锁等待链：谁挡谁（8.0.15+；旧版用 information_schema.innodb_lock_waits）
SELECT r.trx_mysql_thread_id AS waiting_pid, LEFT(r.trx_query,60) AS waiting_query,
       b.trx_mysql_thread_id AS blocking_pid, LEFT(b.trx_query,60) AS blocking_query
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_engine_transaction_id
JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_engine_transaction_id;

-- ④ 元数据锁(MDL)谁在等（需先启用 mdl instrument）
UPDATE performance_schema.setup_instruments SET ENABLED='YES' WHERE NAME='wait/lock/metadata/sql/mdl';
SELECT object_schema, object_name, lock_type, lock_status, owner_thread_id
FROM performance_schema.metadata_locks
WHERE object_type='TABLE' AND lock_status IN ('PENDING','GRANTED');

-- ⑤ 直接看被 MDL 卡住的语句
SELECT id, user, time, state, LEFT(info,80) AS query
FROM information_schema.processlist
WHERE state = 'Waiting for table metadata lock';
```

### 10.4 处置与超时参数（MySQL）

```sql
KILL 12345;        -- 断开连接并回滚其事务（相当于 pg_terminate_backend）
KILL QUERY 12345;  -- 只中断当前语句（相当于 pg_cancel_backend）

-- 超时兜底（会话级 SET SESSION；长期用 SET GLOBAL 或 my.cnf）
SET SESSION max_execution_time = 30000;        -- SELECT 超 30s 中断（不覆盖 DML！）
SET SESSION innodb_lock_wait_timeout = 10;     -- 行锁等 10s 快速失败
SET SESSION lock_wait_timeout = 10;            -- MDL 等 10s（默认一年，务必调小）
SET GLOBAL idle_transaction_timeout = 60;      -- 空闲事务 60s 断开（8.0.20+）
```

**推荐值（对照第 6 节）**：

| 参数 | 业务账号 | 运维/开发 | 迁移/DDL |
|---|---|---|---|
| `max_execution_time` | `30000`(30s) | `600000`(10min) | `0` |
| `innodb_lock_wait_timeout` | `10s` | `30s` | `10s` |
| `lock_wait_timeout`(MDL) | `10s` | `30s` | `5s`+重试 |
| `idle_transaction_timeout` | `60s` | `300s` | `300s` |

**生产红线同 PG**：只为杀单个会话执行 `KILL`，**不要重启整库**；`KILL` 会回滚该事务，杀大写入事务前评估回滚代价。

### 10.5 MySQL 特有的坑

- **`max_execution_time` 管不住跑飞的 `UPDATE/DELETE`**（只对 SELECT 生效）——误发的大 DML 没有语句级超时兜底，只能靠 `KILL`；大更新务必带 `WHERE` 主键范围 + 分批。
- **`lock_wait_timeout` 默认一年**，就是「ALTER 无限期卡住」的元凶，生产必须显式调小。
- **间隙锁 / next-key 锁**（RR 隔离级别特有）是 MySQL 死锁高发源，PG 基本没有；应用侧对 `deadlock detected`（1213）做退避重试。
- **purge lag / History List Length 持续升高** = 有长事务拖住 undo 回收，先 `innodb_trx` 揪出长事务 `KILL`，再谈调参。
- 主从架构下长事务/大 DDL 会**放大复制延迟**，DDL 优先用 `pt-online-schema-change` / `gh-ost` 在线改表。
