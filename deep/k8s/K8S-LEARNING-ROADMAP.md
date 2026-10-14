---
title: "K8s 实战学习路线图（已学 + 待学）"
summary: "目标定位：认识 K8s，不是验证功能。本文件是学习进度的总账，记录已亲手跑通的能力、对应的清单文件、关键认知，以及尚未涉及但实战常用的功能与建议路线。 > > 最后更新：2026-…"
date: 2026-09-04
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
category: deep
series: "k8s"
slug: "K8S-LEARNING-ROADMAP"
reading_time: 15
featured: false
draft: false
---
# K8s 实战学习路线图（已学 + 待学）

> 目标定位：**认识 K8s，不是验证功能**。本文件是学习进度的总账，记录已亲手跑通的能力、对应的清单文件、关键认知，以及尚未涉及但实战常用的功能与建议路线。
>
> 最后更新：2026-10-08

---

## 〇、本机环境基线（重要事实，先记死）

| 项 | 值 |
|---|---|
| 集群形态 | 单节点 **K3s**（all-in-one） |
| 节点 | `lyg`，Roles=control-plane，Version=`v1.36.5+k3s1` |
| 命名空间 | `sc-demo`（所有实战资源都在这里） |
| 可视化 | **Lens**（Windows 侧 import `D:\k3s.kubeconfig`） |
| 存储 | K3s 自带 `local-path` 存储类，PVC 自动供给 |
| Metrics Server | 自带（HPA 依赖，已验证可采数） |
| Ingress Controller | **Traefik**，但安装时被 `--disable traefik` 关掉；已移除该禁用项并 `systemctl restart k3s` 重新部署成功 |
| Traefik Service | LoadBalancer，80:31189/TCP、443:31828/TCP，ClusterIP 10.43.252.79（EXTERNAL-IP `<pending>`，因 `servicelb` 被禁用） |
| IngressClass | `traefik (default)`（默认类，Ingress 无需写 `ingressClassName`） |
| **WSL2 网络坑** | WSL2+K3s 的 **NodePort 外部访问不通**（Windows 侧 `10061` 拒绝）；集群**内部** ClusterIP/Service 名访问正常。**验证入口流量一律走集群内临时 Pod**，别用 NodePort 对外 |

### 已部署的实战资源（sc-demo 命名空间）

| 资源 | 类型 | 关键配置 |
|---|---|---|
| `service-user` | Deployment + ClusterIP Service | 2 副本默认、image `sc-user:0.1.0`、端口 8081、`envFrom` 引用 ConfigMap `user-env` + Secret `user-secret`、三探针、securityContext、resources 限制 |
| MySQL | 外置（docker-compose） | `192.168.0.104:3306`，库 `user_db`，密码 `Root@123456` |
| `redis` (redis-0/redis-1) | StatefulSet + Headless Service | 1主1从；当前**临时态 redis-1 主 / redis-0 从**（内存级 masterauth 修复，待固化）；密码 `Redis@123456` |
| `redis-sentinel` ×3 | Deployment + Service | 监控主库、自动故障转移，quorum=2，端口 26379 |
| `service-user` HPA | HorizontalPodAutoscaler | CPU 50% 目标，min 2 / max 10 |
| `service-user` Ingress | Ingress | host `service-user.local`，path `/` → `service-user:8081` |

---

## 一、已掌握（均亲手跑通验证）

### 1. 无状态服务 —— `service-user`
- **多副本 + Service 负载均衡**：`curltest` 集群内访问 `service-user:8081/user/whoami`，请求均匀散到各 Pod（验证过 5 副本、2 副本两种）
- **故障自愈**：删 Pod → Deployment 自动补齐，副本数不变
- **scale**：手动 `kubectl scale` 调整副本
- **滚动更新 + 回滚**：`kubectl set env` / `set image` 触发，配合 `rollout status/history/undo`
- **声明式固化**：`deploy.yaml` + `user-env.yaml` + `user-secret.yaml` 三件套统一 `apply`，消除 `last-applied-configuration` 注解漂移
- 对应文件：`sc-user/deploy.yaml`、`sc-user/user-env.yaml`、`sc-user/user-secret.yaml`、`sc-user/ROLLING-UPDATE.md`

### 2. 有状态中间件 —— `redis`（三档全跑通）
- **单实例**：StatefulSet + PVC 持久化（删 Pod 后数据不丢，验证 `get hello` 仍返回 `world`）
- **1主1从**：replicas=2 + Headless Service 稳定 DNS（`redis-0.redis`/`redis-1.redis`）+ `--replicaof` 主从复制
- **1主1从3哨兵**：自动故障转移（删主库 → 从库升主 → 旧主重建后被降级归队）
- **踩坑并修复**：主库缺 `--masterauth`，被降级为从后无法向有密码新主认证 → 已在 `redis-statefulset.yaml` 主库分支补上（**待固化**）
- 对应文件：`sc-user/redis-secret.yaml`、`redis-statefulset.yaml`、`redis-service.yaml`、`redis-README.md`、`redis-replication.md`、`redis-sentinel.yaml`、`redis-sentinel-service.yaml`、`redis-sentinel.md`

### 3. 配置管理 —— ConfigMap / Secret
- `ConfigMap user-env` 注入 `MYSQL_*` 环境变量
- `Secret user-secret` 注入密码（不进明文）
- 通过 `envFrom` 整体引用（非逐条 env）
- 认知：改 envFrom 注入的配置**需重建 Pod 才生效**（不热更新）

### 4. 入口流量 —— Ingress（Traefik）
- 七层路由：`Host: service-user.local` + path `/` → `service-user:8081`
- 验证：`curl -H "Host: service-user.local" http://traefik.kube-system.svc.cluster.local:80/user/whoami` 返回 Pod 主机名（HTTP 200）
- **关键认知**：Ingress 只是意图声明，**必须配合 Ingress Controller 才生效**；本机 Traefik 曾被禁用，移除 `--disable traefik` 重启后才起来
- 对应文件：`sc-user/service-user-ingress.yaml`、`ingress-hpa.md`

### 5. 弹性伸缩 —— HPA
- 按 CPU 利用率自动扩缩：压测下 REPLICAS 阶梯 `2→4→8→10`（触顶 max），停压测后约 5 分钟稳定窗口后缩回 2
- **关键认知**：HPA 盯「实际利用率 vs 目标」，有 5 分钟缩容稳定窗口防抖动
- 对应文件：`sc-user/service-user-hpa.yaml`

### 6. 基础安全 / 探针 / 资源
- 三探针：startup / readiness / liveness
- securityContext：`runAsNonRoot` / `readOnlyRootFilesystem` / `drop ALL` / seccomp
- resources：`requests`(100m/256Mi) + `limits`(512Mi)

---

## 二、未学（实战常用，按优先级三档）

### ★ 第一档：最该先补（和已学强相关，性价比最高）

| 功能 | 是什么 | 与已学的衔接 |
|---|---|---|
| **Job / CronJob** | 一次性批处理 / 定时任务（备份、跑脚本） | 之前全是长驻 Pod，这是「跑完即退」的另一种生命周期 |
| **DaemonSet** | 每个节点自动跑一个副本（日志采集、监控 agent、网络插件） | 与 Deployment「按副本数」对照，理解调度单位差异 |
| **ConfigMap 热更新** | 改配置不重启 Pod（挂卷自动同步） | 你现在是 envFrom 注入，**不热更新**；这是生产常见坑（注意 subPath 不热更新） |
| **ServiceAccount + RBAC** | Pod 以什么身份访问 API；谁能动哪些资源 | Traefik 自带 RBAC，sc-user 还没配 SA；多租户/安全必学 |
| **NetworkPolicy** | 命名空间/Pod 间网络隔离（默认全通） | k3s manifests 自带一份 netpol，正好对照 |
| **发布策略：蓝绿 / 金丝雀** | 零停机切换、按流量比例灰度 | 你做过滚动更新，这是进阶形态（Traefik 就能做 canary） |
| **HPA 多指标** | 按 内存 / QPS / 自定义指标扩缩 | 目前只用 CPU，生产常看多指标 |

### ★★ 第二档：运维进阶（集群跑稳必碰）

| 功能 | 是什么 |
|---|---|
| **监控三件套** | Prometheus + Grafana + kube-state-metrics / node-exporter（CPU、Pod、节点全貌） |
| **集中日志** | Loki / EFK（多 Pod 日志汇总检索，告别逐个 `kubectl logs`） |
| **Helm** | K8s 的「包管理器」，模板化部署（实战部署中间件几乎都用它） |
| **节点调度** | nodeSelector / 亲和性 affinity / 污点 taints+容忍 tolerations / 拓扑打散 topologySpread（控制 Pod 落在哪） |
| **ResourceQuota / LimitRange** | 命名空间级资源上限（防某业务把集群吃爆） |
| **VPA / Cluster Autoscaler** | 垂直调容器规格 / 按负载加节点（HPA 横扩，这是纵扩和扩节点） |
| **kubectl debug / drain / cordon** | 临时排障容器、节点维护下线 |

### ★★★ 第三档：高阶（走向生产级平台）

| 功能 | 是什么 |
|---|---|
| **GitOps（ArgoCD / Flux）** | 清单存 Git 自动同步部署，替代手工 `kubectl apply` |
| **Kustomize** | 多环境（dev/test/prod）配置叠加，不重复写 YAML |
| **Operator 模式 + CRD** | 用代码管理复杂有状态应用（你那个「哨兵脚本硬编码」的优雅替代 = Redis Operator） |
| **Service Mesh（Istio/Linkerd）** | 流量治理、mTLS、细粒度灰度，比 Ingress 更底层 |
| **cert-manager** | 自动签发/续期 TLS 证书（配合 Ingress 上 HTTPS） |
| **多集群 / 联邦** | 跨可用区、跨云容灾 |

---

## 三、建议学习路线

> 贴合「认识 K8s」节奏，从「已学概念的对照/补完」入手，再上平台级能力。

1. **第一档轻量补完**（半天内可跑通，全是对照已学）：
   - `Job/CronJob` + `DaemonSet`（生命周期 & 调度单位对照）
   - `ConfigMap 热更新`（直接补 sc-user 配置管理痛点）
   - `ServiceAccount + RBAC`（给 sc-user 配最小权限 SA）
2. **可观测性 + 包管理**：`Helm` 部署 + `Prometheus/Grafana` 把集群看板建起来
3. **发布进阶**：`蓝绿 / 金丝雀`（用 Traefik 做 canary，复用已通的 Ingress）
4. **平台级**：`GitOps(ArgoCD)` → `Operator` → `Service Mesh`

### 遗留项与后续计划
- **[未固化]** redis 主库 masterauth：当前集群为临时修复态，需 `kubectl apply -f redis-statefulset.yaml -n sc-demo` + 重建两 Pod，拓扑回到 redis-0 主且主库自带 masterauth
- **[可选]** sc-user 真正接入 redis 做缓存联调（最有实战感的一步）

---

## 附录：常用命令速查

```bash
# 负载均衡验证（集群内，绕开 WSL2 NodePort 坑）
kubectl run curltest --rm -it --image=curlimages/curl -n sc-demo -- \
  sh -c 'for i in $(seq 1 20); do curl -s http://service-user:8081/user/whoami; echo; done' | sort | uniq -c

# HPA 观察
kubectl get hpa -n sc-demo -w

# Ingress 七层路由验证（集群内）
kubectl run curltest-ing --image=curlimages/curl -n sc-demo -- \
  sh -c 'curl -v -m 5 -H "Host: service-user.local" http://traefik.kube-system.svc.cluster.local:80/user/whoami'
kubectl logs curltest-ing -n sc-demo

# Redis 主从 / 哨兵状态
kubectl exec -it redis-0 -n sc-demo -- sh -c 'redis-cli -a "$REDIS_PASSWORD" info replication'
kubectl exec -it $(kubectl get pods -n sc-demo -l app=redis-sentinel -o jsonpath='{.items[0].metadata.name}') -n sc-demo -- \
  sh -c 'redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster'

# 声明式三件套重建顺序
kubectl apply -f user-env.yaml -f user-secret.yaml -f deploy.yaml -n sc-demo
```
