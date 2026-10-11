# Spring Cloud → K3s 极简落地：用最小子集认识 K8s

> **目标定位**：本方案的目的**不是验证微服务全功能**（分布式事务 / 消息队列 / 定时任务 / 链路追踪都不在目标内），而是**借助 spring-cloud 这个现成项目，在已搭好的 K3s 上认识 K8s 核心概念**。因此尽量精简：只保留最少服务和最少依赖，把注意力放在 K8s 本身。
> **状态**：§4、§5 已于 2026-10-07 实跑通。落地载体**不是原项目**，而是新建的独立最小项目 `k3s-project/sc-user`（`spring-cloud` 源码一行未改，`git status` 已核）；清单在 `k3s-project/manifests/sc-user/`，命名空间 `sc-demo`。
> **衔接**：K3s 底座、`compose-vs-k8s.md`、`manifests/`（nginx 替身那套示例）、`scripts/`（装 K3s / 自检 / 演练 / 清理）均已就绪；本次新增 `sc-user/`（代码+镜像+外部库）与 `manifests/sc-user/`（五份清单）。

---

## 1. 还能省什么（从"完整方案"砍到"极简 K8s 学习方案"）

| 原组件 | 在"认识 K8s"里是否必需 | 处理 |
|---|---|---|
| 5 个业务服务 | 否，1 个就够演示 Pod/Deployment/Service | 新建 `sc-user`，只抄 user 相关 4 个类 + 1 个新 Controller，**不动原项目**（原因见 §4.1） |
| Nacos（注册发现 + 配置） | 否，**K8s 自带 Service 做服务发现**，Nacos 在 K8s 里是冗余 | 新项目 pom 里根本不引入 |
| Seata（分布式事务） | 否，单服务用不到 | 同上，不引入 |
| Redis（缓存） | 可选 | 不引入；想学 StatefulSet 再单独上 K8s（§6） |
| Dubbo | 否，K8s 里用 Service 名调用即可 | 不引入（原项目里它**有消费方**，见 §4.1 第 1 条） |
| RocketMQ / XXL-JOB / SkyWalking / Sentinel / OpenFeign | 否 | 不引入 |
| MySQL | **是**（datasource 必需，否则起不来） | **外置**，用 `sc-user/docker-compose.mysql.yml` 只起 mysql，不上 K8s |

**结论**：上 K8s 的只有 `sc-user` 一个 **Deployment + Service**；外部只留一个 **MySQL 容器**。其余组件一律不碰——它们既不是 K8s 概念，也不影响你认识 K8s。（已按此实跑：2 副本 `1/1 Running`、0 重启。）

---

## 2. 最小依赖与端口

- **外部（docker 起）**：`sc-mysql`（`mysql:8.4`）3306，容器内自动执行 `sc-user/sql/init-user.sql` 建 `user_db`
- **K8s（本方案核心）**：ns `sc-demo`，Deployment/Service 都叫 `service-user`，`containerPort 8081`，Service `ClusterIP` 且 `port 8081 → targetPort http`
- **可打的端点**：`GET /user/info/{userId}`（查库，正/反例都能验）、`GET /user/whoami`（返回 Pod 名，用来看 Service 负载均衡和"滚动更新到底换了哪批 Pod"）、`/actuator/health/{liveness,readiness}`
- **可选上 K8s**：Redis → `redis:6379`（StatefulSet + PVC，§6，未做）

---

## 3. 环境前置（2026-10-07 实测）

- **WSL2 + Docker**：`docker` 与 `docker compose` 可用；`redis:7 / mysql:8.4 / busybox / clickhouse` 在本机镜像里。
- **K3s**：节点 `lyg` / `v1.36.5+k3s1` / `Ready`。
- ⚠ **本机 kubectl 入口是 `sudo k3s kubectl`**（没有独立 kubectl），§5.4 的命令都按这个写。
- **JDK 8**：Windows 侧 `java version "1.8.0_481"`（`D:\jdk-8u481`）。
- **Maven**：`D:\apache-maven-3.6.1`（PATH 默认）与 `D:\apache-maven-3.9.16` 都在，两个 `conf/settings.xml` 都把本地仓写死成 `D:\repo\repository`；本次构建用 3.9.16。
- **MySQL 是空的**：起之前 `docker ps -a` 里没有任何 mysql 容器/镜像/卷，所以 §4.3 那次 `up -d` 是全新初始化（不是覆盖旧库）。

---

## 4. 落地载体：独立最小项目 `sc-user`（原项目零改动）

### 4.1 为什么不改在原项目上（三条当场核到的事实）

1. **`@DubboService` 有消费方**（原文这里判断错了）。三处 `@DubboReference`：
   - `spring-cloud/service-order/src/main/java/com/gui/controller/OrderController.java:30` → `@DubboReference(timeout = 5000)`
   - `spring-cloud/service-order/src/main/java/com/gui/job/TimeoutOrderJob.java:44` → `@DubboReference(timeout = 5000, check = false)`
   - `spring-cloud/service-pay/src/main/java/com/gui/service/impl/PayServiceImpl.java:36` → 同上
   所以"顺手把 service-user 的 Dubbo 剥掉"= 打断整条 5 服务链路，代价不该由学习方案来付。
2. **`spring.cache.type=none` 不会让 `@Cacheable` 变 no-op**（原文药方在本项目不成立）。`service-user/src/main/java/com/gui/config/RedisCacheConfig.java:22-23` 显式声明 `@Bean RedisCacheManager`，显式 bean 让缓存自动配置让位，运行期照样打 Redis；要彻底不依赖 Redis，必须连这个类（连同 `@EnableCaching`）一起去掉。
3. **原项目没有 fat jar**。6 个 pom 里 grep `spring-boot-maven-plugin` 零命中（模块靠 `start-all.bat` 的 `mvn spring-boot:run` 起），`mvn package` 只产出普通 jar（MANIFEST 无 `Main-Class`，我当时打出的 `service-user-1.0-SNAPSHOT.jar` 仅 7.9 KB——那是减法 pom 的产物，但根因相同：没有 repackage 执行）。上镜像必须自己加这个插件。

> 结论：与其在原项目上做减法（还牵动 order/pay），不如把 user 这条最小闭环抄成一个新项目，两边互不干扰。

### 4.2 新项目内容

```
sc-user/pom.xml                       parent 直接是 spring-boot-starter-parent 2.7.18；依赖只有 4 个：
                                      web / actuator / mysql-connector-java / mybatis-plus-boot-starter 3.5.3.1
                                      + spring-boot-maven-plugin（repackage 出可执行 jar）
                                      ⚠ Boot 2.7.18 已不再管理 mysql:mysql-connector-java 的版本，必须显式写 8.0.33，
                                        否则构建报 'dependencies.dependency.version ... is missing'（本轮踩过）
sc-user/src/main/java/com/gui/        UserApplication / entity.User / mapper.UserMapper /
                                      service.UserService(+impl) / controller.UserController
sc-user/src/main/resources/application.yml   只有 datasource（全走 ${MYSQL_*}）+ 探针 + mybatis-plus；
                                      密码故意不给默认值：漏挂 Secret 要立刻失败而不是拿空密码去连库
sc-user/sql/init-user.sql             建 user_db + t_user + 2 行种子（数据与原项目一致，便于对照；无 undo_log）
sc-user/docker-compose.mysql.yml      外部 MySQL 8.4，把 init 脚本挂进 /docker-entrypoint-initdb.d，首次起自动建库
sc-user/Dockerfile                    temurin 8-jre + 非 root uid 10001 + EXPOSE 8081 + MaxRAMPercentage=70
```
`UserService` 接口从 `api-common` 复制进本项目，所以不再依赖 `api-common`；provider 侧也不再有 Dubbo。`@Cacheable/@CacheEvict` 直接不带（没有 `@EnableCaching`，注解本来就是死的，留着只会误导）。

### 4.3 构建与外部依赖（本轮实跑过的命令）

```bash
# 1) 打 fat jar（Windows）
cd /d/high-concurrency-projects/k3s-project/sc-user
/d/apache-maven-3.9.16/bin/mvn -B package        # → target/sc-user-1.0-SNAPSHOT.jar，26.6 MB，Main-Class=JarLauncher

# 2) 起外部 MySQL（WSL，全新卷会自动执行 init-user.sql）
wsl; cd /mnt/d/high-concurrency-projects/k3s-project/sc-user
docker compose -f docker-compose.mysql.yml up -d

# 3) 镜像（第二个 tag 只为演示滚动更新/回滚，内容其实同一个 digest）
docker build -t sc-user:0.1.0 .
docker tag  sc-user:0.1.0 sc-user:0.1.1
```

---

## 5. K8s 落地（核心，已实跑）

### 5.1 清单（`manifests/sc-user/`，命名空间 `sc-demo`）

| 文件 | 对象 | 要点 |
|---|---|---|
| `00-namespace.yaml` | Namespace `sc-demo` | |
| `10-configmap-user.yaml` | ConfigMap `user-env` | `MYSQL_HOST/PORT/DB/USER`，键名对上 `application.yml` 的 `${MYSQL_*}` |
| `15-secret-user.yaml` | Secret `user-secret` | `MYSQL_PASSWORD`；本地开发值，**别 commit 到外部可读的仓库** |
| `20-deployment-user.yaml` | Deployment `service-user` | 2 副本；`maxSurge 1 / maxUnavailable 0`；startup+liveness+readiness 三探针；`envFrom` configmap+secret；`requests 100m/256Mi`、`limits.memory 512Mi`；`runAsNonRoot`+`drop ALL`+`readOnlyRootFilesystem`（`/tmp` 挂 emptyDir，`fsGroup 10001`）；`preStop sleep 5` + `terminationGracePeriodSeconds 35`（这条是 §5.5 实测逼出来的） |
| `25-service-user.yaml` | Service `service-user` | ClusterIP，`port 8081 → targetPort http` |

镜像在 containerd 里的名字是 `docker.io/library/sc-user:0.1.0`（§5.3）。

### 5.2 Pod 里的 MySQL 地址不能写 localhost

Pod 的 `localhost` 是它自己的网络命名空间，而 MySQL 在宿主（WSL）的 docker 里。本机用 busybox 探针 Pod 实测（`nc -z -w2 <ip> 3306`，2026-10-07），四个候选地址**全部可连**：`192.168.0.104`（节点 LAN/IPVS 前置的 WSL 地址）、`172.18.0.1`（docker0 网关）、`172.17.0.1`、`10.42.0.1`（cni0）。清单选了 `192.168.0.104`。

⚠ 这是 WSL 重启后可能变的地址：变了只改 `10-configmap-user.yaml` 那一行，再 `sudo k3s kubectl -n sc-demo rollout restart deploy/service-user`（ConfigMap 改动默认不触发重建）。更稳的做法是再建一个手写 Endpoints 的 Service，让应用只认服务名、不绑宿主机 IP（本方案未做，留作 §6 之后的进阶）。

### 5.3 单节点镜像获取的坑（必看）

K3s 用 containerd，不是 docker，本地 `docker build` 的镜像默认拉不到：
```bash
docker save sc-user:0.1.0 | sudo k3s ctr images import -
# 核对是否真进去了——必须点名 k8s.io 命名空间：
sudo k3s ctr -n k8s.io images ls | cut -f1 | grep '^docker.io/library/sc-user'
```
⚠ 不加 `-n k8s.io` 时 `ctr images ls` 有可能看不到刚导入的镜像，**别据此断定"没导进去"**。tag 用明确的 `0.1.0/0.1.1` 配 `imagePullPolicy: IfNotPresent`，别用 `latest`。

### 5.4 kubectl 实操清单（本机入口 `sudo k3s kubectl`）

```bash
K="sudo k3s kubectl"
$K apply -f manifests/sc-user/ -n sc-demo
$K -n sc-demo get pods -o wide            # 2/2 Running
$K -n sc-demo rollout status deploy/service-user

# 排错三板斧
$K -n sc-demo describe pod <pod>          # Events：调度 / 镜像 / 探针
$K -n sc-demo logs <pod> --tail=50        # 连不上 MySQL 会在这里暴露
$K -n sc-demo exec deploy/service-user -- sh -c 'echo HOST=$MYSQL_HOST DB=$MYSQL_DB'   # envFrom 是否真注入

# 扩缩容
$K -n sc-demo scale deploy service-user --replicas=4
$K -n sc-demo get pods -l app=service-user --no-headers

# 滚动更新与回滚（需要第二个 tag，见 §4.3）
$K -n sc-demo rollout history deploy/service-user
$K -n sc-demo set image deploy/service-user service-user=sc-user:0.1.1
$K -n sc-demo get pods -l app=service-user -o jsonpath='{range .items[*]}{.metadata.name}{" = "}{.spec.containers[0].image}{"\n"}{end}'   # 直接看每个 Pod 跑的是哪个 tag
$K -n sc-demo rollout undo deploy/service-user

# 本机访问
$K -n sc-demo port-forward svc/service-user 18081:8081     # 端口若与本机进程冲突就换号，见 §7
curl -s localhost:18081/user/info/1
curl -s localhost:18081/user/whoami

# 看真实负载均衡必须在集群内发请求（port-forward 只会固定打到一个 Pod）
$K -n sc-demo run lbprobe --image=busybox:1.36 --restart=Never --rm -i --command -- \
  sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do wget -qO- http://service-user.sc-demo.svc.cluster.local:8081/user/whoami; echo; done'
```

### 5.5 实测：`maxUnavailable: 0` 不等于零请求失败

打点方式：ns 内一个 busybox Pod，每秒 `wget -T2 http://service-user.sc-demo.svc.cluster.local:8081/user/info/1`（走 ClusterIP → IPVS，且这条请求真的会查 MySQL）。

| 场景 | 结果 |
|---|---|
| 4 副本，**没有** `preStop`：正向 `set image` + 一次 `rollout undo` | 计数 `82 ok / 2 FAIL`，另有 1 行 `wget: can't connect to remote host (10.43.183.240): Connection refused` 被脚本记成了 ok ⇒ 实际 ≥3 次失败 |
| 同样 4 副本，**加了** `preStop sleep 5` + `terminationGracePeriodSeconds 35` | 正向 + 回滚两次替换期间 `111` 次打点全 ok，`grep -v '^ok$'` 无输出 |

原因同 `manifests/20-deployment.yaml:84` 那条注释：Endpoint 摘除是异步的，进程先死、转发规则后到，中间那批请求会 refused；`maxUnavailable: 0` 只保证"新 Pod Ready 之前不杀旧的"，管不住"杀的那一刻"。

⚠ 方法论两条：① busybox 的 `wget` 失败时也可能返回 0，靠 `&&/||` 计数会漏，判失败要连日志里的报错行一起看；② `port-forward` 由 kubectl 自己挑后端、只连一个 Pod（`manifests/21-service.yaml:24` 已写），拿它观察负载均衡必然失真。

---

## 6. 进阶：把 Redis 也放上 K8s（学 StatefulSet + PVC，未做）

若想多看一个 K8s 概念，把 Redis 用 `manifests/40-redis-statefulset.yaml`（已有范本）起在 K8s，配 PVC 持久化 + `redis` Service。再把 `sc-user` 的 Redis 配置指向 `redis:6379`（经 ConfigMap，并需要把缓存相关代码/依赖加回来）。这样你一次见过 **无状态(Deployment) + 有状态(StatefulSet) + 配置(ConfigMap/Secret) + 网络(Service) + 存储(PVC)** 五类核心对象——足够建立 K8s 的整体直觉。

---

## 7. 本机常见坑（精简）

| 现象 | 处置 |
|---|---|
| `port-forward` 报 `Unable to listen on port 8081 ... address already in use` | 本机 `java -jar` 还占着同一个 localhost（WSL mirrored 网络下 Windows 与 WSL 共用）。**此时你 curl 到的响应来自本机进程、不是集群**——先停本机进程或换端口号再验 |
| Pod CrashLoopBackOff，但同一个 jar 本机跑得好 | 十有八九 `MYSQL_HOST` 写了 `localhost`（Pod 里那连的是它自己），见 §5.2 |
| `kubectl apply` 把我 scale 出来的副本数打回原值 | 正常行为：apply 以清单为准。演练态与声明态冲突时，以清单为准 |
| `rollout undo` 提示 `last-applied-configuration` 不更新 | 混用 `apply` 与 `set image` 的后果；回滚后建议再 `apply` 一次对齐 |
| 探针一直失败 | 没引 actuator（新项目已加）；或路径写成 `/actuator/health`——探针要用 `/actuator/health/liveness` 与 `/readiness`（默认那个 health 不含探针组语义） |
| 镜像拉不到 / 以为没导入成功 | 见 §5.3，导入要 `-n k8s.io` 才看得见 |
| 只读根文件系统起不来？ | 本应用实测只需挂 `/tmp` emptyDir（`fsGroup: 10001`），没复现失败 |
| Pod 一直 Pending | 单节点资源不够；`describe` 看 Events，调小 limits 或减副本 |

---

## 8. 进度与下一步

已完成（2026-10-07 实跑）：§3 环境核对 → §4 `sc-user` 项目与 fat jar → 外部 MySQL（空库自动建 `user_db`）→ 镜像导入 containerd → §5 五份清单部署、`2/2 Ready`、经 Service 的正/反例、`envFrom` 注入、scale 2→4、`set image` 滚动、`rollout undo` 回滚、§5.5 的 preStop 前后对照。

未做，按性价比排序：
1. §6 Redis StatefulSet + PVC（补上"有状态 + 存储"这一类对象，是这套方案里唯一还没见过的核心概念）。
2. 手写 Endpoints 把 `MYSQL_HOST` 从"宿主机 IP"变成"集群内的服务名"（§5.2 那个会变IP的根因）。
3. `scripts/90-fault-drills.sh` 是围绕 `rdx-demo` 的 nginx 替身写的，要不要给 `sc-user` 复制一份对应演练（OOMKilled、探针失败、镜像拉不到）。
4. 演练完记得 `kubectl delete ns sc-demo` + `docker compose -f docker-compose.mysql.yml down`。

> 一句话：**上 K8s 的只有一个 Spring Boot 服务（独立新项目 `sc-user`）+（可选）一个 Redis；外部只有一个 MySQL 容器。原项目一行没改，专注把 K8s 的核心对象和操作摸熟。**
