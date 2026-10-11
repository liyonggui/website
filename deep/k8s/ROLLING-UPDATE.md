---
title: "service-user 滚动更新练习（K8s 核心概念 · B）"
summary: "目标：认识 K8s 的滚动更新（Rolling Update）与回滚，理解\"零停机发布\"是怎么发生的。 > 当前状态：sc-demo 命名空间下，service-user Depl…"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
category: deep
series: "k8s"
slug: "ROLLING-UPDATE"
reading_time: 13
featured: false
draft: false
---
# service-user 滚动更新练习（K8s 核心概念 · B）

> 目标：认识 K8s 的**滚动更新（Rolling Update）**与**回滚**，理解"零停机发布"是怎么发生的。
> 当前状态：`sc-demo` 命名空间下，`service-user` Deployment 副本 2，镜像 `sc-user:0.1.0`，Service 为 ClusterIP:8081。
> 所有 `kubectl` 命令都在 **WSL 终端**里执行（Windows 侧没有 kubectl）。

---

## 一、滚动更新是什么

Deployment 发布新版本时，不会"先删旧的再建新的"（那样会有停机窗口），而是：

1. 新建一个 **新 ReplicaSet**（用新 Pod 模板）
2. 新 RS **逐个**拉起新 Pod，同时旧 RS **逐个**退出旧 Pod
3. 任意时刻都有 `replicas` 个 Pod 在对外服务 → **零停机**
4. 全部替换完成后，旧 RS 缩容到 0，新 RS 接管

`kubectl rollout` 系列命令就是用来观察、暂停、回滚这条流水线的。

---

## 二、路径一（推荐 · 零构建 · 立刻能玩）

**思路**：用 `kubectl set env` 给 Pod 模板加一个环境变量 `APP_VERSION=0.2.0`，这会改变 Pod 模板的 hash → 触发一次真实的滚动更新。**不需要重新构建镜像**。

> 注：当前 `whoami` 接口只返回 Pod 名（不读 `APP_VERSION`），所以这条路径用来观察"滚动过程"和"零停机"，验证靠 Pod 名新后缀 + `rollout status`。若想看到版本号文字变化，走路径二。

### 1. 触发滚动更新
```bash
kubectl set env deployment/service-user -n sc-demo APP_VERSION=0.2.0
```

### 2. 实时看流水线状态（会一直挂到完成）
```bash
kubectl rollout status deployment/service-user -n sc-demo
```
正常会逐行打印 `Waiting for rollout...` 直到 `deployment "service-user" successfully rolled out`。

### 3. 另开一个窗口看 Pod 新旧交替（Ctrl+C 退出）
```bash
kubectl get pods -n sc-demo -w
```
你会看到：新后缀的 Pod 进入 `Running`，同时旧后缀的 Pod 变成 `Terminating` → `Completed`/`消失`。

### 4. 零停机验证（关键）
保持服务一直被访问，证明更新期间请求不中断。在 WSL 里跑集群内 curltest：
```bash
kubectl run curltest --rm -it --image=curlimages/curl -n sc-demo -- \
  sh -c 'for i in $(seq 1 60); do curl -s -m 2 http://service-user:8081/user/whoami; echo; sleep 0.3; done' | sort | uniq -c
```
如果输出里**同时出现新旧两种 Pod 名**（过渡期），且**没有请求失败**，就说明滚动更新确实零停机。

### 5. 看发布历史
```bash
kubectl rollout history deployment/service-user -n sc-demo
```
会列出 `REVISION 1`（原模板）、`REVISION 2`（加了 APP_VERSION 的模板）。

### 6. 回滚（回到上一版）
```bash
kubectl rollout undo deployment/service-user -n sc-demo
# 或回滚到指定 revision：
kubectl rollout undo deployment/service-user -n sc-demo --to-revision=1
```
回滚本身也是一次滚动更新（新 RS 缩、旧 RS 扩），同样零停机。

### 7. 可选：暂停 / 继续（金丝雀发布雏形）
```bash
kubectl rollout pause deployment/service-user -n sc-demo     # 更新到一半先卡住
kubectl rollout resume deployment/service-user -n sc-demo    # 继续完成
```

---

## 三、路径二（真换镜像版本 · 更贴近实战）

这条路径会真正换一个镜像 tag，你能看到 `/user/whoami` 返回的版本号从 `0.1.0` 变 `0.2.0`。

### 1. 改源码，让 whoami 带上版本（读环境变量）
在 `UserController.whoami()` 里追加版本返回，例如：
```java
@Value("${APP_VERSION:0.1.0}")
private String appVersion;

@GetMapping("/whoami")
public String whoami() {
    String hostname = System.getenv("HOSTNAME");
    if (hostname == null) { /* 原有兜底 */ }
    return hostname + " | v=" + appVersion;
}
```
（这一步需要改代码重新打包，可让我帮你改，你再 build。）

### 2. 打包 + 构建镜像（WSL 内）
```bash
cd /mnt/d/high-concurrency-projects/k3s-project/sc-user
mvn -DskipTests package
docker build -t sc-user:0.2.0 .
```

### 3. 导入 K3s（单节点用 containerd，docker 构建的镜像默认看不到）
```bash
docker save sc-user:0.2.0 > /tmp/sc-user-0.2.0.tar
sudo k3s ctr images import /tmp/sc-user-0.2.0.tar
```
> 若你的 K3s 是以 `--docker` 运行时安装的，则无需 import，docker 镜像直接可用。

### 4. 更新镜像（触发滚动更新）
```bash
kubectl set image deployment/service-user -n sc-demo service-user=sc-user:0.2.0
kubectl rollout status deployment/service-user -n sc-demo
```

### 5. 验证版本号变化
```bash
kubectl run curltest --rm -it --image=curlimages/curl -n sc-demo -- \
  sh -c 'for i in $(seq 1 10); do curl -s http://service-user:8081/user/whoami; echo; done'
```
应看到全部返回 `... | v=0.2.0`。

### 6. 回滚（回到 0.1.0 镜像）
```bash
kubectl rollout undo deployment/service-user -n sc-demo
```

---

## 四、关键命令速查

| 目的 | 命令 |
|---|---|
| 触发滚动更新（改 env） | `kubectl set env deployment/service-user -n sc-demo KEY=VALUE` |
| 触发滚动更新（改镜像） | `kubectl set image deployment/service-user -n sc-demo service-user=sc-user:0.2.0` |
| 看流水线状态 | `kubectl rollout status deployment/service-user -n sc-demo` |
| 实时看 Pod 变化 | `kubectl get pods -n sc-demo -w` |
| 发布历史 | `kubectl rollout history deployment/service-user -n sc-demo` |
| 回滚到上一版 | `kubectl rollout undo deployment/service-user -n sc-demo` |
| 回滚到指定版 | `kubectl rollout undo ... --to-revision=N` |
| 暂停 / 继续 | `kubectl rollout pause/resume deployment/service-user -n sc-demo` |
| 描述某次滚动事件 | `kubectl describe deployment service-user -n sc-demo` |

---

## 五、WSL2 + K3s 网络坑提醒（沿用之前结论）

- **NodePort 外部访问不通**：WSL2 下 kube-proxy 的 NodePort 没暴露到节点端口，`curl localhost:<NodePort>` 会"积极拒绝"。所以负载均衡 / 零停机验证一律用**集群内临时 Pod（curltest 访问 `service-user:8081`）**，走 ClusterIP，绕开 NodePort 坑。
- **Port Forward 是单 Pod 隧道**：Lens 的 Forward 按钮底层是 `kubectl port-forward`，直连某 1 个 Pod、绕过了 Service 负载均衡层，只能用来"通不通"，不能用来验证负载均衡/零停机分布。

---

## 附录：声明式基线（已固化为三件套）

`sc-user/` 目录现在包含 service-user 的**完整可重建包**：

| 文件 | 作用 | 来源 |
|---|---|---|
| `deploy.yaml` | Deployment 基线：replicas 2、RollingUpdate 策略（maxSurge 1 / maxUnavailable 0）、startup/readiness/liveness 三探针、securityContext（runAsNonRoot + readOnlyRootFilesystem + drop ALL）、resources 限制、preStop、envFrom 引用下面两个对象 | 导出自 live，清自动字段 |
| `user-env.yaml` | ConfigMap `user-env`：注入 MYSQL_HOST(192.168.0.104)/PORT(3306)/DB(user_db)/USER(root) | 导出自 live，清自动字段 |
| `user-secret.yaml` | Secret `user-secret`：注入 MYSQL_PASSWORD（base64，含真实密码） | 导出自 live，清自动字段 |

> 三份都是 `kubectl get ... -o yaml` 导出后，清理掉 `status` / `resourceVersion` / `uid` / `creationTimestamp` / `last-applied-configuration` 自动字段得到的干净基线。

**重建顺序**（WSL）：
```bash
cd /mnt/d/high-concurrency-projects/k3s-project/sc-user
kubectl apply -f user-env.yaml      # 1. 先建 ConfigMap
kubectl apply -f user-secret.yaml   # 2. 再建 Secret
kubectl apply -f deploy.yaml        # 3. 建 Deployment（依赖前两个）
```

**统一运维（消除 apply / 命令式 set / rollout undo 混管的 warning 正路）**：
- 改配置 → 改对应 yaml → `kubectl apply -f <文件>`
- 看历史 / 回滚 → `kubectl rollout history` / `kubectl rollout undo`
- 始终用 apply 管理，`last-applied-configuration` 与 live 自动对齐，那个 warning 不再出现

⚠️ `user-secret.yaml` 的 `data` 为 base64（含 mysql 真实密码 `Root@123456`）。本地留存无妨，但**切勿提交到公开仓库**。

> 想连 Service 也固化（当前是命令行建的 ClusterIP:8081，未落盘）：`kubectl get svc service-user -n sc-demo -o yaml > service.yaml`，同样清理自动字段即可。
