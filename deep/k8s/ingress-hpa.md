# Ingress + HPA 实践清单（sc-user）

两份清单已生成，风格对齐既有 sc-user 资源（命名空间 `sc-demo`）：

| 文件 | 作用 |
|---|---|
| `service-user-hpa.yaml` | HPA：CPU 利用率 50% 触发，副本 min 2 / max 10 |
| `service-user-ingress.yaml` | Ingress：host `service-user.local` 的 `/` 路径 → `service-user:8081` |

K3s 全家桶已含依赖组件，**无需额外安装**：Metrics Server（HPA 用）、Traefik（Ingress 用）。

---

## 一、HPA 自动扩缩容

部署：
```bash
kubectl apply -f service-user-hpa.yaml -n sc-demo
```

查看状态：
```bash
kubectl get hpa -n sc-demo
kubectl describe hpa service-user -n sc-demo
```

压测看自动扩容（集群内起一个循环打流量的临时 Pod）：
```bash
kubectl run curltest --rm -it --image=curlimages/curl -n sc-demo -- \
  sh -c 'while true; do curl -s -o /dev/null http://service-user:8081/user/whoami; done'
```
另开终端观察（TARGETS 上升、REPLICAS 从 2 涨上去）：
```bash
kubectl get hpa -n sc-demo -w
kubectl get pods -n sc-demo -l app=service-user -w
```
停掉压测后约 5 分钟（默认缩容冷却），副本自动回落到 2。

> 注：HPA 不是真删 Pod，是改 Deployment 的 `replicas` —— 和你手动 `kubectl scale` 是同一件事的自动化。

---

## 二、Ingress 七层入口

部署：
```bash
kubectl apply -f service-user-ingress.yaml -n sc-demo
kubectl get ingress -n sc-demo
```

验证方式 A（集群内，最稳）：
```bash
kubectl run curltest --rm -it --image=curlimages/curl -n sc-demo -- \
  sh -c 'curl -s -H "Host: service-user.local" http://traefik.kube-system:80/user/whoami'
```
> Traefik 在 `kube-system` 命名空间，Service 名 `traefik`、端口 80，相当于把请求送到 Ingress Controller 让它按规则转发。

验证方式 B（WSL 内，Traefik 用 hostPort 绑了节点 80）：
```bash
curl -H "Host: service-user.local" http://localhost/user/whoami
```
> Windows 侧可能因 WSL2 网络限制不通（同 NodePort 坑）；WSL 内或集群内验证即可。
> 想用真域名，把 `service-user.local` 换成你的域名，或在 `/etc/hosts` 里把该域名指向节点 IP。

> Ingress 不替代 Service：它站在 Service 前面做「按规则分流」，Service 仍负责集群内负载均衡。

---

## 三、清理

```bash
kubectl delete -f service-user-hpa.yaml -f service-user-ingress.yaml -n sc-demo
```
