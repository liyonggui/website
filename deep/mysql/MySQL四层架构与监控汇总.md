# MySQL 四层架构、执行顺序、监控与 BufferPool 汇总

> 系统汇总 MySQL 核心原理：四层架构与各层监控、连接层参数辨析、SQL 执行顺序（正向/反向）、两阶段提交、BufferPool 与脏页、BufferPool 与无索引字段、长连接与僵尸半开连接治理。
> 以 InnoDB + MySQL 8.0 + Java/HikariCP 为主。

---

## 目录

- [一、四层架构与各层监控](#一四层架构与各层监控)
- [二、连接层参数辨析（连接/会话/线程/事务）](#二连接层参数辨析连接会话线程事务)
- [三、SQL 执行顺序（正向执行与反向回传）](#三sql-执行顺序正向执行与反向回传)
- [四、两阶段提交（2-PC）](#四两阶段提交2-pc)
- [五、BufferPool 与脏页完整梳理](#五bufferpool-与脏页完整梳理)
- [六、BufferPool 与无索引字段](#六bufferpool-与无索引字段)
- [七、长连接与僵尸半开连接治理](#七长连接与僵尸半开连接治理)

---

## 一、四层架构与各层监控

```
客户端
 ↓
【1 连接层】连接、线程、认证
 ↓
【2 SQL服务层】解析、优化器、执行器、binlog
 ↓
【3 存储引擎层 InnoDB】BufferPool、锁、事务、redo/undo、ChangeBuffer
 ↓
【4 文件系统层】磁盘IO、文件、页刷盘、DoubleWrite
```

> 四层**全部可以监控**，不同层监控对象、工具不一样。
> 
> 注意：MySQL 不同发行版和小版本在某些功能（如线程池、performance_schema 事件名、部分状态变量）上存在差异，文中术语以常见 8.x 行为为准，实际排查请先用 `show variables` / `show status` / `performance_schema` 查询确认。

### 1.1 连接层监控

负责连接、线程池、登录认证、权限。

**监控指标：**

1. **连接总数**：`Threads_connected` 当前活跃连接。
2. **最大连接数**：`max_connections`；`Threads_created` 创建过的线程数。
3. **失败登录**：`Aborted_connects` 连接失败；`Aborted_clients` 客户端异常断开。
4. **线程缓存**：`Thread_cache_hits`。

**查询命令：**

```sql
show status like 'Threads%';
show status like 'Aborted%';
select * from information_schema.processlist; -- 当前会话
```

**风险点**：连接打满、大量无效短连接、连接泄漏。

**连接层状态变量速查表：**

| 状态变量              | 含义                  | 空闲 sleep 会话是否计入          | 事务无关说明                                                                             |
| ----------------- | ------------------- | ------------------------ | ---------------------------------------------------------------------------------- |
| Threads_connected | 当前已建立成功会话数 (TCP 连上) | ✅ 算                      | 包含空闲无事务连接，不是运行中事务数量；瞬时值，不是计数器；包含僵尸半开连接。**不等于活跃执行 SQL 的连接（Threads_running 才是正在干活）** |
| Threads_created   | 累计创建 OS 工作线程数，只增不减  | 无，计数器                    | 短连接频繁重连会快速上涨；从缓存复用线程不计数                                                            |
| Aborted_connects  | 登录/建立连接阶段失败         | ❌ 连接都没建立                 | 密码错误、max_connections 打满拒绝连接，属于这个                                                   |
| Aborted_clients   | 登录成功后会话异常断开         | ✅ sleep 超时被 mysql 踢掉会 +1 | 客户端崩溃、超时、网络断都算                                                                     |
| Thread_cache_hits | 线程缓存命中次数，复用已有 OS 线程 | 无，计数器                    | 缓存操作系统线程，减少创建销毁开销；长连接场景该值增长慢                                                       |

### 1.2 SQL 服务层监控（服务层）

解析、语法树、查询优化、执行器、join、sort、临时表、binlog。

**监控指标：**

- **语句统计**：`Com_select / Com_insert / Com_update / Com_delete`。
- **慢查询**：开启 `slow_query_log`，设 `long_query_time` 阈值。
- **文件排序（filesort）**：常看 `Sort_scan`、`Sort_range`、`Sort_merge_passes`。大量 `Sort_merge_passes` 或 `Created_tmp_disk_tables` 升高，结合慢查询日志，可判定大量排序/临时表落盘。`performance_schema` 的 `events_statements_summary_by_digest` 与 statement 层面的 `EXPLAIN/ANALYZE` 可定位具体语句。
- **临时表**：`Created_tmp_tables`、`Created_tmp_disk_tables`（需结合 ratio 与增长趋势看，单一绝对值意义有限）。
- **binlog**：`Binlog_cache_disk_use` 表示 binlog cache 内容溢出到临时文件写盘的次数（即事务 binlog 缓存不足导致落盘）。
- **查询缓存**：8.0 已移除。

**查询命令：**

```sql
show status like 'Com_%';
show status like 'Sort%';
show status like 'Created_tmp%';
show status like 'Binlog_cache%';
```

> performance_schema 可以抓单条 SQL 的执行耗时、扫描行数。

**风险**：烂 SQL、大量 filesort、磁盘临时表、大事务 binlog 缓存溢出。

### 1.3 InnoDB 存储引擎层监控（最重要）

BufferPool、锁、事务、MVCC、redo、undo、Change Buffer、自适应哈希索引。

核心状态：`show engine innodb status;`

- **Buffer Pool**：`Innodb_buffer_pool_reads`（真实磁盘读，越高说明缓存命中率差）、`Innodb_buffer_pool_read_requests`（内存逻辑读）、`Innodb_buffer_pool_pages_dirty`（脏页数量）。
  
  > 缓存命中率（近似）≈ `1 - delta(Innodb_buffer_pool_reads) / delta(Innodb_buffer_pool_read_requests)`（请用**区间增量**计算，以反映窗口内真实命中率；都是累计计数，直接用绝对值不准）。InnoDB status 变量名在不同版本可能有大小写/下划线差别，建议统一用 `SHOW STATUS` 原名并以 delta 形式监控。

- **Change Buffer / insert buffer 相关**：`Innodb_ibuf_merges`、`Innodb_ibuf_free_list` 等（`Innodb_ibuf_size` 并非所有版本都有，具体名称以 `show status` 为准）。

- **事务与回滚 undo**：`Innodb_trx_active` 活跃事务；`Innodb_undo_logs`；历史版本 purge 情况。

- **Redo log**：`Innodb_log_waits`（redo log buffer 不足产生等待，>0 代表瓶颈）。

- **行操作**：`Innodb_rows_read/inserted/updated/deleted`。

**锁监控：**

```sql
-- 锁等待
select * from information_schema.innodb_trx;        -- 当前事务
select * from information_schema.innodb_locks;      -- 当前锁
select * from information_schema.innodb_lock_waits; -- 锁等待（阻塞）
```

> 锁等待定位常用上述 information_schema 视图；`performance_schema` 的 `events_waits_current` / `events_waits_history_long` 可用于查看具体等待事件和堆栈（注意 performance_schema 需开启相关 consumers）。

> mysqld_exporter 会采集全部 Innodb_* 状态。

**风险**：缓存命中率低、大量锁等待、长事务、redo log 等待、脏页积压。

### 1.4 文件系统层监控

MySQL 已交给操作系统，监控磁盘 IO、文件读写、刷盘、double-write。

> MySQL 本身看不到底层磁盘指标，需要**系统层面监控**。

**监控项：**

1. 磁盘 IOPS、读写吞吐量：`iostat / sar`。
2. 磁盘延迟：await、svctm（IO 慢直接拖垮 MySQL）。
3. Double Write：`Innodb_dblwr_writes` 双写写入次数。DoubleWrite 次数高不一定是错误——它是正常保护机制；但异常增加可能暗示大量脏页刷写压力或磁盘延迟。
4. 页刷盘：`Innodb_pages_written`。
5. 磁盘空间：ibdata1、*.ibd、redo log、binlog 磁盘占用。

> 补充：DoubleWrite 次数增多说明脏页刷写频繁；若伴随 `iostat` await 增高，应关注磁盘延迟或并发刷盘压力。

**风险**：磁盘打满、IO 瓶颈、磁盘延迟高。

### 1.5 整套监控实现方案

**方案 A：原生 MySQL 自带**

1. `show status`：全局状态。
2. `show engine innodb status`：InnoDB 快照。
3. `information_schema`：会话、锁、事务。
4. `performance_schema`：细粒度语句、等待事件（四层各类等待）。
5. `slow-log`：慢 SQL。

缺点：不做时序存储，只能看实时，不能看历史趋势。

**方案 B：生产标准 Prometheus + mysqld_exporter + Grafana**

- **连接层**：Threads_*、Aborted_*。
- **SQL层**：Com_*、Sort、Tmp表、binlog 指标。
- **InnoDB层**：BufferPool 命中率、锁、事务、redo、change buffer。
- **文件层**：exporter 拿不到磁盘 IO，需要 node_exporter 补充系统磁盘 IO 指标。

### 1.5.1 监控实践原则（delta/窗口）

对 `SHOW STATUS` 返回的**累计计数类指标**用 delta（时间窗口）计算速率/比率；对**当前量**（如 Threads_connected、Innodb_buffer_pool_pages_dirty）直接用当前值。

### 1.5.2 常用告警建议

- `Threads_connected` > 0.8 × max_connections 或接近 max_connections → 报警。
- `delta(Threads_created)` 在短窗口内持续高（例如每分钟 > N）→ 报警（短连接压力或线程缓存配置问题）。
- `Aborted_connects` 突增 → 报警（认证失败或 max_connections 限制）。
- `Created_tmp_disk_tables / Created_tmp_tables` 的磁盘临时表比率持续 > ~5%（根据场景）→ 排查慢 SQL。
- `delta(Innodb_buffer_pool_reads) / delta(Innodb_buffer_pool_read_requests)` > 0.01（示例阈值，需结合业务）→ 缓存命中率下降。
- `Innodb_log_waits` > 0 或快速上升 → redo log 写入成为瓶颈（检查 innodb_log_file_size、innodb_flush_log_at_trx_commit、磁盘延迟）。
- `iostat` await（平均等待）持续 > 10ms 且所有 SQL 都变慢 → 磁盘 IO 瓶颈。

### 1.6 四层常见等待事件（快速定位哪层瓶颈）

1. **连接层等待**：`wait/io/socket/sql/client_connection` 连接 socket 等待。
2. **SQL服务层等待**：`wait/synch/mutex/sql/*`，排序、临时表相关等待。
3. **InnoDB层等待**：
   - `wait/io/file/innodb` 文件 IO 等待；
   - `wait/synch/mutex/innodb/buffer_pool` BufferPool 锁；
   - `wait/synch/mutex/innodb/log_flush` redo 刷盘等待；
   - `wait/lock/table` 表锁、行锁等待。
4. **文件系统层等待**：全部落在 `wait/io/file/*` 文件 IO 等待。

> 注意：event 名称可能随 MySQL 版本/打补丁不同（例如子类路径会变化），请用 `performance_schema.events_waits_current` 或 `sys.schema` 确认实际可用事件。示例查询确定事件名：
> 
> ```sql
> SELECT EVENT_NAME, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name ORDER BY SUM_TIMER_WAIT DESC LIMIT 50;
> ```

### 1.7 快速判断瓶颈口诀

1. 连接数飙升、大量 Abort → **连接层瓶颈**。
2. Tmp_disk、filesort 大量上涨、慢 SQL 多 → **SQL服务层（SQL写得烂）**。
3. BufferPool 命中率低、锁等待、log_waits>0、长事务多 → **InnoDB 存储引擎层瓶颈**。
4. 所有 SQL 都慢，performance_schema 大量 `wait/io/file`，iostat 磁盘 await 高 → **文件系统磁盘 IO 瓶颈**。

### 1.8 补充小问题

> Q：四层中哪一层监控最容易被忽略？
> A：文件系统层。很多人只看 MySQL 内部指标，忽略磁盘 IO；磁盘慢会导致上层所有层全部阻塞，但 MySQL 内部指标只会体现各种 IO 等待事件。

---

## 二、连接层参数辨析（连接/会话/线程/事务）

> 重要前提：MySQL 一个 TCP 连接 ≈ 一个会话(session)；一个会话绑定一个服务线程（1:1 模型）。默认模型：Community Edition（大多数 8.0 发行）采用一连接一线程模型；线程池为可选插件/发行版特性（MySQL Enterprise 提供 thread pool 插件，部分第三方发行版亦有实现）。在使用线程池的实例上，监控/指标语义会不同。

### 2.1 Threads_connected

> **当前已经建立成功的 TCP 连接总数，会话数。** 只要 TCP 连接连上 MySQL 就算，不管有没有执行 SQL、有没有开事务、有没有休眠 sleep。

- 连接上来 sleep 休眠、没执行任何事务：算入 Threads_connected。
- 正在跑事务：算。
- 空闲长连接挂在那里啥也不干：也算。
- 仅尝试连接但连接失败：**不算**。

> ❗误区：不是正在执行事务的连接数。看活跃 SQL/事务用 `show processlist`（Command=Query）或 `information_schema.innodb_trx`。

### 2.2 Threads_created

> **已经创建出来的服务线程总累计数（全局累加，不会归零）。**

**临时连接执行完断开算不算？** 算。开启 `thread_cache_size>0` 时：

1. 客户端短连接 → MySQL 新建线程 → Threads_created +1。
2. SQL 执行完，close 断开。
3. 线程缓存有空位则线程入缓存待命；缓存满则销毁。

> 每一次真正新建 OS 线程 Threads_created 才 +1；从缓存复用不会 +1。

**长连接是固定数值吗？** 不是。长连接复用缓存线程不新增；重启/重连会新建。

> Threads_created 是**累计计数器只增不减**，要看**上涨速率**；上涨快代表大量短连接。

### 2.3 Aborted_connects

> **尝试连接 MySQL，但连接建立阶段就失败**的累计次数（TCP 握手/认证阶段失败，会话没创建成功）。

触发场景：密码错误、不存在的账号、IP 不允许访问、达 max_connections 被拒、网络三次握手中断。

✅ 拒绝连接 → Aborted_connects +1；❌ 连上后业务中主动断开不增加。

> 一句话：还没登录成功就挂了 → Aborted_connects。

### 2.4 Aborted_clients

> **连接已建立登录成功，会话存在，后续异常断开，累计 +1。**

触发场景：客户端崩溃/kill 没 close、**wait_timeout/interactive_timeout 超时被 MySQL 主动踢掉（会算！）**、网络抖动代理断连、执行完直接退出。

> ❗误区：不止客户端主动断开；**服务端因超时踢空闲 sleep 连接也会 Aborted_clients +1**。

**区分两个 Aborted：**

| 指标               | 阶段          | 含义                            |
| ---------------- | ----------- | ----------------------------- |
| Aborted_connects | 连接认证阶段，登录失败 | 还没连上 MySQL，被拒绝/密码错            |
| Aborted_clients  | 连接登录成功之后    | 会话已建立，中途异常断开（客户端崩、服务端超时踢、网络断） |

### 2.5 Thread_cache_hits

> **线程缓存命中次数，累计值。** Thread Cache 缓存的是**服务线程（OS 线程）**，不是连接、不是会话。参数 `thread_cache_size=N` 最多保留 N 个空闲 OS 线程。

完整流程：

1. 新客户端 TCP 连接进来。
2. 缓存池有空闲线程：直接复用 → Thread_cache_hits +1，Threads_created 不增。
3. 缓存池无空闲：新建线程 → Threads_created +1；连接结束后线程放回缓存（未满时）。

> 缓存的是**线程不是 TCP 连接**！8.0 默认 `thread_cache_size=-1` 自动自适应；5.7 默认 8。长连接业务几乎不复用缓存，Thread_cache_hits 增长慢属正常。

**线程缓存命中率公式：**

```
命中率 ≈ delta(Thread_cache_hits) / (delta(Thread_cache_hits) + delta(Threads_created))
```

（两个都是累计计数，直接用绝对值算命中率意义不大；请用**时间窗口内的增量 delta** 计算更可靠。）

> 更可靠的判断方法：观察时间窗口内 `delta(Thread_cache_hits)` 与 `delta(Threads_created)`。一般当 `delta(Threads_created)` 明显 >0 且 `delta(Thread_cache_hits)` 很小时，说明短连接多且线程缓存未命中；或直接看高负载期 `delta(Threads_created)` 是否持续增长。
> 
> 不要把线程缓存等同于连接池：线程缓存缓存的是 **OS 线程**，不是 TCP 连接本身。

### 2.6 生产排查小技巧

1. Threads_connected 很高但 processlist 大量 Command=Sleep → 大量空闲长连接。
2. Threads_created 持续快速上涨、Thread_cache_hits 低 → 大量短连接，调大 thread_cache_size 或程序加连接池。
3. Aborted_connects 飙升 → 排查账号密码、IP 白名单、连接数是否打满。
4. Aborted_clients 持续上涨 → 排查 wait_timeout 过小、应用未正常关闭连接、程序崩溃、代理超时。
5. 看真实运行事务数量：`select count(*) from information_schema.innodb_trx;`（和 Threads_connected 两码事）。
6. **delta 思路**：监控短连接时取两次样本，计算 `delta(Threads_created) / delta(time)` 与连接数增长；或用累计建立的连接数 `Connections` 与 `Threads_created` 的比值辅助判断。

> 修正：Aborted_clients 是"登录成功后会话异常断开"，**包括被服务器因超时踢掉的连接（wait_timeout/interactive_timeout）**。
> 补充：企业版 Thread Pool 线程池逻辑完全不同，以上是默认 one-thread-per-connection 模型。

---

## 三、SQL 执行顺序（正向执行与反向回传）

> 四层顺序是从上往下执行；返回结果从下往上原路返回。

### 3.1 正向执行（客户端发起 SQL）

以 `select * from t where id=1;` 为例：

1. **连接层（最上层）**：TCP 连接、身份认证、权限校验、分配服务线程。
   
   > 只做网络、登录、权限，**不解析 SQL**。

2. **SQL服务层（Server 层）**：
   
   - ①语法解析：词法语法分析，生成 AST 抽象语法树。
   
   - ②语义检查：表、字段是否存在，权限二次校验。
   
   - ③查询优化器：选索引、选 join 顺序，生成执行计划。
   
   - ④执行器：按执行计划调用存储引擎接口 API。
     
     > 执行器不读磁盘数据，只是调度，调用 InnoDB 接口。

3. **InnoDB 存储引擎层**：收到读/写请求。
   
   - ①访问 Buffer Pool：尝试从内存页读取。
     - 命中内存：直接拿数据。
     - 未命中：向文件系统层发 IO 请求读磁盘页，加载进 Buffer Pool。
   - ②读：行锁判断、MVCC、Undo 版本链处理，返回行数据向上传递。
   - ③写：WAL 机制，先写 redo log buffer；变更写 Change Buffer；产生脏页。

4. **文件系统层（最底层）**：InnoDB 与操作系统交互，读 ibd、redo log、binlog、undo 表空间文件；页读取、刷脏页、redo log 刷盘、double-write 写入，调用 OS read/write 系统调用。

**磁盘存储表（mysql 库下，权限）：**

| 权限层级 | 系统表                  | 授权范围                       |
| ---- | -------------------- | -------------------------- |
| 全局权限 | `mysql.user`         | `*.*` 实例全部库表               |
| 库级别  | `mysql.db`           | `db_name.*` 一个库所有表         |
| 表级别  | `mysql.tables_priv`  | `db.table_name` 单张表        |
| 列级别  | `mysql.columns_priv` | `db.table(col1,col2)` 表指定列 |
| 存储过程 | `mysql.procs_priv`   | 存储过程/函数                    |

**两次权限校验对比：**

| 项目     | 连接层权限（Stage1）              | SQL 服务层二次权限（Stage2）                                |
| ------ | -------------------------- | -------------------------------------------------- |
| 时机     | 建立连接阶段，还没执行 SQL            | **每条 SQL 执行前都做校验**                                 |
| 作用     | 能不能登录连上 MySQL              | 登录后能不能执行这条 SQL 操作库/表/列                             |
| 主要来源表  | `mysql.user`（身份+密码）        | `mysql.user(全局)`、`db`、`tables_priv`、`columns_priv` |
| 设置语句   | `CREATE USER / ALTER USER` | `GRANT / REVOKE`                                   |
| 失败现象   | 直接断开连接，无法登录                | 连接保留，拒绝执行当前 SQL                                    |
| 会话生效时机 | 新建连接瞬间加载权限到内存              | 复用连接会话内存中的权限快照                                     |

### 3.2 返回结果：反向回传

文件系统 → InnoDB 存储引擎层 → SQL 服务层执行器组装结果集 → 连接层网络缓冲区 → 返回客户端。

### 3.3 写 SQL 完整流程（update）

`update t set name='xx' where id=1;`

1. 连接层：校验连接、账号权限。
2. SQL 服务层：解析 SQL、优化器生成计划，执行器调用 InnoDB 更新接口。
3. InnoDB 存储引擎层：按索引定位记录；加行锁；生成 undo 版本；修改内存 Buffer Pool 页变成脏页；写 redo log buffer；写 change-buffer。（binlog 是 SQL 服务层产生，在 commit 阶段写入）
4. 文件系统层：redo log 按配置刷盘；脏页后续异步刷 ibd；binlog 刷盘。数据逐层向上返回成功。

### 3.4 重要关键点（面试高频）

1. **binlog 属于【SQL服务层】；redo/undo 属于【存储引擎层】！**

2. 层与层之间是**函数调用**：上层调用下层接口，不直接操作下层文件。

3. **不是每条 SQL 都要穿透四层到磁盘**：Buffer Pool 命中，InnoDB 直接内存返回。
   
   > 热点查询：连接层 → Server层 → InnoDB(BufferPool 内存命中)，直接返回，不访问磁盘。

4. 隔离级别、MVCC、行锁全发生在**存储引擎 InnoDB 层**，Server 层不知道行锁、MVCC。

5. 连接层只管网络，不处理 SQL 逻辑。

**容易混淆的点：**

- 执行计划在 **SQL服务层优化器** 生成；
- B+树索引查找、锁、MVCC、undo、redo 发生在 **存储引擎层**；
- 真正磁盘读写发生在 **文件系统层**。

**故障/瓶颈对应层级：**

1. 连接打满、认证报错 → 连接层。
2. 慢 SQL、filesort、临时表、join 效率差 → SQL 服务层。
3. 锁等待、长事务、BufferPool 命中率低、redo log wait → 存储引擎层。
4. IO 高、磁盘 await 高、文件损坏 → 文件系统层。

---

## 四、两阶段提交（2-PC）

> 区分：**不是分布式事务的 2PC**，是单机 InnoDB 事务提交内部两阶段，用来保证 `redo log(存储引擎层)` 和 `binlog(SQL服务层)` 两份日志内容一致。

参与两层：**SQL 服务层**生成 binlog；**InnoDB 存储引擎层**生成 redo log、undo log。

四层架构视角完整时序（update 事务 commit）：

```
客户端
   ↓
1.连接层（连接、线程，无日志逻辑）
   ↓
2.SQL服务层：解析、优化、执行器调用InnoDB更新接口
   ↓
3.InnoDB存储引擎层：修改BufferPool页(脏页)、生成undo版本、写入redo log buffer
   ↓
4.文件系统层：redo还没持久化，ibd数据页(脏页)还在内存
```

### 4.1 Phase1 Prepare（准备阶段）👉 InnoDB 存储引擎层

1. InnoDB 将事务对应的 `redo log record` 刷到磁盘，**确保 redo 的 LSN 持久化**，使该事务在 redo log 中处于可恢复状态（常说的 PREPARED 阶段/LSN 已写盘）。在 redo log 中把事务标记为 **PREPARED**。
2. 此时 binlog **还未写入磁盘**（取决于 `sync_binlog` 配置）。

> ⚠️此时：undo 已生成、redo 已落盘，**binlog 还没写磁盘，ibd 数据脏页依旧在内存**。

> 这一步结束：redo = PREPARED（可恢复状态）；binlog = 空。

### 4.2 Phase2 Commit（提交阶段）👉 SQL 服务层先写 binlog，再通知 InnoDB 提交

1. **SQL 服务层**：写并刷 binlog（受 `sync_binlog` 控制），写盘成功后通知 InnoDB 正式提交。
2. 写完 binlog 后 Server 层调用 InnoDB 接口通知正式提交。
3. **回到 InnoDB 层**：把 redo 状态标记从 PREPARED → COMMIT（在 redo log 内更新状态或写少量 record）。
4. 返回成功给客户端。

> 脏页（Buffer Pool 修改的数据）**不需要在 commit 时刷入 ibd**，后续后台线程异步刷到文件系统层。

### 4.3 四层完整路径标注

1. **连接层**：接收 commit 命令透传给 Server 层，不参与两阶段逻辑。
2. **SQL服务层（Server）**：负责写 binlog，驱动两阶段提交流程。
3. **InnoDB 存储引擎层**：执行 redo prepare、redo commit 标记。
4. **文件系统层**：真正执行 redo log、binlog 的 write/fsync 系统调用。

```
graph TD
C[客户端 commit] --> A[连接层]
A --> B[SQL服务层]
B --> C1[InnoDB层 Phase1 Prepare<br/>1.redo buffer刷磁盘<br/>2.redo标记 PREPARED]
C1 --> D[文件系统层 刷redo日志到磁盘]
D --> C1 --> B
B --> B1[SQL服务层：写binlog并刷磁盘]
B1 --> D2[文件系统层 刷binlog磁盘]
D2 --> B1 --> B --> C2[InnoDB层 Phase2 Commit<br/>redo标记改为COMMIT]
C2 --> A --> Resp[返回commit成功给客户端]
```

### 4.4 崩溃恢复逻辑（关键面试点）

数据库在 Phase1 完成、Phase2 还没做完时断电崩溃，重启读 redo log：

1. redo 状态为 **COMMIT** → 事务已完整，直接提交。
2. redo 状态为 **PREPARED** → 检查 binlog 磁盘上是否有对应记录：
   - ✅ binlog 在磁盘上：**应用该事务（redo replay）**，保证 redo 与 binlog 对齐。
   - ❌ binlog 不存在/不完整：**回滚该事务**。

> 目的：保证 redo 和 binlog 一一对应。若 redo 提交但 binlog 丢失，主库提交而从库没有这条日志，主从不一致。

### 4.5 参数对应四层

- `innodb_flush_log_at_trx_commit`：控制 **InnoDB层→文件系统层 redo 刷盘**（prepare 阶段）。
- `sync_binlog`：控制 **SQL服务层→文件系统层 binlog 刷盘**（commit 阶段）。

生产安全组合：`innodb_flush_log_at_trx_commit=1` + `sync_binlog=1`

- prepare：redo 每次事务 fsync 落盘；commit：binlog 每次事务 fsync 落盘。
- 最安全，但性能有损耗。

> **补充（group commit）**：在高并发小事务场景，InnoDB 与 binlog 都有 **group commit（组提交）** 优化，多个事务合并一次 fsync，并非每个事务单独刷盘（但 `sync_binlog=1` + `innodb_flush_log_at_trx_commit=1` 是最严格的安全组合）。

### 4.6 高频易错点

1. ❌ 错误理解：两阶段提交会把 ibd 数据页刷盘。✅ 不会！commit 不刷 ibd 数据页，脏页是后台线程慢慢刷入文件系统；prepare/commit 只操作 redo 日志。
2. redo 属存储引擎层，binlog 属 SQL 服务层，两个不同模块，所以需要两阶段提交对齐。
3. **undo log 不参与两阶段提交**；undo 只用于回滚和 MVCC。
4. 分布式事务 XA 的两阶段提交和这个单机两阶段提交名字一样，**完全是两套东西**，不要混淆。

### 4.7 串联整套四层+两阶段完整流程（update 语句）

1. 连接层建立会话，接收 SQL。
2. SQL 服务层解析、优化，执行器调用 InnoDB 更新接口。
3. InnoDB 层：修改内存页、生成 undo、写 redo buffer。
4. 用户执行 commit：
   - ① InnoDB prepare：redo 刷磁盘标记 PREPARED（存储引擎→文件系统）。
   - ② Server 层写 binlog 并刷磁盘（SQL 服务层→文件系统）。
   - ③ InnoDB 把 redo 标记改为 COMMIT。
5. 返回成功；脏页后续异步刷 ibd 磁盘文件。

---

## 五、BufferPool 与脏页完整梳理

> Buffer Pool 是 **InnoDB 存储引擎层的核心内存区域**，MySQL 最核心缓存，单位：Page 页，固定 16KB 一页。

### 5.1 Buffer Pool 里面存什么

缓存磁盘读出的 InnoDB 数据页（.ibd 文件的页），包含：

1. 表数据页（聚簇索引叶子，真实行数据）。
2. 二级索引页（非主键索引）。
3. undo 日志页。
4. 自适应哈希索引 AHI 相关页。
5. Change Buffer 页。

❌ **不存**：redo log、binlog（redo 有独立 `Log Buffer`；binlog 属 Server 层，有 `binlog_cache`）。

> 简单记：所有从 ibd 磁盘文件加载进来的页，全部放在 Buffer Pool。

**Buffer Pool 内部 3 种链表：**

1. **Free 空闲链表**：空闲干净页，还没存放任何磁盘数据。
2. **LRU 链表**：已加载数据的页，冷热链表，管理访问热度。
3. **Flush 刷新链表**：存放脏页（内存改过还没刷回 ibd 的页）。

> 同一个脏页会同时存在于 LRU 链表 + Flush 链表。

### 5.2 BufferPool 能存多少数据

参数：`innodb_buffer_pool_size`，**生产建议物理内存 50%-70%**。

- 例：机器内存 16G，buffer_pool 设 10G；最多缓存 10G 的 16KB 数据页。
- 内部拆多实例：`innodb_buffer_pool_instances`，大于 1G 自动拆分，减少锁竞争。

> 最大能缓存的数据量 = innodb_buffer_pool_size。数据库总数据量 > buffer_pool，就一定会有磁盘 IO。

### 5.3 BufferPool 什么时候清理/淘汰页

淘汰发生：**Free 链表没有空闲页，需要读取新的磁盘页进内存**。

LRU 链表分两部分：

- **young 区（热区）**：频繁访问的页，链表头部。
- **old 区（冷区）**：很久没访问的页，链表尾部。

淘汰逻辑：

1. 新读入磁盘页优先放 LRU 的 old 区头部。
2. 再次访问则移动到 young 热区。
3. Free 链表耗尽，淘汰 LRU 链表 old 区尾部的页：
   - 干净页：直接丢弃，回收回 Free 链表。
   - 脏页：**必须先刷盘**（写 ibd），刷完变干净页再回收。

> ⚠️ BufferPool 不会主动"清理删除业务数据"；淘汰只是驱逐内存页，磁盘 ibd 还保留原始数据。Buffer Pool 只是缓存，不是持久存储。

### 5.4 脏页是什么

> **脏页(Dirty Page)：内存 Buffer Pool 里的页数据被修改了，但还没刷回磁盘 .ibd 文件。**

- 内存页内容 ≠ 磁盘 ibd 页内容 → 脏页。
- 内存页内容 == 磁盘 ibd 页内容 → 干净页。

> **redo 不等于脏页**；redo 是日志，脏页是被修改的数据页。事务 commit **不会把脏页立刻刷 ibd**，只刷 redo、binlog；脏页留在内存后台慢慢刷盘。

### 5.5 脏页完整生命周期：产生 → 存在 → 刷盘清理

**① 脏页如何产生：**

1. update/delete/insert 修改一行记录。
2. 数据页已加载进 Buffer Pool，直接修改内存里的页。
3. 内存页和磁盘 ibd 不一致 → 标记为脏页，加入 Flush 链表。
4. 同时把修改写入 redo log buffer（WAL，先写日志）。

> 此时磁盘 ibd 没变，但 redo log 已记录变更。若页不在 Buffer Pool：先读磁盘加载进 BP 再修改，变脏页。

**② 脏页什么时候刷盘（4 种触发）：**

| 场景              | 说明                                                       | 是否阻塞业务                       |
| --------------- | -------------------------------------------------------- | ---------------------------- |
| 后台线程定时刷脏        | `page_cleaner` 线程，依据 `innodb_max_dirty_pages_pct`（默认75%） | 不阻塞                          |
| LRU 淘汰刷脏        | Free 链表没空闲页，淘汰 old 尾部遇脏页必须同步刷盘                           | **会阻塞，业务抖动**                 |
| redo log 快写满强制刷 | redo 循环文件快满，强制刷脏释放可复用空间                                  | **严重卡顿，Innodb_log_waits 上涨** |
| 数据库关闭           | shutdown 把所有脏页刷到 ibd                                     | —                            |

> 场景③原理：redo 里记录的变更对应脏页；脏页刷到 ibd 后，这部分 redo 日志可覆盖重用。

> 常用控制参数：`innodb_max_dirty_pages_pct`（脏页上限，默认 75%）、`innodb_max_dirty_pages_pct_lwm`（低水位触发值）、`innodb_page_cleaners`（后台刷脏页线程数），这些影响后台刷盘强度与 LRU 淘汰刷盘行为。相关参数还有 `innodb_flush_neighbors`、`innodb_lru_scan_depth`（不同版本变量名略有差异）。

**③ 刷脏完成之后：**
脏页写入 ibd；内存页与磁盘一致，变干净页；从 Flush 链表移除；仍留在 LRU 链表继续提供查询，不直接回收。

### 5.6 关键区分（面试高频）

1. **redo log 和脏页区别**：
   - redo log：记录修改操作，日志文件，崩溃恢复用，循环文件；prepare 阶段刷盘。
   - 脏页：被修改的数据页（真实表数据），后续刷入 ibd；commit 不刷脏页。
   - **简短记忆：redo = 日志（用于恢复），脏页 = 数据页（内存中变更，最终刷到 ibd）。commit 刷的是 redo（和 binlog），而不是脏页。**
2. 崩溃断电：脏页在内存直接消失，依靠已落盘的 redo log 重启恢复。
3. Change Buffer 也是 Buffer Pool 里的脏页，最终刷到 ibd。
4. Double-Write 双写缓冲：脏页刷 ibd 时使用，防 16K 页写一半断电损坏。当 DoubleWrite 次数异常上升时，排查方向：磁盘延迟、脏页过多、IOPS 饱和。

### 5.7 相关监控指标

```
Innodb_buffer_pool_pages_total     -- BufferPool总页数
Innodb_buffer_pool_pages_free      -- 空闲页
Innodb_buffer_pool_pages_dirty     -- 当前脏页数量
Innodb_buffer_pool_reads           -- 磁盘物理读（缓存未命中）
Innodb_pages_written               -- 已刷盘的页总数
Innodb_log_waits                   -- redo log空间不足等待，出现代表刷脏跟不上写入
```

脏页占比公式：`脏页占比 = Innodb_buffer_pool_pages_dirty / Innodb_buffer_pool_pages_total`

### 5.8 极简背诵总结

1. BufferPool 缓存 ibd 磁盘的数据、索引页；大小由 `innodb_buffer_pool_size` 控制。
2. 没有空闲页时 LRU 淘汰 old 冷页；脏页淘汰前必须先刷盘。
3. **脏页：内存改了，ibd 磁盘没改的数据页。**
4. 脏页 4 种刷盘时机：后台定时、LRU 淘汰、redo log 快满强制刷、数据库关闭。
5. commit 事务**不会刷脏页到 ibd，只刷 redo/binlog**，脏页后台异步处理。
6. redo 是日志，脏页是数据页，两者完全不同。

---

## 六、BufferPool 与无索引字段

> ❌ 不能理解为"BufferPool 只缓存索引，普通字段不进内存"。

> BufferPool 操作的粒度是 **16KB 的数据 Page（页），不是字段，不是行，不是索引**。

### 6.1 核心概念

InnoDB 的页（page）是 Buffer Pool 的最小单位，默认页大小为 **16KB**（大多数发行版），但页面大小可以在编译或特定配置下不同。重要的是：BufferPool **按页加载/驱逐，不是按字段或单行**。需要注意大列（BLOB/TEXT）在不同 row_format（COMPACT/REDUNDANT vs DYNAMIC/COMPRESSED）下可能采用**溢出页（overflow/外部页）**存储，只有页内存放的前缀（或指针）在聚簇页中，其余大字段数据存放在单独的溢出页，这些溢出页也会**单独进入 BufferPool（按需加载）**。

举例：表 `user(id 主键, name, age, address)`，id 主键（聚簇索引），name 建索引；age、address 没有索引。聚簇索引叶子节点页存整行：`id,name,age,address`。

1. `select id,name from user where id=100;` 走主键索引，读聚簇索引页，**整页进 BufferPool，age、address 无索引字段也一并加载**，只是 SQL 没读它们。
2. `select address from user where id=100;` 按主键找到聚簇索引页，整页载入 BP，取出 address。
3. `select address from user where name='xxx'` name 是二级索引，二级索引叶子存主键 id；先读二级索引页进 BP 拿到 id，**回表读取聚簇索引数据页进 BP** 拿 address。

> ✅ **无索引的普通字段，是跟着聚簇索引的数据页一起进入 BufferPool 内存。**

> 说明：二级索引叶子页只存索引键与聚簇主键（primary key）；若查询需要非索引字段，则必须**回表**读取聚簇索引叶子页（full row）；若二级索引包含了查询所需的全部列（覆盖索引 / index-only），则**无需回表**，节省了对聚簇页的 I/O 并降低了 BufferPool 额外占用。

### 6.2 那什么页不会进 BufferPool？

1. 没被访问过的数据页。
2. 溢出页（TEXT/BLOB 的外部页）仅在被访问时**按需加载**到 BufferPool，并非永不进入。
3. redo log、binlog（不属于数据页，不走 BufferPool）。

### 6.3 二级索引页进 BufferPool 吗？

会。访问二级索引，**整个二级索引 Page 完整加载进 BufferPool**。二级索引页只存索引键+主键，不存普通非索引字段。

**区分两个东西：**

- **索引页**：存索引 key，进入 BP。
- **聚簇索引叶子数据页**：存放完整行，包含所有普通字段，全部进入 BP。

> 误区：❌"BufferPool 只缓存索引，普通字段不进内存"；✔ BufferPool 缓存**磁盘页**，一页里有什么内存就缓存什么，普通字段是聚簇索引页的一部分，自然被缓存。
> 
> 注意：虽然覆盖索引避免回表，但**二级索引页本身也会占用 BufferPool**（通常比存放整行的聚簇页少），因此设计索引时要综合考虑 IO 与内存占用。

### 6.4 通俗总结

1. BufferPool 最小单元是**页 Page，不是字段**。一页被读入内存，页上所有列（不管有没有索引）全在内存。
2. **普通无索引字段本身没有独立页，它依附聚簇索引的数据页存在磁盘上。访问这一页，无索引字段就跟着进 BufferPool。**
3. 二级索引页里面**没有普通字段**，只有索引列+主键，想拿无索引字段必须回表加载聚簇索引数据页。
4. 没访问过的页，无论有没有索引，都不会加载进 BufferPool。

> BufferPool 的 LRU 管理与后台 `page_cleaner` / `innodb_page_cleaners` 参数共同影响脏页刷写和淘汰策略；参数如 `innodb_lru_scan_depth`、`innodb_page_cleaners`、`innodb_max_dirty_pages_pct` 等会影响刷脏/驱逐行为。

### 6.5 举例场景对比

| SQL                                 | 读取什么页进入 BufferPool | 内存含无索引字段？                                         |
| ----------------------------------- | ------------------ | ------------------------------------------------- |
| `select id from t where id=1`       | 聚簇索引数据页            | ✅ 有（查询会加载聚簇索引叶子页，页内包含无索引字段，因此这些字段会被读入 BufferPool） |
| `select name from t where name='a'` | 二级索引页              | ❌ 二级索引页无 age/address；不回表则不会加载聚簇页                  |
| `select age from t where name='a'`  | 二级索引页 + 回表聚簇数据页    | ✅ age 进入内存                                        |

### 6.6 延伸面试点：覆盖索引为什么快

> `select id,name from t where name='xxx'` 二级索引页就包含全部所需字段，**不需要回表读聚簇索引数据页**，不加载含一大堆普通字段的数据页，减少磁盘 IO，也减少 BufferPool 内存占用。

> 不用覆盖索引则要回表，把整行聚簇索引页加载进 BP，大量无关无索引字段一并载入，占用内存。

补充：BufferPool 淘汰也是**按页淘汰**，不会单独把某个无索引字段清出内存；要么整页在内存，要么整页被驱逐。

### 6.7 小结与要点速查

- BufferPool 最小单位：**页**（默认 16KB），按页加载/驱逐。
- 聚簇索引叶子页包含整行（含无索引字段）→ 访问主键或回表会把整页加载到 BP。
- 二级索引页仅含索引键 + 主键，回表才会访问聚簇页。
- 大字段（BLOB/TEXT）可能使用外部溢出页，**按需加载**。
- 设计时尽量使用覆盖索引减少回表，但也要考虑索引页对 BufferPool 的额外占用。

---

## 七、长连接与僵尸半开连接治理

> 适用：Java + MySQL 生产环境，HikariCP 连接池。包含长短连接对比、参数配置、故障原理、风险、防护方案、面试要点。

### 7.1 基础概念：长连接 vs 短连接

**短连接**：每次业务请求建立 TCP 连接 → 执行 SQL → 客户端主动 close 关闭。PHP 默认模式；Java 不用连接池直接建连接也属短连接。

- 优点：用完即释放，不长期占用连接。
- 缺点：高并发下频繁创建销毁连接，线程、TCP 握手、认证开销大；瞬时峰值易打满 `max_connections`。
- 重点指标：`Threads_created`。

**长连接（连接池复用，HikariCP/Druid）**：连接建立后不关闭，保存在连接池反复复用。Java 微服务主流。

- 优点：减少 TCP 握手、MySQL 认证开销，性能好。
- 缺点：空闲连接持续占用连接名额；会出现僵死连接、连接泄漏风险。
- 重点指标：`Threads_connected`（当前连接数）、`Threads_running`（当前运行/非 Sleep 的线程数）、`Threads_created`（累计新建线程）、`Connections`（累计连接尝试数）。

| 项目          | 短连接（无连接池）                                       | 长连接（Hikari 连接池）                                       |
| ----------- | ----------------------------------------------- | ----------------------------------------------------- |
| 重点 MySQL 参数 | max_connections、max_user_connections            | max_connections、**wait_timeout**、max_user_connections |
| 次要参数        | wait_timeout 几乎无效                               | interactive_timeout（控制 Navicat 等交互式客户端）               |
| 核心监控指标      | Threads_created                                 | Threads_connected、Threads_running                     |
| 主要风险        | 频繁创建销毁线程；瞬时峰值打满连接；CPU 抖动                        | wait_timeout 导致僵死连接；程序连接泄漏；大量 Sleep 空闲连接占用内存与连接数      |
| 线程表现        | Threads_connected 瞬时冲高快速回落；Threads_created 持续上涨 | Threads_connected 维持高位平稳，Threads_created 几乎不涨         |

> 约束规则：长连接池配置 `idleTimeout(客户端) < wait_timeout(MySQL服务端)`，预留 30-60s 安全余量。

### 7.2 核心参数说明

#### MySQL 服务端参数（mysqld）

| 参数                     | 位置                  | 含义                                                                       |
| ---------------------- | ------------------- | ------------------------------------------------------------------------ |
| `max_connections`      | my.cnf              | 实例全局最大并发连接上限                                                             |
| `max_user_connections` | MySQL SQL           | 单账号最大连接数，防止单个业务占满实例                                                      |
| `wait_timeout`         | my.cnf / set global | **非交互式连接空闲超时，单位秒，JDBC 连接池受该参数控制**。空闲超时 MySQL 主动关闭连接。global 修改仅对**新连接生效** |
| `interactive_timeout`  | my.cnf              | 交互式客户端（mysql 命令行、Navicat）空闲超时，Java JDBC 不受此参数控制                          |

> 生产建议：`wait_timeout` 作为最后兜底机制，应**保持大于应用连接池的 maxLifetime**。常见做法：让 maxLifetime 明显小于 wait_timeout（例如 maxLifetime=30min，wait_timeout 保持默认 8h 或至少 1h），让应用端优先回收连接而不是被服务端无预告关闭。若必须把 wait_timeout 设得较低（例如 10 分钟）以加速半开连接回收，务必把连接池的 maxLifetime/idleTimeout/keepaliveTime 一并下调匹配，避免频繁拿到被服务端断掉的连接。

#### Java HikariCP 连接池参数（应用侧 yaml 配置，MySQL 无感知）

| 参数                                  | 单位  | 说明                                                   |
| ----------------------------------- | --- | ---------------------------------------------------- |
| maximum-pool-size                   | -   | 连接池最大连接数，宕机时最多遗留该数值的僵尸连接，不要盲目调大                      |
| idle-timeout                        | 毫秒  | **池中空闲连接回收时间，必须小于 MySQL wait_timeout**               |
| keepalive-time                      | 毫秒  | **应用层心跳，周期性执行 `SELECT 1`，检测池内连接是否僵死；Java 进程死亡后完全失效** |
| data-source-properties.tcpKeepAlive | -   | true 开启 socket 的 SO_KEEPALIVE，才能启用操作系统内核 TCP 保活      |

示例配置：

```yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 20
      idle-timeout: 300000       # 5分钟
      keepalive-time: 240000     # 4分钟，keepaliveTime < idleTimeout
      data-source-properties:
        tcpKeepAlive: true
```

JDBC URL 示例（增加保活 + 自定义连接标记）：

```
jdbc:mysql://mysql-ip:3306/business?tcpKeepAlive=true&connectionAttributes=app:order-service,pod:order-01
```

> `connectionAttributes` 用于在 `performance_schema.session_connect_attrs` 区分不同微服务实例，方便排查连接来源。

#### OS 内核 TCP Keepalive 参数

> 属于操作系统，不是 MySQL、不是 Java 参数。

**Linux（Java 应用服务器，非 MySQL 机器）**

```conf
# /etc/sysctl.conf
net.ipv4.tcp_keepalive_time = 300     # 连接空闲5分钟开始发探测包，单位秒
net.ipv4.tcp_keepalive_intvl = 30     # 探测失败间隔30s重试
net.ipv4.tcp_keepalive_probes = 3     # 重试3次无应答关闭socket
```

总探测超时 = 300 + 30*3 = **390 秒（6 分 30 秒）**。生效：`sysctl -p`，热生效无需重启机器。

**Windows Server**
注册表路径：`HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters`

| 注册表项              | 值(毫秒)  |
| ----------------- | ------ |
| KeepAliveTime     | 300000 |
| KeepAliveInterval | 30000  |
| TcpMaxDataRetries | 3      |

> ⚠️单位是毫秒，修改注册表**必须重启服务器**，生产一般不改动 Windows 内核保活，优先依靠应用层+MySQL 兜底。

> 重要前提：socket 必须开启 `SO_KEEPALIVE`（JDBC `tcpKeepAlive=true`）内核参数才会生效；否则配置无效。

### 7.3 参数约束与推荐配置详解

#### 7.3.1 HikariCP 核心参数详解表

| 参数名                | 默认值           | 单位  | 核心行为                                                                                                                                                               | 关键约束与注意事项                                                                 |
| ------------------ | ------------- | --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| **max-lifetime**   | 1800000（30分钟） | 毫秒  | 1. 连接创建后累计存活的最大生命周期；2. 到期时连接空闲：直接关闭 socket，MySQL 连接立刻消失；3. 到期时连接正在业务使用：仅打 `evict=true` 驱逐标记，**不打断 SQL、不关闭 socket**，等业务归还连接时直接销毁、不回池；4. 附带 0~2.5% 随机抖动，防止批量连接同时过期雪崩 | 1. **必须小于 MySQL `wait_timeout`**，避免拿到僵死连接；2. 无法解决连接泄漏，泄漏的连接即使超期也不会主动关闭    |
| **idle-timeout**   | 600000（10分钟）  | 毫秒  | 1. 仅针对**已归还到池的空闲连接**生效；2. 后台 housekeeper 线程每 30 秒扫描，空闲时长超阈值则直接移除、关闭 socket                                                                                         | 1. 必须小于 `max-lifetime`，否则不生效；2. 正在被业务使用的连接，该参数完全不生效                       |
| **keepalive-time** | 0（关闭）         | 毫秒  | 1. 仅针对池中空闲连接做**应用层 SQL 心跳探活**（非 OS TCP-Keepalive）；2. 探活成功：连接放回池继续复用；3. 探活失败：销毁坏连接，池自动新建补齐                                                                          | 1. 必须明显小于 `idle-timeout`（建议几十秒~几分钟）；2. 仅处理空闲连接，正在业务使用的连接无法探活；3. 进程宕机时完全失效 |
| connection-timeout | 30000（30秒）    | 毫秒  | 业务从池获取连接的等待超时，拿不到连接直接抛出 `Connection is not available` 异常                                                                                                           | 与连接生命周期无关，仅控制获取连接的等待时长                                                    |
| maximum-pool-size  | 10            | -   | 连接池最大连接数，需匹配 MySQL `max_connections`，一般设置 8~20                                                                                                                     | 不能超过 MySQL 全局 `max_connections` 配置                                        |
| minimum-idle       | 10            | -   | 连接池最小空闲连接数，官方建议与 `maximum-pool-size` 保持一致，避免动态扩容缩容开销                                                                                                               | 需小于等于 `maximum-pool-size`                                                 |

> **与连接可用性相关的实务参数：**
> 
> - **leakDetectionThreshold**：检测连接泄漏（单位 ms），超过阈值未归还则记录堆栈，便于排查泄漏代码。
> - **validationTimeout / connectionTestQuery**：通过 JDBC 的 `isValid()` 做连接健康检测（Hikari 默认用 isValid），尽量不使用自动重连（autoReconnect），因为它掩盖真实连接失败问题。
> - **maxLifetime 到期**：不会中断正在运行的 SQL，仅在归还时被销毁；Hikari 会对 maxLifetime 到期做随机抖动，避免大量连接同时被关闭。

#### 7.3.2 MySQL 连接相关核心参数表

| 参数名                 | 默认值        | 单位  | 核心作用                                               | 使用场景                                                                          |
| ------------------- | ---------- | --- | -------------------------------------------------- | ----------------------------------------------------------------------------- |
| **wait_timeout**    | 28800（8小时） | 秒   | MySQL 服务端兜底清理机制：空闲连接超过该时长，MySQL 主动单方面关闭 socket     | 1. Java 进程崩溃、网络断连等异常场景，Java 无法主动关闭连接时，靠该参数清理 Sleep 僵尸连接；2. 正常连接池回收场景，该参数完全不生效 |
| interactive_timeout | 28800（8小时） | 秒   | 交互式客户端（如 mysql 命令行）的空闲连接超时，一般与 `wait_timeout` 保持一致 | 同 `wait_timeout`，仅针对交互式连接                                                     |
| max_connections     | 151        | -   | MySQL 全局最大并发连接总数，所有客户端连接数总和不能超过该值                  | 限制连接池 `maximum-pool-size` 的上限，避免连接数打满导致 MySQL 拒绝新连接                           |

#### 7.3.3 生产环境参数约束与推荐配置表

| 参数                 | 推荐值              | 强制约束                    | 核心目的                            |
| ------------------ | ---------------- | ----------------------- | ------------------------------- |
| keepalive-time     | 120000（2分钟）      | 必须 < idle-timeout       | 提前探活清理僵死连接，避免业务拿到坏连接报错          |
| idle-timeout       | 600000（10分钟）     | 必须 < max-lifetime       | 回收长期空闲的连接，释放多余资源                |
| max-lifetime       | 1800000（30分钟）    | 必须 < MySQL wait_timeout | 保证 Java 侧先主动销毁连接，不等 MySQL 被动踢连接 |
| MySQL wait_timeout | 28800（8小时，默认不修改） | 必须 > max-lifetime       | 作为异常场景的兜底清理，不干预正常连接池回收          |

> 完整约束顺序：`keepalive-time < idle-timeout < max-lifetime << MySQL wait_timeout`
> 
> **统一方案（解决 wait_timeout 冲突）**：默认情况下可保留 MySQL 的默认 wait_timeout（8 小时），并通过调整应用层连接池参数（maxLifetime、idleTimeout、keepaliveTime）来控制连接回收；如果在典型网络环境中遇到大量半开连接（例如云环境或频繁网络抖动），可考虑把 wait_timeout 调小（例如几百秒到 1 小时），但**务必同时把连接池的 maxLifetime/idleTimeout 调整为更小值**，保证应用优先回收连接并避免频繁拿到被服务端断开的连接。总原则：`max-lifetime < wait_timeout`（否则会出现应用"以为连接还可用但被服务端踢掉"的问题）。

#### 7.3.4 常见误区与正确认知对照表

| 错误认知                                          | 正确认知                                                                                       |
| --------------------------------------------- | ------------------------------------------------------------------------------------------ |
| max-lifetime=30 分钟，到点就会立刻打断正在跑的 SQL、关闭 socket | max-lifetime 到期时，仅给正在使用的连接打驱逐标记，**不会打断 SQL、不会关闭 socket**，等业务归还连接时才会销毁                      |
| keepalive-time 是操作系统的 TCP-Keepalive           | keepalive-time 是 Hikari 应用层的 SQL 心跳探活，云环境/网关通常会忽略内核 TCP-Keepalive，必须靠应用层心跳保证探活生效           |
| 长连接是 MySQL 开启的配置                              | 长连接是 Java 连接池的客户端行为，MySQL 仅被动接收连接、超时主动踢连接，没有"长连接开关"                                        |
| 连接泄漏可以靠 max-lifetime 自动回收                     | 连接泄漏时业务永不归还连接，max-lifetime 仅能打标记，无法主动关闭连接，只能靠 MySQL wait_timeout 兜底回收                      |
| 必须把 MySQL wait_timeout 改小，避免僵死连接              | 优先调小 Java 侧 max-lifetime，不建议修改 MySQL 默认 wait_timeout；改小会导致 MySQL 频繁断开连接，连接池不停重建连接，CPU 开销暴涨 |
| 启用 autoReconnect 能解决所有断连问题                    | autoReconnect 会**掩盖真实断连/网络问题**，不建议在生产环境依赖，应该靠连接池重试逻辑与探活策略                                  |

### 7.4 僵尸（半开 Sleep）连接产生原理

1. **正常场景：kill-9、正常关机**：Java 进程被杀，OS 发出 **TCP RST 复位包**；MySQL 收到 RST 立刻关闭 session，回收连接，不产生僵尸连接。
2. **故障场景：Java 机器断电、拔网线、内核 panic**：机器直接死掉，**OS 无法发出 RST 包**。
   - MySQL 服务端 TCP 状态仍认为连接存活，连接状态为 Sleep。
   - Java 机器已彻底失联 → **半开僵尸连接**。
   - 此时 Java 进程、JVM、OS 全部停止，Java 侧所有逻辑全部失效。

> 长连接池宕机，一次性遗留最多 `maximum-pool-size` 条僵尸连接；短连接只遗留宕机瞬间正在执行的少量连接。

### 7.5 Java 服务宕机场景分析

| 故障场景                   | 是否发送 RST  | 是否产生半开僵尸连接                |
| ---------------------- | --------- | ------------------------- |
| kill-9 Java 进程         | ✅发送 RST   | ❌无僵尸连接                    |
| 操作系统正常关机               | ✅发送 RST   | ❌无僵尸连接                    |
| 机器断电、网线断开              | ❌无法发送 RST | ✅产生半开 Sleep 僵尸连接          |
| 网络隔离、防火墙切断             | ❌无法发送 RST | ✅产生半开 Sleep 僵尸连接          |
| Java 死锁/FullGC 卡死，进程存活 | TCP 正常连通  | ❌半开；会出现僵死连接，由 Hikari 心跳检测 |

> 关键：**机器彻底断电死机，Hikari 心跳、OS TCP keepalive 全部停止运行，只能依靠 MySQL wait_timeout 兜底回收。**

### 7.6 三层保活机制分工

> 三者互不替代，分层防护。

1. **HikariCP keepaliveTime【Java 应用层】**：周期性执行 `SELECT 1` SQL 心跳。
   - ✅ Java 进程活着：识别池内僵死连接。
   - ❌ Java 宕机：JVM 停止，完全失效。
2. **OS 内核 TCP-Keepalive【TCP 内核层】**：发送 TCP 空 ACK 探测包。
   - ✅ 机器活着，只是网络断开：内核探测识别半开连接，关闭 socket，MySQL 回收连接。
   - ❌ 机器彻底断电死机：OS 停止，完全失效。
   - ⚠️ 局限：防火墙/NAT/云安全组可能丢弃 TCP 空探测包，会失效，仅作辅助。
   - ⚠️ 注意：内网 NAT、负载均衡和防火墙可能**丢弃或延迟 TCP 探测包**，使 OS keepalive 失效或延后。因此在云环境上应**更依赖应用层心跳 + 合理 wait_timeout**，而不是仅靠内核 keepalive。
3. **MySQL wait_timeout【MySQL 服务端，最终兜底】**：无论客户端状态，空闲超时 MySQL 主动关闭会话。
   - ✅ 任何场景都生效，是最后一道防线。

> Nacos / SpringBoot Actuator：**只管微服务业务流量摘除，完全不会清理 MySQL TCP 连接。** Nacos 把故障实例下线，只是不再路由新请求，存量半开僵尸连接依旧残留在 MySQL。

### 7.7 僵尸连接全部清理手段对比

| 手段                   | Java 已宕机，能否清理遗留僵尸连接  | 层级          | 备注                 |
| -------------------- | -------------------- | ----------- | ------------------ |
| 内核 TCP Keepalive     | ✅（机器存活，网络允许）         | OS 内核 TCP 层 | 断电死机直接失效；防火墙可能丢探测包 |
| MySQL wait_timeout   | ✅ 一定生效，兜底            | MySQL 应用层   | 需要等待超时时间           |
| 运维脚本 / pt-kill       | ✅ 可以，主动 KILL 会话      | 运维侧 SQL 操作  | 应急兜底，谨慎使用，防误杀正常会话  |
| TCP RST 包            | ✅（仅正常关机/kill-9，断电无效） | OS 内核       | 断电断网场景失效           |
| Hikari keepaliveTime | ❌ Java 宕机 JVM 停止     | Java 应用层    | 仅进程存活时检测坏连接        |
| Nacos / Actuator     | ❌ 完全无关               | 微服务注册发现     | 只摘除流量，不操作数据库连接     |

> **运维脚本 / pt-kill 使用建议**：在生产使用前务必在测试集群验证，使用 host/用户/SQL pattern 白名单并逐步删除；大量 KILL 可能引发额外 load（IO/日志），应**分批执行**，避免误杀正常会话。

### 7.8 Nacos 健康检查边界

1. Nacos 默认客户端心跳：Java 内部线程上报，**只能检测进程是否存活**；进程活着但业务卡死（死锁、线程池耗尽）识别不出。
2. Nacos HTTP 健康检查模式：Nacos 服务端主动调用 `/actuator/health`，可识别业务不可用。
3. 局限：**Nacos 没有任何能力感知、关闭 MySQL 半开 TCP 连接**。故障实例摘除只是不再分配业务流量。
4. **优雅下线流程建议**：在下线前先从负载均衡/注册中心摘除流量、等待连接空闲、主动调用连接池的 `close` 方法（或先关闭 DataSource），再停服务。这样可尽量避免产生新的半开连接和遗留连接。

### 7.9 生产完整落地方案（降低半开僵尸连接影响）

> 客观：机器彻底断电死机场景，没办法主动远程清理 MySQL 半开连接，只能加速回收、控制总量、监控告警。

**① Java 应用侧（长连接池重点）**

1. Hikari `maximum-pool-size` 不要设过大，控制宕机遗留僵尸连接的最大总量。
2. 设 `idle-timeout < wait_timeout`，连接池主动回收空闲连接。
3. 开启 `keepaliveTime` 做应用层心跳；开启 `tcpKeepAlive=true`。
4. JDBC 增加 `connectionAttributes` 打标记，方便定位连接来源。

> 短连接无连接池，不需要 Hikari 参数。

**② Java 服务器操作系统**

1. Linux：配置 tcp_keepalive 内核参数，sysctl -p 热生效，作为辅助。
2. Windows Server：不修改注册表，不依赖内核保活。

**③ MySQL 数据库侧（必配）**

1. 合理设置 `wait_timeout`：默认可保留 8 小时（靠应用层连接池参数控制回收）；若云环境频繁出现半开连接，可调小（如几百秒~1 小时），但**必须同步下调连接池 maxLifetime/idleTimeout** 匹配。

2. 设账号 `MAX_USER_CONNECTIONS`，防止单个业务僵尸连接占满实例。
   
   ```sql
   ALTER USER 'app'@'%' WITH MAX_USER_CONNECTIONS 50;
   ```
   
   > 注意：为账号设定 `MAX_USER_CONNECTIONS` 能防止单个账号把实例连接耗尽。语法 `ALTER USER ... WITH MAX_USER_CONNECTIONS` 在 MySQL 8+ 支持；老版本请用 `CREATE/GRANT` 资源限制方式或用户属性表实现。

**④ 微服务架构**

1. Nacos 开启 HTTP 健康检查，故障实例快速摘除流量。
2. 业务多实例部署，避免单点故障。

**⑤ 监控与应急**
监控指标告警：`Threads_connected` 总连接数、Sleep 状态连接数量。

```sql
SELECT count(*) FROM information_schema.processlist WHERE command='Sleep';
```

应急脚本部署在独立运维机器，**禁止部署在 Java 业务服务器**；只做人工应急，不建议自动定时 kill。

```sql
SELECT id,host,time FROM information_schema.processlist WHERE command='Sleep' AND time>600 AND host LIKE '10.0.0.%';
KILL 会话ID;
```

### 7.10 高频面试问答

**Q1：为什么长连接池会报 `Communications link failure`？**

> MySQL 服务端 `wait_timeout` 关闭空闲连接，但连接池不知道，继续复用这条僵死连接执行 SQL。
> 解决：连接池 `idleTimeout` 小于 MySQL 的 wait_timeout，开启心跳检测。

**Q2：Java 进程 kill-9 宕机，会不会遗留僵尸连接？**

> kill-9 OS 发出 RST 包，MySQL 立刻回收连接，不会残留；如果机器断电断网，发不出 RST，则产生半开 Sleep 僵尸连接。

**Q3：Java 服务宕机后，Hikari keepaliveTime 可以清理僵尸连接吗？**

> 不可以。心跳是 JVM 内部线程执行，进程死亡 JVM 停止，心跳完全不运行。只能靠 OS TCP keepalive（机器存活时）或 MySQL wait_timeout 兜底。

**Q4：短连接会不会产生半开僵尸连接？**

> 会。宕机瞬间还没有执行 close() 的正在处理请求的 TCP 连接同样变成半开；只是数量远小于长连接池。

**Q5：TCP keepalive 能不能替代 wait_timeout？**

> 不能。防火墙/NAT 会丢弃 TCP 探测包，会探测失效。wait_timeout 是最终兜底，生产环境必须配置。

**Q6：Nacos 实例下线，MySQL 连接会释放吗？**

> 不会。Nacos 只做服务注册发现，不操作 MySQL 的 TCP 连接，半开僵尸连接依旧残留在 MySQL。

**Q7：能不能彻底杜绝半开僵尸连接？**

> 不能。Java 主机彻底断电死机，本机 OS 停止运行，外部没有手段主动关闭远端 MySQL 上的会话。只能控制数量、加速回收、监控告警。

> 完整核心记忆：**Hikari 管活着的 Java；内核 TCP keepalive 管机器活着但网络断开；wait_timeout 管一切兜底；Nacos 只管业务流量，不管数据库连接。**

### 7.11 实用清单（监控 / 排查 SQL）

```sql
-- 查 Sleep 连接数
SELECT COUNT(*) FROM information_schema.processlist WHERE COMMAND='Sleep';

-- 查来源（host）为某网段的 Sleep
SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO
FROM information_schema.processlist
WHERE COMMAND='Sleep' AND HOST LIKE '10.0.0.%' ORDER BY TIME DESC LIMIT 100;

-- 查活跃连接（非 Sleep）
SELECT * FROM information_schema.processlist WHERE COMMAND<>'Sleep' ORDER BY TIME DESC LIMIT 100;

-- 查 MySQL wait_timeout / interactive_timeout
SHOW VARIABLES LIKE 'wait_timeout';
SHOW VARIABLES LIKE 'interactive_timeout';
```

> Hikari 连接池泄漏：在日志中寻找 `leakDetectionThreshold` 报告，并在代码中注入连接归还点监控。

### 7.12 Hikari 推荐生产参数（示例，需按实际业务校准）

- `maximum-pool-size`：按应用 QPS 与并发 SQL 数量估算，例如 10~100。
- `max-lifetime`：1800000 ms（30 min）。
- `idle-timeout`：600000 ms（10 min）。
- `keepalive-time`：120000 ms（2 min）（可选）。
- `leakDetectionThreshold`：300000 ms（5 min）用于排查泄漏（上线后可关闭）。
- `validationTimeout`：5000 ms。
- 注意：务必保证 `max-lifetime < MySQL wait_timeout`。

### 7.13 场景建议（决策树）

- 频繁出现 "Communications link failure" 且日志显示应用在重用死连接 → 先检查池的 `idleTimeout/maxLifetime` 与 MySQL `wait_timeout` 的关系，然后开启 keepalive / connection validation。
- 大量 Sleep 且机器经常宕机/重建 → 把 pool size 限制合理，设置 `max_user_connections` 保护实例，并增加监控告警。

---

## 附录：快速诊断 SQL 与版本检查速查

### 常用快速诊断 SQL

```sql
-- 当前连接 / 活动 SQL
SELECT * FROM information_schema.processlist WHERE COMMAND <> 'Sleep';

-- 活跃事务数
SELECT COUNT(*) FROM information_schema.innodb_trx;

-- 查找阻塞链
SELECT r.trx_id waiting_trx_id, r.trx_mysql_thread_id waiting_thread, r.trx_query waiting_query,
       b.trx_id blocking_trx_id, b.trx_mysql_thread_id blocking_thread, b.trx_query blocking_query
FROM information_schema.innodb_lock_waits w
JOIN information_schema.innodb_trx b ON w.blocking_trx_id = b.trx_id
JOIN information_schema.innodb_trx r ON w.requesting_trx_id = r.trx_id;

-- performance_schema 等待热点
SELECT EVENT_NAME, SUM_TIMER_WAIT/1000000000000 AS sec
FROM performance_schema.events_waits_summary_global_by_event_name
ORDER BY sec DESC LIMIT 20;
```

### 版本/发行区别提示

推荐在每个生产实例上运行以下命令确认实际行为，以免文档与实例不一致：

```sql
SHOW VARIABLES LIKE 'version%';
SHOW VARIABLES LIKE 'thread_cache_size';
SHOW VARIABLES LIKE 'innodb_flush_log_at_trx_commit';
SHOW VARIABLES LIKE 'sync_binlog';
```
