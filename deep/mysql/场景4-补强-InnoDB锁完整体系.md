---
title: "场景 4 补强 · InnoDB 锁完整体系（S/X锁、意向锁、插入意向锁等）"
summary: "在\"间隙锁 / Next-Key Lock\"基础上，补齐 InnoDB 锁体系的其余核心技术点。所有演示基于 tgaplock 表。"
date: 2026-04-08
author: "小番茄程序猿"
tags:
  - MySQL
  - 锁
category: deep
series: "mysql"
slug: "场景4-补强-InnoDB锁完整体系"
reading_time: 6
featured: false
draft: false
---
# 场景4 补强：InnoDB 锁完整体系（S/X锁、意向锁、插入意向锁等）

在"间隙锁 / Next-Key Lock"基础上，补齐 InnoDB 锁体系的其余核心技术点。所有演示基于 `t_gap_lock` 表。

## 一、共享锁 vs 排他锁（S/X）

| 锁类型 | SQL | 特性 |
|--------|-----|------|
| 共享锁（S） | `SELECT ... LOCK IN SHARE MODE` | 兼容，多个事务可同时持有 |
| 排他锁（X） | `SELECT ... FOR UPDATE` / UPDATE / DELETE | 互斥 |

**兼容矩阵（实测 `/innodb-lock/sx`）**：

| A 持有 | B 尝试 | B 等待实测 | 结论 |
|--------|--------|-----------|------|
| X | X | **1501ms** | 互斥 |
| S | X | **1501ms** | 冲突（S 锁不兼容 X） |
| S | S | **2ms** | 兼容（可并发读） |

并发共享锁（`/innodb-lock/sx/concurrent-shared`）：两个事务同时持 S 锁均成功（S+S 兼容）。

## 二、当前读 vs 快照读（`/innodb-lock/snapshot-current`）

实测（REPEATABLE READ 下）：
```
snapshotBefore: "A-10"           ← 事务B第一次快照读，建立快照
snapshotAfter:  "A-10"           ← A提交修改后，B快照读仍读旧值（MVCC快照）
currentRead:    "MODIFIED-BY-A"  ← B当前读(FOR UPDATE)读最新已提交值
```

**核心**：
- **快照读**（普通 SELECT）：走 MVCC，事务内第一次读固定快照，不加锁
- **当前读**（`FOR UPDATE`/`LOCK IN SHARE MODE`/UPDATE）：读最新已提交值 + 加锁

## 三、无索引条件全表锁（`/innodb-lock/no-index-fulltable`）

事务 A 用无索引条件（`note`）UPDATE 一行，实测：

| 项目 | 值 |
|------|-----|
| B 插入任意新行 | **被阻塞约 2001ms** |

**结论**：无索引条件的 UPDATE/DELETE 会**锁全表所有间隙**，即使插入位置与更新行无关，也会被阻塞。生产应避免大范围无索引 UPDATE。

## 四、插入意向锁（`/innodb-lock/insert-intention`）

- A 锁 category='A' 间隙后，B/C 往该间隙插入被阻塞（等待 A 释放）
- A 释放后，B/C **可并发插入不同位置**（插入意向锁之间兼容）

实测：B/C 均等待约 10 秒后插入成功（被 A 间隙锁阻塞；A 释放后并发插入）。

## 五、锁等待诊断（`/innodb-lock/diagnose`）

制造 A 持排他锁，诊断当前锁与事务：

```
locks: [TABLE IX, RECORD X,REC_NOT_GAP]   ← 意向排他锁 + 排他记录锁
trx:   [id=..., state=RUNNING]            ← 事务状态
```

**说明**：事务加行锁前先在表上加**意向锁（IX/IS）**，与表锁高效判断兼容性；通过 `data_locks`/`innodb_trx` 可诊断锁与事务状态。

## 六、意向锁（IS/IX）兼容矩阵（理论）

| | IS | IX | S | X |
|--|----|----|----|----|
| IS | 兼容 | 兼容 | 兼容 | 冲突 |
| IX | 兼容 | 兼容 | 冲突 | 冲突 |
| S | 兼容 | 冲突 | 兼容 | 冲突 |
| X | 冲突 | 冲突 | 冲突 | 冲突 |

**关键**：IX/IX 兼容（多事务可同时加行锁）；S（表锁）与 IX 冲突——即 `LOCK TABLES READ` 会阻塞行级写。

## 七、完整锁技术点清单

| 技术点 | 演示 | 状态 |
|--------|------|------|
| 记录锁 / 间隙锁 / Next-Key Lock | `/gaplock/*` | ✅ |
| 间隙锁阻塞插入 | `/gaplock/insert-block` | ✅ |
| 间隙锁死锁 | `/gaplock/deadlock` | ✅ |
| Next-Key 锁范围 | `/gaplock/locks` | ✅ |
| S/X 锁兼容矩阵 | `/innodb-lock/sx` | ✅ |
| 当前读 vs 快照读 | `/innodb-lock/snapshot-current` | ✅ |
| 无索引全表锁 | `/innodb-lock/no-index-fulltable` | ✅ |
| 插入意向锁 | `/innodb-lock/insert-intention` | ✅ |
| 锁等待诊断 | `/innodb-lock/diagnose` | ✅ |

## 八、要点速览

> **InnoDB 锁完整体系**：行锁分 S（共享）/X（排他），S/S 兼容、S/X 与 X/X 互斥（实测 X/X、S/X 阻塞 1.5s，S/S 立即）；行锁前加表级意向锁（IS/IX），IX/IX 兼容、S 与 IX 冲突。锁分记录锁、间隙锁、Next-Key Lock：主键等值只锁记录，非唯一索引/范围查询锁间隙防幻读，无索引条件锁全表。普通 SELECT 走 MVCC 快照读不加锁，FOR UPDATE 走当前读加锁（实测快照读读旧值、当前读读最新值）。
