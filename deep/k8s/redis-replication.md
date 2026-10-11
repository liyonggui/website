# Redis 1主1从（StatefulSet 主从复制）

在「单实例」基础上加主从，目的不是高可用（没有哨兵就不自动故障转移），而是理解
**StatefulSet + Headless Service 如何给每个 Pod 固定身份和稳定 DNS**，以及 Redis 主从复制本身。

## 这次改了什么（相对单实例）
| 项 | 单实例 | 1主1从 |
|---|---|---|
| `replicas` | 1 | **2**（redis-0 主 / redis-1 从） |
| Service | ClusterIP | **Headless（clusterIP: None）** |
| 启动命令 | 固定 redis-server | **按 Pod 序号判断**：0 号主，>0 号加 `--replicaof redis-0.redis 6379` |
| 从库额外参数 | — | `--masterauth "$REDIS_PASSWORD"`（主库有密码，从库必须带认证） |

## 部署 / 更新（WSL）
单实例已经在跑的话，直接重新 apply 即可，K3s 会滚动把 redis-0 重建成主、新建 redis-1 为从：
```bash
kubectl apply -f redis-secret.yaml -f redis-statefulset.yaml -f redis-service.yaml -n sc-demo
```

## 验证主从
```bash
# 两个 Pod 都应 Running（注意名字固定 redis-0 / redis-1）
kubectl get pods -n sc-demo -l app=redis

# 看主库角色与复制状态：role:master，connected_slaves:1
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" info replication'

# 看从库角色：role:slave，master_host:redis-0.redis，master_link_status:up
kubectl exec -it redis-1 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" info replication'
```

## 验证数据同步
```bash
# 在主库写
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" set foo bar'

# 在从库读（应返回 bar，证明复制生效）
kubectl exec -it redis-1 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" get foo'

# 注意：往从库写会报错 "(error) READONLY You can't write against a read only replica."
#       这是 Redis 从库默认只读，符合预期。
```

## 验证持久化（主库）
```bash
kubectl delete pod redis-0 -n sc-demo   # 删主，K3s 重建后仍读 PVC，数据还在
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" get foo'   # 仍返回 bar
```

## 重要认知（本课重点）
- **没有哨兵 = 不会自动故障转移**：如果 redis-0（主）真挂了，redis-1 仍是只读从库，不会自动升主，写会失败。手动升主需要 `replicaof no one`（哨兵章节再讲）。
- **稳定 DNS 来自 Headless Service**：`redis-0.redis` / `redis-1.redis` 能解析，是因为 Service 是 Headless 且 StatefulSet 的 `serviceName` 指向它。
- **客户端区分主从**：无哨兵时，写连 `redis-0.redis`、读连 `redis-1.redis`；直接连 `redis:6379`（轮询）可能打到只读从库。

## 下一步
- 想要「主挂了自动切换」→ 上 **哨兵（1主1从3哨兵）**：哨兵监控主从、自动选主，客户端经哨兵发现主库。配置和发现逻辑更重，放最后。
- 想看 Pod 固定身份价值 → 把 `replicas` 改成 3，会出 redis-2（也是从库，replicaof 指向 redis-0）。
