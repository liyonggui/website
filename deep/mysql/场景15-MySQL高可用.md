---
title: "场景 15 · MySQL 高可用（主从切换 / 故障演练）"
summary: "单点 MySQL 一旦故障，整个服务不可用。高可用（HA） 的核心是：故障时能快速探测 + 自动切换，让从库接替主库继续服务。本项目基于主库(3306) + 从库(3307)复制拓…"
date: 2026-03-29
author: "小番茄程序猿"
tags:
  - MySQL
category: deep
series: "mysql"
slug: "场景15-MySQL高可用"
reading_time: 8
featured: false
draft: false
---
# 场景15：MySQL 高可用（主从切换 / 故障演练）

## 一、问题背景

单点 MySQL 一旦故障，整个服务不可用。**高可用（HA）** 的核心是：故障时能**快速探测 + 自动切换**，让从库接替主库继续服务。本项目基于主库(3306) + 从库(3307)复制拓扑，演示 HA 能力。

> 高可用分两层：
> - **读高可用**：主库故障时读请求切到从库（读写分离天然提供）
> - **写高可用**：需要"主从切换"（提升从库为新主），常由 MHA / Orchestrator 自动完成

## 二、演示接口

```bash
# 1. 复制拓扑监控（主/从 server_id、版本、只读状态）
curl "http://localhost:8080/ha/topology"

# 2. 健康探测（主库/从库可用性 + 复制链路健康）
curl "http://localhost:8080/ha/probe"

# 3. 读写分离故障转移演练：主库正常（读走主库）
curl "http://localhost:8080/ha/failover-demo?simulateMasterDown=false&rounds=20"

# 4. 读写分离故障转移演练：主库故障（读自动切从库）
curl "http://localhost:8080/ha/failover-demo?simulateMasterDown=true&rounds=20"

# 5. 主从切换流程演示（提升从库为新主库的 SQL 步骤）
curl "http://localhost:8080/ha/switch-plan"

# 6. 故障转移吞吐对比（正常读主 vs 主故障切从）
curl "http://localhost:8080/ha/failover-bench?concurrency=50&total=20000"
```

## 三、实测结果

### 1. 拓扑监控
```
master: server_id=1, read_only=0, version=8.4.9
replica: server_id=2, read_only=1, version=8.4.9
```
→ 主写从读，拓扑清晰，从库 `read_only=1` 防止误写。

### 2. 健康探测
主库/从库均 `alive=true`，复制链路 `replicationHealthy=true`（写主读从验证）。

### 3. 故障转移演练（读写分离提供读高可用）
| 场景 | masterOk | replicaOk | 读可用性 |
|------|----------|-----------|---------|
| 主库正常 | 10/10 | 0/10 | 100% |
| **主库故障** | 0/10 | **10/10** | **100%** |

→ 主库故障时读请求**自动切从库**，读服务持续可用（读高可用）。

### 4. 故障转移吞吐对比
| 方案 | QPS |
|------|-----|
| 正常读主库 | 8892 |
| 故障切从库 | 16722 |

→ 从库读吞吐甚至更高（主库还承担写负载）。**从库容量决定故障期间的读能力**，若从库承接不下全部读流量会雪崩。

## 四、主从切换流程（写高可用）

```
1. 确认从库追平：SHOW REPLICA STATUS → Seconds_Behind_Source=0
2. 停主库写入（可选，视切换策略）
3. 停止从库复制：STOP REPLICA;
4. 提升从库为主库：RESET REPLICA ALL;
5. 关闭从库只读：SET GLOBAL read_only=OFF; SET GLOBAL super_read_only=OFF;
6. 其他从库改指向新主库：CHANGE REPLICATION SOURCE TO ...; START REPLICA;
7. 应用层切新主库地址，原主库降级为新从库重新挂复制
```

切换策略对比：**MHA**（半自动，分钟级）/ **Orchestrator**（自动，秒级）/ 手动脚本。生产常用 **Orchestrator + 探活** 实现自动故障转移（探活 → 选主 → 切换 → VIP 漂移）。

## 五、生产环境故障转移中间件对比

| 方案 | 类型 | 自动故障转移 | 典型 RTO | 特点 |
|------|------|:---:|------|------|
| **Orchestrator** | 自动 | ✅ | 秒级 | 当前最主流，能处理复杂拓扑，GitHub 大规模生产验证 |
| **MHA** | 半自动/准自动 | ✅ | 秒~分钟级 | 老牌经典，成熟稳定，需脚本编排 |
| **MGR / InnoDB Cluster** | 官方组复制 | ✅ | 秒级 | MySQL 原生，Router 做路由，官方推荐 |
| **PXC (Galera)** | 同步多主 | ✅ | 秒级 | 强一致，多主，但写放大、对网络敏感 |
| **DRBD + Pacemaker** | 存储层 | ✅ | 秒级 | 双机热备，需共享/块复制，VIP 漂移 |
| **Keepalived** | VIP 层 | ✅（仅漂 IP） | 秒级 | 只漂 VIP，不切换数据库状态，需配合探活 |

**选型建议**
- 单主多从 + 读写分离：**Orchestrator** 或 **MHA**
- 要官方原生、自动化、易运维：**InnoDB Cluster (MGR + MySQL Router)**
- 强一致、多主、不允许丢数据：**MGR 多主 / PXC**
- 云数据库（RDS/Aurora）：自带高可用，无需自建中间件

**原理要点**
- **Orchestrator**：持续监控复制拓扑，故障自动选主（数据最全的从库）→ 提升 → 重挂其他从库 → VIP/路由切换；Raft 协调器保证自身高可用。
- **MHA**：Manager 探活主库，故障自动选主、补 binlog（尽力补齐）、提升、重挂复制。
- **InnoDB Cluster = MGR + MySQL Shell + MySQL Router**：MGR 组协议自动选主，Router 做读写路由+故障转移，应用几乎无感。
- **PXC / Galera**：同步多主广播，任一点故障自动摘除，剩余节点继续服务（强一致无丢数据）。
- 配合**半同步复制**可将 RPO 降到 0；**应用接入**（MySQL Router / VIP / 连接池多节点 failover）三者结合实现应用无感切换。

## 六、高可用工程要点

1. **从库容量要够**：故障转移后从库承接全部读流量，容量不足会二次雪崩。
2. **自动故障转移工具**：Orchestrator 自动探活/选主/VIP 漂移，比手动快且稳。
3. **数据追平才切**：切换前必须 `Seconds_Behind_Source=0`，否则丢数据（GTID 复制可自动定位位置）。
4. **半同步复制**：主库提交需从库 ACK，减少切换时丢数据风险（RPO≈0）。
5. **VIP/路由漂移**：应用连 VIP，切换时 VIP 漂移到新主，应用无感。
6. **只读治理**：从库 `read_only+super_read_only` 双保险，防止误写。
7. **故障演练常态化**：定期演练切换，验证切换脚本与流程，避免"纸上 HA 真宕机"。

## 七、要点速览

> **MySQL 高可用**：主从复制 + 读写分离提供**读高可用**（主库故障读切从库，可用性 100%）；**写高可用**需主从切换（STOP REPLICA → RESET REPLICA ALL → 关只读 → 重挂复制 → 应用切新主），常由 MHA/Orchestrator 自动完成（探活/选主/VIP 漂移）。关键点：从库容量要够承接故障读流量、切换前数据追平（Seconds_Behind_Source=0）、半同步复制降低 RPO、VIP 漂移让应用无感、定期故障演练。
