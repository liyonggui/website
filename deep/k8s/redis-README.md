---
title: "单实例 Redis on K3s（学习用）"
summary: "把 Redis 放上 K8s，比 sc-user 只多一个概念：数据要持久化、身份要稳定。 对应到资源就是三件套：StatefulSet + volumeClaimTemplate…"
date: 2026-09-08
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
  - Redis
category: deep
series: "k8s"
slug: "redis-README"
reading_time: 3
featured: false
draft: false
---
# 单实例 Redis on K3s（学习用）

把 Redis 放上 K8s，比 `sc-user` 只多一个概念：**数据要持久化、身份要稳定**。
对应到资源就是三件套：`StatefulSet` + `volumeClaimTemplates(PVC)` + `Service`。

## 文件清单
| 文件 | 作用 |
|---|---|
| `redis-secret.yaml` | Redis 密码（Secret，不进明文） |
| `redis-statefulset.yaml` | StatefulSet：1 个 Pod + 1Gi 持久盘，挂 `/data` |
| `redis-service.yaml` | ClusterIP Service，服务名 `redis:6379` |

## 部署（WSL 里执行）
```bash
kubectl apply -f redis-secret.yaml -f redis-statefulset.yaml -f redis-service.yaml -n sc-demo
```

## 验证
```bash
# 看 Pod 是不是 Running（注意 Pod 名是 redis-0，不是随机 hash）
kubectl get pod redis-0 -n sc-demo

# 进容器敲一条命令，确认能写、能读、数据落盘
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" set hello world; redis-cli -a "$REDIS_PASSWORD" get hello'

# 删掉 Pod，看 K3s 自动重建，且数据还在（PVC 还在）
kubectl delete pod redis-0 -n sc-demo
# 重建后重试上面的 get hello，应仍返回 world
```

## 与 sc-user 的关键差异（本课重点）
| 维度 | sc-user | Redis |
|---|---|---|
| 资源类型 | Deployment | **StatefulSet** |
| Pod 名 | 随机 hash（如 `service-user-7d9c-abc12`） | **固定 `redis-0`** |
| 存储 | 无（无状态） | **PVC 持久盘 `/data`**，重启不丢数据 |
| 扩副本 | `scale` 随便加 | 有状态，加副本 ≠ 自动主从，需额外编排 |

## 下一步（可选）
- 想看 PVC 真生效：删 Pod 后 `get hello` 仍返回 `world` 即证明持久化 OK。
- 想进阶「1主1从」：把 `replicas` 改 2，再给从库加 `redis-server --replicaof redis-0.redis ...`（涉及 Headless Service，到时候再说）。
- 想进阶「哨兵」：1主1从3哨兵，配置和发现逻辑更重，放最后再碰。
