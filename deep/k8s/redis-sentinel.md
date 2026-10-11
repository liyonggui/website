---
title: "Redis 1主1从3哨兵（自动故障转移）"
summary: "在「1主1从」之上加 3 个哨兵，补齐最后一块：主库宕机自动选主 + 客户端经哨兵发现当前主库。 这是 Redis 高可用的完整形态，也是「在 K8s 上跑有状态中间件」最有收获的…"
date: 2026-09-10
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
  - Redis
category: deep
series: "k8s"
slug: "redis-sentinel"
reading_time: 6
featured: false
draft: false
---
# Redis 1主1从3哨兵（自动故障转移）

在「1主1从」之上加 3 个哨兵，补齐最后一块：**主库宕机自动选主 + 客户端经哨兵发现当前主库**。
这是 Redis 高可用的完整形态，也是「在 K8s 上跑有状态中间件」最有收获的一课。

## 架构
```
            ┌─────────────┐
客户端 ─────▶│ redis-sentinel │ (ClusterIP :26379，3 副本)
            └──────┬──────┘
                   │ 监控 / 自动选主
        ┌──────────┴──────────┐
   redis-0 (主)          redis-1 (从)
   Headless: redis-0.redis  Headless: redis-1.redis
```
- 哨兵无状态 → 用 **Deployment 3 副本**（满足 quorum=2）。
- 哨兵靠 `redis-0.redis`（Headless 稳定 DNS）找到初始主库；彼此靠主库 `__sentinel__:hello` 频道互相发现。
- 主库有 `requirepass` → 哨兵配置里必须带 `sentinel auth-pass mymaster <密码>`。

## 部署（WSL）
前提：redis 的 1主1从 已在跑。直接 apply 哨兵即可：
```bash
kubectl apply -f redis-sentinel.yaml -f redis-sentinel-service.yaml -n sc-demo
```

## 验证哨兵工作状态
```bash
# 3 个哨兵都 Running
kubectl get pods -n sc-demo -l app=redis-sentinel

# 问哨兵：当前主库是谁（返回主库 Pod IP + 6379）
kubectl exec -it redis-sentinel-xxxx -n sc-demo -- sh -c 'redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster'

# 哨兵是否互相发现：应列出另外 2 个哨兵
kubectl exec -it redis-sentinel-xxxx -n sc-demo -- sh -c 'redis-cli -p 26379 SENTINEL sentinels mymaster'
```
（pod 名用补全：`kubectl get pods -n sc-demo -l app=redis-sentinel` 看实际名字）

## 验证自动故障转移（核心演示）
```bash
# ① 先确认当前主库
kubectl exec -it redis-sentinel-xxxx -n sc-demo -- sh -c 'redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster'
#   （假设是 redis-0）

# ② 干掉主库 Pod（模拟宕机）
kubectl delete pod redis-0 -n sc-demo

# ③ 等约 5~10 秒（down-after 5000ms + 选主耗时），再看主库 —— 应已变成 redis-1 的 IP
kubectl exec -it redis-sentinel-xxxx -n sc-demo -- sh -c 'redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster'

# ④ 确认 redis-1 已升主
kubectl exec -it redis-1 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" info replication'   # role:master
```

## 一个真实的知识点（务必理解）
redis-0 被删后，K3s 会**自动重建它**，而 redis StatefulSet 的启动脚本规定「0 号永远是主」，
所以 redis-0 重启后又以 master 身份起来 → 会短暂出现**两个主**（脑裂窗口）。
此时哨兵会检测到 redis-0 重新上线，并向它发 `REPLICAOF`，把它**降级为 redis-1 的从库**。
过一会儿再查：
```bash
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" info replication'
# role:slave, master_host:redis-1.redis  ← 哨兵已完成 Reconcile
```
这正好演示了哨兵的核心职责：**不仅选主，还会把旧主重新纳入新拓扑。**

> 生产环境要彻底避免“0号永远主”的脚本写法，通常用 Redis Operator 或让哨兵/初始化脚本动态决定角色；
> 本例用最直观的方式把机制讲清楚，生产请改用成熟方案（如 Redis Operator / K8s Operator）。

## 客户端怎么连（接入要点）
不要直连 `redis:6379` 或某个固定 Pod，而是：
1. 连 `redis-sentinel:26379`（任意哨兵）；
2. 发 `SENTINEL get-master-addr-by-name mymaster` 拿到当前主库地址；
3. 再连真正的主库做写、从库做读。
常用客户端（如 jedis/lettuce）都内置「哨兵模式」，配哨兵地址列表即可自动跟进主从切换。

## 三档 Redis on K8s 全景
| 档位 | 资源 | 解决什么 | 自动故障转移 |
|---|---|---|---|
| 单实例 | StatefulSet + PVC | 有状态 + 持久化 | ❌ |
| 1主1从 | + Headless Service | 读写分离 + 数据冗余 | ❌（主挂从只读） |
| 1主1从3哨兵 | + Sentinel×3 | **主挂自动选主** | ✅ |
