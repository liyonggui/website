---
title: "k3s 实战：一个可跑通、可炸给你看的单机集群"
summary: "这份东西不是\"K8s 入门教程\"，而是把前面几轮聊的结论落成能在你机器上跑、并且每个关键结论都有对应现场证据的一套材料。目标只有一个：让你亲手看到那些\"背下来但没感觉\"的机制——C…"
date: 2026-09-06
author: "小番茄程序猿"
tags:
  - K8s
  - 云原生
category: deep
series: "k8s"
slug: "README"
reading_time: 31
featured: false
draft: false
---
# k3s 实战：一个可跑通、可炸给你看的单机集群

这份东西不是"K8s 入门教程"，而是把前面几轮聊的结论落成**能在你机器上跑、并且每个关键结论都有对应现场证据**的一套材料。目标只有一个：让你亲手看到那些"背下来但没感觉"的机制——ConfigMap 不热更新、liveness 探针写错导致全站重启、requests 是调度门槛、NetworkPolicy 默认全通、local-path 的数据其实躺在本地目录里、滚动更新为什么会有一小撮 502。

看完你应该能回答："我的 Spring Boot 单体上 K8s，第一天会踩什么坑。"

## 目录结构

```
k3s-project/
├── README.md                      本文件，按顺序读
├── compose-vs-k8s.md              会 docker-compose 的人专用对照表（建议先看这个）
├── scripts/
│   ├── 00-install-k3s.sh          在 WSL2 里装单机 k3s
│   ├── 01-verify.sh               底座自检（5 项，装完必跑）
│   ├── 90-fault-drills.sh         8 个故障演练，每个独立可跑、自带收尾
│   └── 99-cleanup.sh              清工作负载 / 连 k3s 一起卸
└── manifests/
    ├── 00-namespace.yaml
    ├── 10-configmap.yaml          env 型 + file 型两种用法
    ├── 11-secret.yaml             含"为什么这是反面示范"
    ├── 20-deployment.yaml         ★ 核心：探针/优雅下线/资源/安全上下文全在里面
    ├── 21-service.yaml
    ├── 30-ingress.yaml            与生产对齐的写法示范
    ├── 40-redis-statefulset.yaml  有状态示范（含 local-path 的证据链）
    ├── 50-netpol-redis-only.yaml
    └── 51-netpol-default-deny.yaml  不进任何脚本，手工 apply/删；文件头写了本机实测的三条结论
```

示例里跑的是 nginx（当"替身镜像"）+ Redis，因为这两个不用你构建任何东西就能验证完整链路。`20-deployment.yaml` 末尾列了换成你自己的 Spring Boot 镜像时要改的 5 处，字段其余照抄。

## 前置：WSL2 里的 systemd

k3s 是 Linux 程序，Windows 上必须放进 WSL2。它依赖 systemd，所以先确认：

```bash
# /etc/wsl.conf（Windows 侧 C:\Users\你\etc\wsl.conf 也能写，等价）
[boot]
systemd=true
```

改完在 **Windows PowerShell** 执行 `wsl --shutdown` 再重进，然后验证——这一步不能跳，WSL 的 systemd 没起来是 k3s 安装失败的第一大原因：

```bash
systemctl is-system-running     # 期望 running 或 degraded；输出 offline 就是没开 systemd
```

`degraded` 在 WSL 里很常见（一些单元本来就不该跑），只要 `systemctl is-active k3s` 后面能变 active 就不用管。

## 装 k3s

进 WSL，**先把材料从 Windows 盘复制进 Linux 文件系统**再动手。`/mnt/d/...`（drvfs）和 `/mnt/c/...` 一样走 9P，小文件读写慢一个数量级，而且建议别在源仓库工作树里直接跑——在 Linux 侧留一份干净副本：

```bash
mkdir -p ~/k3s-project && cp -r /mnt/d/<k3s-project 源目录>/* ~/k3s-project/
cd ~/k3s-project/scripts
```

安装脚本里三个决策点，值得逐条看：

```bash
sudo -E MIRROR_ENDPOINT=https://<你的加速地址> bash 00-install-k3s.sh

# 本机（GitHub asset 不通）实际跑通的是这条：
sudo -E INSTALL_K3S_ARTIFACT_URL=https://rancher-mirror.rancher.cn/k3s \
       INSTALL_K3S_VERSION=v1.36.5-k3s1 \
       MIRROR_ENDPOINT=https://docker.1panel.live bash 00-install-k3s.sh
```

**动手前先量二进制下载源，别照抄老教程。** 下面三件事必须自己打一遍：

1. `INSTALL_K3S_MIRROR=cn` **已经失效**——现行官方 `install.sh` 全文没有这个变量（`grep -in mirror` 0 命中），设了会被静默忽略，你以为走了镜像、其实还在连 GitHub。替代的是 `INSTALL_K3S_ARTIFACT_URL`（二进制前缀）和 `INSTALL_K3S_CHANNEL_URL`（版本通道）。
2. `api.github.com` 通 **不等于** release asset 通。本机实测：`curl https://github.com/k3s-io/k3s/releases/download/<ver>/k3s` 90s 收到 0 字节，而 `INSTALL_K3S_ARTIFACT_URL=https://rancher-mirror.rancher.cn/k3s` 4s 拿到 79 MB，且 `sha256sum` 与官方 `sha256sum-amd64.txt` 里那条逐字符相同。
3. 走镜像时**必须把版本号按镜像上的目录名传**：`INSTALL_K3S_VERSION=v1.36.5-k3s1`（减号）。安装脚本第 385 行是 `VERSION_URLSAFE=$(printf '%s' "$VERSION_K3S" | sed 's/+/%2B/g')`——它把 `+` 编成 `%2B`，**不是**换成 `-`，而镜像那边存的是减号目录，所以用默认的 `v1.36.5+k3s1` 拼出来的 URL 直接 404（我第一次装就栽在这，日志停在 `[ERROR] Download failed`）。传减号是安全的：`VERSION_K3S` 只用于拼 URL 和打 info 日志，装完 `k3s kubectl get nodes` 里仍是 `v1.36.5+k3s1`。另外这个镜像上没有 airgap 包：`k3s-airgap-images-amd64.tar.zst` 虽然写在它的 `sha256sum-amd64.txt` 里，实际 GET 回来是 OSS 的 `NoSuchKey` 404——所以别指望"下 airgap 就绕开 docker.io"，加速地址还是得配好。

镜像加速地址也不能只看 `/v2/` 返 200：要带 `Accept` 打一个**真实镜像的 manifest**，200 且有 body 才算能用。实测对比——`docker.m.daocloud.io` 对 `nginx/manifests/alpine` 返 **401**（走不通），`docker.1panel.live` 与 `docker.xuanyuan.me` 返 **200 + 10 KB**；脚本默认值已换成前者。

`00-install-k3s.sh` 做的事：

1. `--disable servicelb --disable traefik`：把 Klipper LB 和 Traefik 关掉。理由就是上一轮的结论——本地用和生产不同的 ingress controller，等于把最容易漂移的那一层给藏起来了。
2. 写 `/etc/rancher/k3s/registries.yaml` 配 docker.io 加速。**这是国内装 k3s 的第一道墙**，不配的话 `01-verify.sh` 第 4 项会卡 30s 超时。改这个文件后必须 `systemctl restart k3s` 才生效。
3. `--write-kubeconfig-mode 644`：让 `/etc/rancher/k3s/k3s.yaml` 非 root 也能读，否则每个 kubectl 都要 sudo。脚本还会把 `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` 落到 `/etc/profile.d/k3s-kubectl.sh` **和** `~/.bashrc`（两处都要：非登录的交互式 shell 不读 profile.d），这样重开终端不用手动 export——排错表里那条 `connection refused localhost:6443` 就是漏了它。这是装完留在你 shell 里的全局配置，`99-cleanup.sh all` 会把 profile.d 那个文件删掉并从 `.bashrc` 里抹掉那一行。

装完确认服务在跑、节点 Ready：

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s kubectl get nodes -o wide      # STATUS 应为 Ready
```

## 底座自检：`scripts/01-verify.sh`

装完别急着写 YAML，先跑它。它检查的是 5 件后续所有问题都会回溯到的事：

| 检查项 | 不正常时的表现 | 为什么重要 |
|---|---|---|
| k3s 服务 + 节点 Ready | 服务 active 但节点 NotReady | 通常是 CNI（flannel）没起来，WSL 里多为内核模块缺失 |
| kube-system Pod 全 Running | `local-path-provisioner` CrashLoop | 它挂了不会报错，只会让你的 PVC **永远 Pending**（下一步 40 就是 PVC） |
| StorageClass 与 local-path 落盘目录 | `config.json` 读不到 | 让你亲眼看到"PV = 节点上一个目录"，这是理解有状态服务的关键 |
| 拉一个 busybox 镜像 | 卡 30s 超时 | 99% 是 mirror 没配或没重启 k3s，不修好后面全是假故障 |
| `k3s crictl ps` / `k3s ctr images ls` | — | 建立正确直觉：k3s 用内置 containerd，`docker ps` 永远是空的 |

第 4 项卡住时的定位顺序：`cat /etc/rancher/k3s/registries.yaml` 确认内容 → `systemctl restart k3s` → `journalctl -u k3s -n 50 | grep -i 'failed to pull\|registry'`。

## 按序 apply

```bash
cd ~/k3s-project
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
for f in manifests/00-namespace.yaml manifests/10-configmap.yaml manifests/11-secret.yaml \
         manifests/20-deployment.yaml manifests/21-service.yaml manifests/40-redis-statefulset.yaml; do
  echo ">>> $f"; k3s kubectl apply -f "$f"
done
k3s kubectl -n app-demo get pod -o wide -w      # 盯到 4 个 Running（3 web + 1 redis-0）
```

看到的效果和预期：

```
NAME                   READY   STATUS    RESTARTS   AGE
redis-0                1/1     Running   0          45s
web-6c8f9b7d4-abcde    1/1     Running   0          40s
web-6c8f9b7d4-fghij    1/1     Running   0          40s
web-6c8f9b7d4-klmno    1/1     Running   0          40s
```

注意 `redis-0` 这个名字——它和 `web-xxxxx` 的区别不是随机后缀，是身份。演练 1 会把这一点砸实。

再验证 Service 与 DNS：

```bash
k3s kubectl -n app-demo exec deploy/web -- sh -c 'for i in 1 2 3 4 5; do wget -qO- web.app-demo.svc.cluster.local; done'
# 期望：交替出现 "served by web-6c8f9b7d4-xxxx"，不同 Pod 名 → Service 在做负载均衡
# 这一步同时在验证：CoreDNS 正常、Service selector 命中、targetPort 对得上。三样有一样错这里就断。
```

## Ingress：故意不给你"开箱即用"

`30-ingress.yaml` 直接 apply 是**不会生效**的——因为安装时把 Traefik 关了，而集群里没有 `ingressClassName: nginx` 对应的 controller。这不是疏漏，是故意的：Ingress 资源本身只是"一条声明"，没有 controller 处理它就是一条静默的无效配置，**而且 `kubectl apply` 会返回 success**。这个体验比任何解释都值钱：生产上"我明明配了 Ingress 为什么不通"最常见的三类原因就是它（class 名打错、controller 没装、注解属于另一家用不了）。本机已把这段空跑实测过一遍，四条取证命令和它们的输出（`get ingressclass` = No resources found、apply 报 created、`ADDRESS` 空列、`.status` = `{"loadBalancer":{}}`、`Events: <none>`、`ss` 查不到 80 端口监听）写在 `manifests/30-ingress.yaml` 文件头，`delete -f` 同一份文件即可回滚。

想让它在本地真通，装和生产同一款 controller：

```bash
# 用 baremetal 版（Service 类型是 NodePort，适配单机）；版本号对齐你生产的 ingress-nginx 版本
k3s kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/baremetal/deploy.yaml
k3s kubectl -n ingress-nginx wait --for=condition=Ready pod -l app.kubernetes.io/component=controller --timeout=180s
k3s kubectl -n app-demo apply -f manifests/30-ingress.yaml
```

然后取节点 IP 和 NodePort 访问（**端口以实际 `get svc` 输出为准**）：

```bash
k3s kubectl -n ingress-nginx get svc                       # 记下 HTTP 那行的 3xxxx 端口
IP=$(hostname -I | awk '{print $1}')
curl -s -H "Host: app.local" http://$IP:30080/
k3s kubectl -n app-demo get ingress                         # ADDRESS 应该有值了
```

如果你生产用的是云厂商 ingress（ALB/CLB/TE），这一步就换成"确认它的 class 名和注解前缀"，本地永远装不出一模一样的行为——所以本示例只把**声明写对**，把 controller 差异明确留在"必须去 staging 验"那一栏里。

## 故障演练：这才是这套材料的主菜

`scripts/90-fault-drills.sh` 每个演练独立跑、自带收尾（改坏的字段会改回来）。**建议顺序跑完 1→8**，全程实测 **3 分 06 秒**（本机 2026-10-07 连跑一遍打的时间戳：1=17s、2=26s、3=77s、4=14s、5=16s、6=14s、7=8s、8=14s）。原话写的"大约 25 分钟"是把装集群和读 YAML 的时间一起估了，跑脚本本身远用不了这么久——大头全在演练 3 的 77s，那 55s 是**故意等出来的**：要等最大 `RESTARTS` 到 2 才采样，等短了就只能看到一次偶发重启，看不出"循环杀"。

```bash
cd ~/k3s-project/scripts
sudo bash 90-fault-drills.sh 1
```

| # | 演练 | 你会看到什么 | 对应那句"背过但没感觉"的话 |
|---|---|---|---|
| 1 | 删 Pod | web 换名字换 IP、Service 不动；redis-0 名字不变、PVC 的 AGE 比 Pod 老 | "StatefulSet 提供稳定的网络标识与存储身份" |
| 2 | 改 ConfigMap | Pod 里读到的是旧值，必须 `rollout restart` 才生效 | "ConfigMap 是运行时挂载，不进 pod hash" |
| 3 | liveness 路径写错 | 三个 Pod 开始无限重启，`RESTARTS` 往上跳（首次杀掉在 22~25s） | "liveness 只探进程自身存活，别带依赖" |
| 4 | 内存超限 | `Reason: OOMKilled`，日志里没有任何应用异常 | "容器内存是 cgroup 层杀的" |
| 5 | request 写成 32 核 | 直接 `Pending` + `Insufficient cpu`，不是变慢 | "requests 是调度依据，limits 是运行上限" |
| 6 | NetworkPolicy | 收紧前任意 Pod 能连 redis:6379；apply 后约 5s 内拒绝 | "不写策略 = 集群内全通" |
| 7 | 删 Pod vs 删 PVC | 删 Pod 数据在；目录就在 `/var/lib/rancher/k3s/storage/pvc-xxx` | "PV 是申请出来的资源，不是宿主机目录" |
| 8 | 观察 Terminating | Pod 停留约 5s（preStop），然后才收 SIGTERM | "摘流量与杀进程存在竞态" |

四个额外提醒：

- 演练 6 里"从 dbg Pod 连 redis"如果**收紧前后都通**，说明你的 CNI 根本没执行 NetworkPolicy。这不叫失败，这叫你躲过了一次生产事故——很多集群（vanilla flannel）就是这个状态，而所有人都以为有隔离。
- 演练 7 我只演示到"删 Pod 数据在 + 物理躺在本地目录"，**没有真删 PVC**。删完 `redis-0` 会永久 Pending（StatefulSet 不重建 PVC，要人手再造），单机场景下这个操作风险不高但收益也没有，你自己判断。
- 演练 3 和 8 区分度最高，因为它们的答案不能靠背，必须见过现象。
- 本机（k3s v1.36.5 + WSL2，2026-10-07 实跑 1→8）测出五条修正，换环境时按同样方法重验，别直接信：
  1. **演练 3 第一次跑是空跑**，一次重启都没有。根因不在脚本，在 `10-configmap.yaml`：nginx 写成前缀 `location /` 时 `/nope` 也返回 200，探针永远绿。已改成精确匹配 `location = /`（验证过：`/`→200，`/nope`→404）。等待逻辑也改成按 `RESTARTS>=2` 采样，而不是死睡 25s——实测首次杀掉落在 22~25s，25s 是压线，经常什么都抓不到。
  2. **取证管道原本是结构性空管道**：`kubectl describe pod -l app=web` 命中多个对象时**整段 `Events:` 都不打印**（实测 256 行输出里没有这个字段）。演练 3 改成按 Pod 名字 describe，演练 5 改成 `kubectl get events --sort-by=.lastTimestamp --field-selector reason=FailedScheduling`，两处现在都能打出真实事件行。
  3. **演练 6 在这套底座上是生效的**（收紧前 `REACHABLE` → apply 后 `BLOCKED` → web 白名单仍 `OK`）。原因不是 flannel，是 k3s 自己起了策略控制器：`journalctl -u k3s` 里有 `Starting network policy controller version v2.6.3-k3s1`，对应开关 `k3s server --help` 里的 `--disable-network-policy`；原先那句"k3s 自带的 flannel 里已打包 kube-router 的策略控制器"我没法从本机验证到"打包自 kube-router"这一层，已按日志改写。"CNI 静默忽略策略"在这里不是风险，但你换集群仍要重跑一遍。另外补测了 local-path 的回收：默认 StorageClass 建的 PV 是 `RECLAIMPOLICY=Delete`，删掉 PVC 后磁盘目录约 30s 内被回收、PV 对象随之消失（用一次性 PVC+Pod 实测，没动 redis）。`99-cleanup.sh` 原先那句"删 ns 不会自动回收目录"在默认配置下是错的，已按实测改写。
  4. **`51-netpol-default-deny.yaml` 原本是个悬空文件**：README 目录树说它"只在演练里手动打开"，但没有任何演练脚本引用它；它自己的注释还写"必须配套放行 DNS，否则全站 502"——这半句和实测不符。本机把两条一起 apply：default namespace 的 Pod 连 app-demo 的 web ClusterIP → `DENIED`，delete 后同一探测立刻 `REACHABLE`；同 namespace 内（web→redis、web→Service）照旧通；Pod 内 `127.0.0.1:8080` 不受影响。DNS **没断**，因为这两条只声明 `policyTypes: [Ingress]`（不限 egress，且选中的只是 app-demo 的 Pod，CoreDNS 在 kube-system）。真正被断的"跨 namespace 进来的入站流量"，恰恰就是 Ingress controller / 监控采集器所在的方向，所以全站 502 的成因说法成立、"断 DNS"的说法不成立。四条实测结论已写进该文件头，apply/删除命令也在那里。
  5. **顺序连跑不打架，但演练 5 的取证原本会拿到陈旧样本**：1→8 连跑一遍（3m06s）每个演练的收尾都把现场恢复干净了（d2 回到 `served by <pod>`、d3 探针改回 `/healthz`、d5 requests 改回并滚回 3 副本、d6 策略删除、d4 的 `oomtest` 删掉）。但 d5 那句 `get events --field-selector reason=FailedScheduling | tail -3` 不过滤对象，实测把 44 分钟前上一轮那个 Pod 的事件也打了进来——事件不随 Pod 消失，看着像证据其实是陈旧样本。已改成先抓 Pending 的 Pod 名再按 `involvedObject.name` 过滤，并在输出里把取证对象打出来；`--event-ttl` 的默认保留时长本轮没核到（`k3s server --help` 里查不到这个参数），只写实测到的"48 分钟前的事件还在"。

## 这套东西能验证什么、不能验证什么

必须说清楚边界，否则你会拿着一个假的安全感去上生产。

**能验证**（这些恰恰是本地最划算的部分）：YAML 语法与 API 版本是否被接受、探针配错会不会反复重启、Service selector 与 targetPort 是否命中、配置变更是否真的需要滚动、资源 request 与 limit 的语义、Pod 被杀后的自愈行为、滚动更新与回滚是否无损。

**不能验证**（这些只能去和生产同云、同 CNI、同 CSI 的 staging）：NetworkPolicy 在真实 CNI 下的边界行为、云盘 detach 慢导致 Pod 卡 `Terminating`、跨可用区延迟对 Redis 主从的影响、SLB 健康检查与 K8s readiness 的联动、节点驱逐与 PDB、HPA 在真实负载曲线上的抖动、镜像拉取限速与冷启动时间。

单机 k3s 尤其不能验证的还有：调度（只有一个节点，反亲和/拓扑约束全是空转）、存储漂移（local-path 永远在本地，见演练 7）、多副本同时被驱逐。想练这些就加节点，但**加了节点的 k3s 也不会更像生产，只会更像另一套自建集群**。

## 与生产对齐清单

每次上集群前对一遍，这一节的收益比纠结用哪个发行版大一个数量级：

| 项 | 本地怎么对齐 | 不对齐的代价 |
|---|---|---|
| K8s minor 版本 | 装前查生产 `kubectl version`，用 `INSTALL_K3S_VERSION` 锁到同 minor（或本地更新一点） | 废弃 API 路径、行为变更，本地绿线上红 |
| Ingress class 名与注解 | 本地装同款 controller（本示例走 ingress-nginx） | 注解静默失效，Ingress 不通且无报错 |
| StorageClass 名 | 建一个同名 SC 指向 hostpath 占位 | 清单里的 `storageClassName` 到生产改掉 = 配置漂移源头 |
| 镜像仓库与凭证 | 本地也走私仓（或 kind 的本地 registry），别习惯 `docker.io` 直连 | imagePullSecret 忘配，上线卡 `ImagePullBackOff` |
| Namespace 策略 | 与生产一致的 namespace 划分 + 资源配额 | 生产 Quota 一 apply 就 Pending |
| 配置来源 | 生产用 External Secrets/KMS，本地假值 + 明确注释"反面示范" | 密钥进 Git；`kubectl get secret -o yaml` 明文外泄 |
| 环境差异表达 | 一份 base + Kustomize/Helm overlay，GitOps 提交，禁止手 `kubectl apply` | dev 改的东西上不了 prod，且没人知道线上到底跑的是哪份 YAML |

## 常见报错 → 定位

| 现象 | 第一反应 |
|---|---|
| 节点一直 `NotReady` | `journalctl -u k3s -n 100`；WSL 里试 `sudo modprobe nf_conntrack br_netfilter`，缺内核模块时 `sudo apt install linux-modules-extra-$(uname -r)` |
| Pod 永久 `ContainerCreating` | `kubectl describe pod` 看事件；`FailedMount` 多半是 PVC 没绑定（回看 `local-path-provisioner` 是否 Running） |
| PVC 一直 `Pending`，provisioner 明明是 Running | 先 describe 看事件是不是 `WaitForFirstConsumer`——k3s 的 local-path 就是这个 `volumeBindingMode`（本机实测），**PVC 要等有 Pod 引用它才绑定**，这不是故障；真故障是事件里出现 `provisioning failed` |
| `ImagePullBackOff` | 先确认 `registries.yaml` 改了之后 `systemctl restart k3s` 过没有 |
| `CrashLoopBackOff` 但日志正常 | 大概率是 liveness 探针打死它，看 `describe` 里的 `Liveness probe failed`，别只看应用日志 |
| `kubectl describe pod -l app=web` 里根本没有 `Events:` 段 | 不是没事件，是 describe **只在描述单个对象时**打印 Events（本机实测：`-l` 命中 3 个 Pod 时 256 行输出里没有一行 Events）。改成 `kubectl describe pod <名字>` 或 `kubectl get events --sort-by=.lastTimestamp` |
| Service 通、Pod 健康、请求就是不通 | `kubectl -n app-demo get endpoints web` 是空的 → selector 和 Pod label 没对上 |
| `kubectl` 报 `connection refused localhost:6443` | 忘了 `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`，或 k3s 服务没起 |
| `docker ps` 是空的 | 正常，k3s 用内置 containerd：`sudo k3s crictl ps` |
| WSL 重启后集群没了 | WSL 默认会回收 VM。`wsl --shutdown` 之后要 `wsl` 重进，k3s 靠 systemd 自启；想让它一直活着就在 WSL 里开个常驻终端 |

## 收尾与下一步

```bash
sudo bash 99-cleanup.sh        # 只删 namespace，k3s 保留
sudo bash 99-cleanup.sh all    # 连 k3s 一起卸（官方卸载脚本，不要自己 rm 数据目录）
```

卸完在 Windows 上执行 `wsl --shutdown` 把内存真正还回去——这就是我们前面说的"按需启停"形态：不用的时候 vmmem 直接消失。

下一步按你的目的挑：

想**动手加深**，把 `20-deployment.yaml` 换成你真实项目的镜像（改末尾那 5 处），跑通"改代码→CI 出镜像→改 tag→`kubectl rollout undo` 回滚"这一整条链路，这是最能沉淀成简历素材的一段。想**换个更轻的底座**，同样的 manifests 在 kind 上完全可用（kind 需要的是 docker，也就是你上一轮定的裸 WSL2 + docker-ce，不用额外 VM）。想**整理成复盘文档**，我建议把演练 3、5、8 各写成一页"现象→定位→根因→修复→预防"，这三条比"我会写 Deployment YAML"有说服力得多。

