---
title: "自研 Agent 应用工程化落地纪要"
summary: "项目根：Agent 服务代码目录（Spring Boot 3.5.3 / Spring AI 1.1.8 / JDK17，端口 8090） > 定…"
date: 2026-10-04
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
category: deep
series: "agent"
slug: "自研 agent 应用-工程化落地纪要"
reading_time: 19
featured: false
draft: false
---
# 自研 agent 应用 工程化落地纪要

> 项目根：Agent 服务代码目录（Spring Boot 3.5.3 / Spring AI 1.1.8 / JDK17，端口 8090）
> 定位：阶段 1~6（工具调用 / RAG / 真实业务接口+记忆 / 护栏 / MCP / 评测）合流的**产品层**——一个 ChatClient 统调本地 `rag_search` 工具 + MCP 动态发现的业务工具 + 记忆 + 治理。
> 本文记录在此之上做的**中间件优化 + 安全/可观测/测试加固**，每条都带实测数据与踩坑；末尾是"诚实教训"和已知边界。

---

## 0. 一句话总览

把一个"能跑的 demo"打磨成"能自证、可运维、有回归"的工程件：**能落 Redis 的全落了 Redis；检索做成向量+词法+精排三段式；评测进 Langfuse 版本化；熔断舱壁换 resilience4j；infra 收进一个 compose；再按 code-review 补了鉴权/脱敏/测试**。全程"改前改后各跑一轮、数字难看先怀疑尺子"。

---

## 1. Redis Stack 全量迁移（向量库 / 语义缓存 / 会话记忆）

自托管 `redis/redis-stack-server`（含 RediSearch + ReJSON），端口 6380，命名卷 + AOF 持久化。三样东西都从进程内搬到它：

| 组件 | 类 | 存储 | 关键点 |
|---|---|---|---|
| RAG 向量库 | `RedisVectorStore`（自定义 Bean） | `rag-index` / `app:doc:<id>`（RedisJSON） | 向量字段 `embedding` HNSW/COSINE/dim1024；`content`+`source` TEXT |
| 语义缓存 | `SemanticCache` | `cache-index` / `app:cache:<sha1>` | FT.SEARCH KNN + 本地精确复算余弦 + facet 校验 + namespace 隔离 + TTL |
| 会话记忆 | `RedisChatMemoryRepository`（自建） | `app:chat:<cid>`（JSON 串 + TTL） | 接 `MessageWindowChatMemory`；Spring AI 1.1.8 只有 InMemory 版 |

**实测**：语义缓存 hit 0.12s vs miss ~27s（≈200x）；杀 JVM 重启后 `--scan app:cache:*` 仍在、原题重问 `cached:true` 不花 LLM（内存版做不到）；会话记忆重启后 `/history` 仍在、追问"那它排第几"靠记忆把"它"解析成清华答"第1名"。

**踩坑（都靠实测/javap 逼出来）**：
1. yml 键是 `index-name` 不是 `index`——写错**不报错**、静默回落 `default-index`。
2. starter 自动装配的 `RedisVectorStore` **不回传自定义 metadata（source）**→ 自建 Bean + `.metadataFields(MetadataField.text("source"))` + `spring.autoconfigure.exclude` 掉自动装配 + `FT.DROPINDEX idx DD` 重建 schema。
3. 入库默认 UUID id + 持久卷 → **每次重启多灌一份**（实测堆到 55）→ 改**确定性 id `sha1(来源+正文)`**，重启变覆盖（稳定 11）。
4. 普通 `redis:7` 无向量/全文模块（早期误以为"Redis 已在跑就行"）→ 必须 redis-stack-server。
5. `FT.DROPINDEX idx DD` 才连哈希一起删；清 FT 数据不能用 shell `del`（安全守卫会拦）。

---

## 2. 检索三段式：向量 + BM25 → RRF → Rerank

`rag_search` 链路：`① 向量宽召回 top10 → ② RediSearch BM25 词法 top10 → ③ RRF(k=60) 融合 → ④ qwen3.7-text-rerank 精排 → top4`。任一臂异常→退回向量原序，检索不炸。

- **Rerank**：百炼 MaaS `POST .../compatible-api/v1/reranks`（注意 **`compatible-api` 不是 chat/embed 的 `compatible-mode`**），体 `{model,query,documents[],top_n}`，回 `results[].{index,relevance_score}`；实测 200/~0.4s。
- **混合检索中文真实边界（实测）**：RediSearch 按空白/标点分词，**连续中文=一个 token** → 自由中文短语 BM25 命中 0；只有精确词/数字/英文/标点分隔词有效（`8000`✅ `MBA`✅ `网上报名`✅，`学费`❌）。所以词法臂定位成**"精确词增强器"**，中文靠向量臂。
- **A/B 探针**：`GET /api/agent/retrieve?query=&pool=&keep=&hybrid=&rerank=`（绕开 LLM、确定性）。实测把向量池压到 3，开 hybrid 后有块以 `vec=-1 lex=1` **被纯词法补召回**（融合确实扩召回）。

**诚实边界**：小 FAQ 语料答案集中在 1~2 块、纯向量 MRR 本就接近满分 → 别指望 precision 大幅跳（`phase2-rag` 已证 precision@4=0.55 是"答案1块却保4块"的**指标假象**）。这里实测到的是扩召回 + 连续相关性分 + 剔噪，不是 precision 神话。

---

## 3. 语义缓存的两道防线（facet + namespace）

- **维度一致性校验（facet guard）**：给问句抽"维度指纹"（省份/城市/排名/学费/条件/时间/材料…领域词表），命中除余弦≥阈值外还要**指纹相等**。实测造近重复（`Q = E原句 + 追加另一维度子句`）在 0.92 **真碰撞**：关护栏时"同济学费…在哪个城市"命中"只答学费"的缓存、丢了"上海"（有害）；开护栏 `guardRejects=1`、走真实链路答全。
- **命名空间隔离（namespace）**：缓存 key/过滤加 `namespace = 模型@KB指纹`（`KbFingerprint` 对 kb 算内容哈希）。**换模型/改语料自动失效**（实测换模型后同题 `cached=false`），护住"评测分不被 stale 缓存污染"。
- 自然同义改写（换词不换维度）余弦 <0.92 不误命中 → 护栏不误伤。

---

## 4. resilience4j 换手写舱壁/熔断

`AgentService`：原手写 `Semaphore` → **`Bulkhead("llm")`**（`max-wait=0` 保持 fail-fast，配额复用 `agent.llm-max-concurrent`）+ **`CircuitBreaker("agentChat")`**（滑窗10/min-calls5/失败率≥50%开/开态15s/半开放2/自动转半开）。`/stream` 也纳入同一套（手工 tryAcquirePermission + onSuccess/onError 挂 Flux 终态）。

**关键点**：真实调用 `doChat` 必须**让异常往上抛**，CircuitBreaker 才计得失败（中间层吞了就永远 CLOSED）。
**实测**：`/resilience-demo` 走通 CLOSED→OPEN→快速拒绝→HALF_OPEN→CLOSED；并发 8 条 /chat(max=3)→3 通过 5 拒；并发 5 路 /stream→3 通过 2 拒；`/actuator/prometheus` 有 `resilience4j_*` 指标。

---

## 5. Langfuse Datasets 评测闭环

- gold 集进 Langfuse 数据集 `gold 数据集`（13 题：6 normal + 7 对抗 similar/exact/multihop/refuse），按 id upsert、版本化；打分器**读回 Langfuse 的 expectedOutput**（集=唯一真相源）+ `expect_absent` 反幻觉 + 按类型汇总。
- `run-langfuse-eval.py`（纯 stdlib，Basic pk:sk）：`seed`/`run <name>`/`cachetrap <name>`；回写 `trace-create`+`dataset-run-items`+`scores`(NUMERIC)。多 run 在 UI 并排对比。
- **契约从运行实例 `/generated/api/openapi.yml` 现查**（v3.225.7），没照文档猜。
- **两轮自我纠错（价值所在）**：① 第二次跑同题 `tool_correct 0/6`——语义缓存持久化把整条链跳过了 → 给 `/chat` 加 `nocache=true`；② 初判"0.92 省份↔城市串味"是**测试假象**（B 命中自己同文本 + DB city 就返回浙江）→ 改"Q=E+追加子句"造真碰撞才证实护栏有效。
- 与 phase6 gate 互补：gate 管"过没过（中位数阈值）"，Datasets 管"分数趋势 + 逐条 trace"。

---

## 6. Higress AI 网关（统一入口）

`higress-registry…/higress/all-in-one`（控制台 :8001、数据面 :8080，避开被后端占的 80）。经控制台 **REST API 脚本化**配了：DNS 服务来源 `bailian-maas`（→MaaS:443/https/SNI）+ 路由 `/v1/chat/completions`、`/v1/embeddings` → 重写到 `/compatible-mode/v1/...` + 透传鉴权。自研 agent 应用 `--spring.ai.openai.base-url=http://localhost:8080` 后 chat+embedding 全经网关，端到端问答正常。
**限流也脚本化了**：内置 `key-rate-limit` 经 `PUT /v1/global/plugin-instances/{name}`（配置字段是 `rawConfigurations` 不是 yamlConfig），实测并发 6 发→1×200+5×429。复现脚本 `setup-higress-ai-gateway.sh`。
**没做透（诚实）**：多厂商 fallback（`fallbackConfig`/`proxyNextUpstream` 字段逆通了、provider 建了，但单 MaaS 环境造不出可重试的传输层失败，failover 没验成，已回滚稳定路由）。

---

## 7. 统一基础设施（compose）

项目根 `docker-compose.yml` 一把起全部 infra：后端 `bx-mysql/redis/minio/mongodb`（容器名仍 `bx-*`）+ `redis-stack`(6380) + Langfuse 栈。合并时处理的坑：服务名去重（两份都有 redis/minio）、相对路径前缀、后端口令只用 `env_file`（不写 `environment:${}`，否则空值覆盖真值）、**命名卷固定 `name:`** 切换不丢数据。之前手动 `docker run` 的 redis-stack 也收编进来。

---

## 8. Code-review 六条加固

| # | 问题 | 处置 | 验证 |
|---|---|---|---|
| 1 | /stream 绕过治理链 | 经 `AgentService.stream()` 走同套 Bulkhead+CB（流式仍不接缓存/轨迹，记边界） | 并发 5 路→3 通 2 拒 |
| 2 | 轨迹 byCid 无界泄漏 + 同 cid 串写 | 每请求自持事件列表 + TTL + maxEntries 清扫 | 单测 |
| 3 | 缓存未按 模型/KB/会话 隔离 | namespace=模型@KB指纹 + facet 校验（见 §3） | 换模型 cached=false |
| 4 | 调试端点裸奔 | `AdminAuthInterceptor` 给 /history·/trace·/retrieve·/cache-stats·/resilience* 加 `X-Admin-Token`（/chat·/stream 属产品API另议） | 无/错 token→401、对→200、/chat 仍公开 |
| 5 | prompt/completion/工具内容全量入观测 | `application-prod.yml` 关三开关；`--spring.profiles.active=prod` 脱敏；启动 `[obs]` 日志可验 | dev=true/true、prod=false/false |
| 6 | 无自动化测试 | 抽出 Rrf/Vecs/rerank-parse + 单测 + 4 真Redis集成测试（双模式：testcontainer / `-Dredis.it.port=6380`）+ Web层鉴权测试 + CI workflow | `mvn clean verify` **25/25 绿** |

---

## 9. 诚实教训（这套流程最值钱的部分）

1. **数字难看先怀疑尺子**：precision@4=0.55 是指标假象、judge 假阴性、"1 月"带空格的关键词假阴性——都是量具/口径错，不是系统差。
2. **发现的"bug"先怀疑自己测错**：0.92"串味"初判是测试假象（同文本自命中 + DB 数据本就那样），再造可控数据才证伪/证实。
3. **别为显先进堆架构**：混合检索/rerank 在玩具语料上看不出 precision 提升，先判断问题属于哪种再上重武器。
4. **不伪造绿灯**：Higress 多厂商 fallback 没验成就标"未做透"、Testcontainers 本机跑不动就如实说环境不兼容——宁可标"部分/待办"也不假绿。
5. **评测必绕缓存/上下文污染**：否则量的是缓存不是系统（`nocache`）。

---

## 10. 已知边界 / 未做

- Higress 多厂商 fallback、token 配额限流（机制逆通、未验成）。
- MCP server 鉴权（Higress 附带收益，未做）。
- 缓存 user/tenant 隔离（多租户才需要）。
- 全量 Spring 上下文/端到端 IT（已有真Redis集成，缺 @SpringBootTest 级）。
- 成本是占位单价；Guardrails/内容安全生产再上。

---

## 11. 复现命令

```bash
# 起全部 infra（后端库 + redis-stack + Langfuse）
cd Agent 项目根目录 && docker compose up -d

# 起后端 + mcp-server + agent（见各 run-*.sh；agent 用 JDK17 全路径）
#   语义缓存/检索 A/B：
curl -G localhost:8090/api/agent/retrieve --data-urlencode "query@q.txt" -d hybrid=true -d rerank=true
curl -G localhost:8090/api/agent/chat --data-urlencode cid=c --data-urlencode "q@q.txt" -d nocache=true
curl -H "X-Admin-Token: <token>" localhost:8090/api/agent/cache-stats
curl localhost:8090/api/agent/resilience

# 评测：Langfuse Datasets
python run-langfuse-eval.py seed
python run-langfuse-eval.py run "my-experiment-$(date +%Y%m%d)"
python run-langfuse-eval.py cachetrap "cache-trap-$(date +%Y%m%d)"

# Higress AI 网关（先起容器 + 控制台设 admin 密码，再跑）
bash setup-higress-ai-gateway.sh          # 服务来源/路由；APPLY_RATELIMIT=1 加限流演示

# 测试（本机连已在跑的 redis-stack；CI 用 testcontainer）
cd 自研 agent 应用 && REDIS_IT_PORT=6380 mvn test      # 17/17
```

---

## 12. 构建与交付加固（build verification / hygiene）

- **环境锁死**：`maven-enforcer-plugin` 强制 JDK `[17,18)` + Maven `[3.9,)`，不满足直接 fail（杜绝"只在我电脑能编"）。本机 JDK 17.0.19 / Maven 3.9.16。
- **构建门**：`mvn -B clean verify` → BUILD SUCCESS，**25 个测试全绿**（13→25：新增 rerank 边界/空/缺字段、recorder 并发不交叉、Web 层鉴权 401/200/缺参 400）。
- **仓库卫生**：`.gitignore` 从"逐个模块列 target/"改成 `**/target/` + IDE + 临时 txt；把之前**误提交进 git 的 `phase5-*/target`、`phase6-eval/target`（含 `.jar.original`）和 `自研 agent 应用/c1~c3.txt`** 用 `git update-index --force-remove` 取消跟踪（本地文件保留）。
- **配置分层**：`application.yml`(dev) / `application-prod.yml`(prod 观测脱敏)；默认不跑 prod，需 `--spring.profiles.active=prod`；密钥全走 env。
- **交付 checklist** 见 `自研 agent 应用/README.md`「发布与交付」节。

---

*配套细节文档：`自研 agent 应用/README.md`（逐 API 精读 + 各节实测）、`Agent中间件优化选型.md`（选型与优先级）、`RAG代码与原理详解.md`、`phase6-评测代码与原理详解.md`、`observability/README.md`。*
