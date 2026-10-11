---
title: "Compose → K8s 概念对照（给已经会 docker-compose 的人）"
summary: "如果你只记一句话：Compose 描述的是\"一台机器上如何起一组容器\"，K8s 描述的是\"一个集群里期望状态长什么样\"。 前者的单位是容器，后者的单位是\"契约 + 调和循环\"。这个…"
date: 2026-08-30
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
category: deep
series: "k8s"
slug: "compose-vs-k8s"
reading_time: 9
featured: false
draft: false
---
# Compose → K8s 概念对照（给已经会 docker-compose 的人）

如果你只记一句话：**Compose 描述的是"一台机器上如何起一组容器"，K8s 描述的是"一个集群里期望状态长什么样"。**
前者的单位是容器，后者的单位是"契约 + 调和循环"。这个差别会让下面每一条对照都有隐性折扣。

## 字段映射

| Compose | K8s | 折扣在哪（最容易误判的一点） |
|---|---|---|
| `services.web.image` | `Deployment.spec.template.spec.containers[].image` | 一样 |
| `deploy-resources.replicas` | `Deployment.spec.replicas` | Compose 的 replicas 不解决流量分发，K8s 有 Service 兜底 |
| `ports: "8080:80"` | **没有对应物**，要 Service + Ingress（或 NodePort） | 集群内 Pod 之间从来不需要映射端口，直接走 Pod IP + 容器端口 |
| `environment` / `env_file` | `ConfigMap`（env 或挂载）+ `Secret` | ConfigMap 改了**不会**自动重启 Pod，这是最经典的一次"我改了配置怎么没生效" |
| `volumes: ./src:/app` | `volumeMounts` + `emptyDir` / `hostPath` / `PVC` | bind mount 宿主机目录在 K8s 里是 hostPath，属于要被审的写法 |
| `volumes: named` | `PersistentVolumeClaim`（Deployment 共享 / StatefulSet 按副本独占） | 语义跃迁最大的一项，见第 3 节 |
| `depends_on` | **没有对应物**。K8s 假设依赖是"最终可用"的，用 readinessProbe + 应用侧重试表达 | Compose 能保证单机启动顺序，K8s 里 Pod 分布在不同节点，顺序无意义 |
| `networks` / 同一 compose 文件内互访 | `Service` DNS：`<name>.<namespace>.svc.cluster.local` | Compose 靠服务名，K8s 靠 Service 名 + namespace；跨 namespace 要带全名 |
| `healthcheck` | 三根探针：`startupProbe` / `readinessProbe` / `livenessProbe` | Compose 只有一个 health status；K8s 把"能不能收流量"和"要不要重启"拆成两件独立的事 |
| `restart: always` | 控制器 + kubelet 的隐式行为 | 不需要写，但也意味着你没法像 Compose 那样"就地改 restart 策略" |
| `expose` / `internal` | `NetworkPolicy` | Compose 的隔离靠拆 network；K8s 默认全通，隔离必须显式写策略 |
| `build:` | **不该出现在 K8s 清单里**。构建在 CI 里做，产出镜像，清单只引用 digest | 见到 K8s YAML 里想"现场构建"就是走偏了 |
| `secrets` | `Secret` + CSI Secret Provider / External Secrets | 同上一节 |
| `deploy.update_config` | `Deployment.spec.strategy.rollingUpdate` | 语义接近 |
| `deploy.placement.constraints` | `nodeSelector` / `affinity` / `tolerations` | K8s 表达能力强得多，也因此更容易写出调度不过去的清单 |
| Compose Profile | K8s 没有：用不同 overlay（Kustomize/Helm values）表达 | 呼应"环境差异用 overlay 切，不是换发行版" |

## 三件真正需要重建心智的事

**1. 网络：从"端口映射"变成"名字解析"。**
Compose 里你习惯 `8080:80`，因为在 Docker 眼里容器端口要暴露到宿主机。K8s 里整个集群是一张扁平网络，Pod IP 集群内可路由，Service 给一个不变的名字和虚拟 IP。所以迁移时最常见的错误是：Service 的 `targetPort` 和容器实际监听端口对不上（尤其是 Spring Boot 改了 `server.port` 但清单里还是 8080），表现是 Service 存在、Pod 健康，但连上就是不通。

**2. 存储：从"宿主机上一个目录"变成"一个会被调度的资源"。**
Compose 的 named volume 就是 `/var/lib/docker/volumes/xxx`，永远在那台机器上。K8s 的 PVC 是一份**申请**：谁来 provision、Provisioner 能不能跨节点挂载、访问模式是 RWO 还是 RWX，会直接决定 Pod 能不能被调度。`ReadWriteOnce` 在 local-path 上意味着"这个 Pod 被钉死在那台节点"；换成 RWX 的共享存储（NFS/CephFS/云盘多_attach_）才允许漂移。这一层想清楚了，StatefulSet 为什么不能换成 Deployment 就自动有答案了。

**3. 生命周期：从"起完就算成功"变成"永远在调和"。**
Compose `up -d` 之后容器在跑就算完。K8s 里"在跑"只是中间态：探针在反复判定，controller 在反复对比副本数，节点一挂 Pod 就在别处重建。所以你的应用必须能回答三个问题：怎么被摘流量（readiness）、怎么被要求下线（SIGTERM + 优雅停机）、怎么被杀掉（Graceful Shutdown 超时）。**答不出第一个和第三个，滚动更新就必然有一小撮 502**——这就是 `90-fault-drills.sh` 演练 8 演示的那个竞态。

## 迁移一个 Spring Boot 单体（比如 BladeX）的最小清单顺序

1. 镜像：Dockerfile 里 `USER 10001` 非 root，`EXPOSE 8080`，jar 只读层。
2. 探针：引入 `spring-boot-starter-actuator`，`management.endpoint.health.probes.enabled=true`，得到 `/actuator/health/liveness` 与 `/readiness`（Spring Boot 2.3+ 自带，别自己造）。
3. JVM：`-XX:MaxRAMPercentage`，让堆跟着容器 limit 走。
4. 配置：`application-prod.yml` 的参数全走 env（`SPRING_APPLICATION_JSON` 或逐个 `SPRING_DATASOURCE_URL`），密钥走 Secret 挂载文件而不是 env。**"env 会进 `kubectl describe`"这句要说准，本机实测两种写法不一样**（假值 `NOT_A_REAL_TOKEN_000`，一次性 Pod 取证后即删）：
   - 清单里写死的字面量 env：`describe` 直接打值 —— `LITERAL_IN_MANIFEST:  NOT_A_REAL_TOKEN_000`。任何有权限看 Pod 的人都看得到，还跟着 Git 进仓库。
   - `valueFrom.secretKeyRef` 的 env：`describe` 只打引用不打值 —— `FROM_SECRET_KEYREF:  <set to the key 'token' in secret 'env-probe-secret'>`（我把整份 describe 输出对该串计数，结果为 1，就是上面那条字面量）。
   - 但**别因此就改用 secretKeyRef 收工**：`kubectl exec ... -- env` 里两条都是明文（实测），进程的日志、crash dump、`/proc/1/environ` 一样带出去。所以推荐挂载成文件（`describe` 里只出现 `Mounts: /secrets from s (ro)` 这一行路径），应用按文件读。
5. 有状态依赖先**不要**上 K8s：MySQL/KingbaseES 继续用外部实例，集群里只跑无状态应用。这一步克制能省掉 80% 的事故面。
6. 灰度：先 `replicas: 1` 跑通，再 2，再加 HPA。一次只引入一个变量。
