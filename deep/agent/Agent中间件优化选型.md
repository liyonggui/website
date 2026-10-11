---
title: "Agent 中间件优化选型（性能 / 准确度 / 护栏 / 可观测）"
summary: "原则：带着病历找药。每个中间件都必须对应一个\"已实测出的问题\"，并说清\"治好了没\"——不是堆组件。 > 当前栈：Spring AI 1.1.8 + 百炼 MaaS + RAG 向量…"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
  - 压测
category: deep
series: "agent"
slug: "Agent中间件优化选型"
reading_time: 24
featured: false
draft: false
---
# Agent 中间件优化选型（性能 / 准确度 / 护栏 / 可观测）

> 原则：**带着病历找药**。每个中间件都必须对应一个"已实测出的问题"，并说清"治好了没"——不是堆组件。
> 当前栈：Spring AI 1.1.8 + 百炼 MaaS + **RAG 向量库 + 语义缓存均已迁 Redis Stack**（同一 `redis-stack-server:6380`：`rdx-rag-index` / `rdx-cache-index`，RediSearch HNSW/COSINE）+ 手写护栏（Caffeine/Breaker/舱壁）+ Langfuse + phase6 评测门禁。

## 一、性能类（延迟 / 成本 / 吞吐）

### 1. 语义缓存 Semantic Cache ⭐⭐⭐ ✅ 已落地并迁到 Redis Stack FT（rdx-agent：hit 0.12s vs miss ~27s；跨进程持久化实测通过）
- **治什么**：LLM 调用成本与延迟。现有 Caffeine 是精确 key 缓存（同一句话才命中）；语义缓存把"问题向量"存下来，新问题相似度 ≥ 阈值（如 0.92）**直接复用上次答案**，整个 LLM 调用省掉。
- **接哪里**：`rdx-agent` 的 `AgentService.execute()` 入口，或 phase4 `ToolGateway` 同款思路——命中判断从"key 相等"换成"向量相似度"。
- **基础设施**：⚠️ 早期记的"环境里 Redis 已在跑、自带向量检索"是**错的**——当时是普通 `redis:7`、**无向量/全文模块**。已单独拉起 **`redis/redis-stack-server`（端口 6380，含 RediSearch）**，RAG 向量库先迁过去（`rdx-rag-index`），**语义缓存也已迁到同一实例的独立索引 `rdx-cache-index`**（RedisJSON + HNSW/COSINE + `PEXPIRE` TTL）。实测铁证：杀掉 JVM 后 `--scan rdx:cache:*` 仍在、全新 JVM 原题重问仍 `cached:true`（内存版重启即丢，做不到）。
- **量化口径**：命中率 + 省下的 token/延迟；**误命中用 Langfuse `cachetrap` 实测标定（见 #8）**。
- **✅ 已实现"维度一致性校验"护栏（②）**：`SemanticCache` 给每个问句抽【维度指纹】(省份/城市/排名/学费/条件/时间/材料…，领域词表匹配)，缓存条目连指纹一起存；命中除"余弦≥阈值"外，还要**新旧问句指纹相等**才复用，同余弦近邻但维度不同(省份↔城市)判未命中、走真实链路。KNN 取前 K=5 候选再按"余弦+维度"择优；任一侧识别不出维度则不否决（保守、退回纯余弦）。`stats()` 有 `guardRejects` 计数。配置 `agent.cache-facet-guard` / `cache-candidate-k`。
- **✅ 缓存命名空间隔离（补强）**：缓存 key/过滤再加一层 `namespace = 模型@KB指纹`（`KbFingerprint` 对 classpath:kb 算内容哈希）——**换模型或改语料自动失效**，杜绝旧模型/旧 KB 的答案被复用（实测换模型后同题 `cached=false`）；这直接护住"评测分不被 stale 缓存污染"。user/tenant 多租户维度可再叠加。
- **⚠️ 诚实纠错 + 复测（两轮）**：上一轮我声称"0.92 对同实体换维度会串味"——**复查发现那是测试假象**（cachetrap 里两条 B 是同一问题文本、命中的是自己那条，且 DB 浙大 city 本就="浙江"）。但用"**Q = E 原句 + 追加一个别的维度子句**"造近重复，**在真实生产阈值 0.92 下确实碰撞了**：缓存存了"同济大学学费多少"，问"同济大学学费多少，**在哪个城市**"→ 关护栏时 `cached=true` 且答案**丢了"上海"**（纯余弦把只答学费的缓存复用给了多问一句的题，答不全=有害）；开护栏后 `guardRejects=1`、`cached=false`、真实链路把"上海 + 学费"都答对。指纹也印证：E=`学费`、Q=`城市,学费`，集合不等才拦。**结论**：自然同义改写(换词不换维度)余弦<0.92 不误命中（护栏不误伤），但"**复合问题命中其单问子集**"这类近重复会在 0.92 碰撞——维度校验正是拦这个，且实测有效。
- **预期**：FAQ/择校类高重复问题，命中率 30-60%。

### 2. AI 网关（模型路由 / 多 provider / 统一限流）⭐⭐ ◑ 落地（Higress 跑起来；模型流量+限流经 API 脚本化并实测 429；多厂商 fallback 机制已配、但 failover 未验成，已回滚稳定路由）
- **治什么**：单模型直连三宗罪——成本（简单题也用贵模型）、可用性（provider 抖动无退路）、治理（限流/计费/审计分散）。
- **选型**：**Higress**（阿里开源，原生支持百炼、MCP 代理与注册）；备选 LiteLLM / Kong AI Gateway。
- **已做（实测）**：`higress-registry…/higress/all-in-one:latest` 跑起来（控制台 :8001、数据面网关 :8080，避开被后端占用的 80）；经控制台 **REST API 脚本化**配了 `bailian-maas` DNS 服务来源 + 两条路由：`/v1/chat/completions`、`/v1/embeddings` → 路径重写到 `/compatible-mode/v1/...` + TLS/SNI 到 MaaS + **透传客户端鉴权**；rdx-agent `--spring.ai.openai.base-url=http://localhost:8080` 后，**chat 和 RAG 的 embedding 全部经网关，端到端问答正常**。复现脚本 `setup-higress-ai-gateway.sh`（登录 admin/admin→建服务来源→建路由→直连测）。
- **限流也脚本化了（关键坑记牢）**：内置 `key-rate-limit` 插件经 **`PUT /v1/global/plugin-instances/{pluginName}`** 配（不是 `/v1/global/plugin-instances` 集合 POST——那只 GET、POST/PUT 405），配置字段是 **`rawConfigurations`（YAML 串）不是 `yamlConfig`**（写错会被静默置空），`scope:ROUTE`+`target:llm-chat`。配 `query_per_second:1` 后，带 `x-demo-key: burst` 的并发 6 发实测 **1×200 + 5×429**。复现脚本 `setup-higress-ai-gateway.sh` 已含这步。
- **多厂商 fallback：机制摸通、但没验成（诚实，不硬凑）**：把 AI 抽象层 API 也逆出来了——`PUT /v1/ai/providers/{name}`（openai 型，key/url 在 `rawConfigs`）、`/v1/ai/routes`（`upstreams` 权重之和必须=100，另有 `fallbackConfig` + `proxyNextUpstream{attempts,conditions:[error,timeout]}`）。建了 `flash`/`max` 两个 provider + 一条带 fallback 的 AI 路由。但**实测没触发成功兜底**：把 flash 指黑洞 → `503 no healthy upstream`（DNS 挂属"无健康上游"，非可重试 error）；指超时 IP → 10s 后仍 `503`（连接超时也没走到 fallback，且我设的 timeout 没生效，ai-proxy 超时是另一套）。→ 结论：**Higress AI 路由的 fallback 触发条件对"传输层失败"不灵**，要可靠演示得让主 provider 返回可重试的 HTTP 5xx/或按 ai-proxy 的 model 级失败语义配——单一 MaaS + 盲调语义没啃下来，遂**回滚到稳定的单 provider plain 路由**（agent 恢复正常，5s 经网关答对），不伪造 200。providers `flash`/`max` 留着当已配好的底子。
- **接哪里（持久化）**：把 base-url 写进 rdx-agent yml 即常态走网关；rerank 是 `compatible-api` 另一条，单独配 `agent.rerank.url` 或再加一条网关路由。附带收益：Higress 还能给 **phase5-mcp-server 补鉴权/治理**（MCP 托管）。
- **附带收益**：Higress 可给 **phase5-mcp-server 补鉴权/治理**（当前 server 无鉴权是已知边界）。

### 3. resilience4j 替换手写舱壁/熔断 ⭐⭐ ✅ 已落地（rdx-agent：Bulkhead 换 Semaphore + CircuitBreaker 护 LLM 调用）
- **治什么**：手写熔断缺滑动窗口/失败率阈值/半开探活/指标。resilience4j 提供 CircuitBreaker/Bulkhead/RateLimiter/Retry 全家桶 + Micrometer 自动埋点。
- **接哪里**：rdx-agent `AgentService`——原手写 `Semaphore(tryAcquire→503)` 换成 **resilience4j `Bulkhead("llm")`**（`max-wait-duration=0` 保持"快速失败"，语义与 tryAcquire 一致；并发配额复用 `agent.llm-max-concurrent` 单一来源）；LLM+工具调用再套一层 **`CircuitBreaker("agentChat")`**（count 窗口10、min-calls5、失败率≥50% 开、开态等15s、半开放2、自动转半开）。**关键实现点**：真实调用 `doChat` 必须**把异常向上抛**，CircuitBreaker 才能计为失败（不能在中间层吞掉，否则永远 CLOSED）。`BulkheadFullException`→503(rejected)、`CallNotPermittedException`→503(circuit_open)。用 Registry 编程式取实例，不用注解（免 AOP 自调用坑）。
- **实测**：`/api/agent/resilience` 看各 CB/BH 状态+失败率+计数；`/resilience-demo?fail=` 用独立 "demo" breaker **确定性走完 CLOSED→OPEN(注入3失败)→快速拒绝→(8s)→HALF_OPEN→CLOSED(探针成功)**；并发 8 条 `/chat`(max=3)→ **3 通过 / 5 直接 503**；`/actuator/prometheus` 有 `resilience4j_circuitbreaker_*` / `resilience4j_bulkhead_*` 指标。
- **补：`/stream` 也纳入治理**——之前流式端点绕过链，现 `AgentService.stream()` 用同一 Bulkhead+CircuitBreaker（手工 tryAcquirePermission + onSuccess/onError/releasePermission 挂 Flux 终态），实测并发 5 路→3 通过 2 拒；流式仍不接缓存/轨迹（记为边界）。
- **话术**："自研验证机制(phase4 手写熔断先证明懂原理)→ 生产换成熟组件(rdx-agent 用 resilience4j，带滑动窗口/半开/Micrometer)"——两头都占。

### 4. 异步削峰（MQ / 任务队列）⭐
- **治什么**：复杂问题（生成报告、批量择校对比）同步占线程几分钟。
- **做法**：提交→返回 taskId→MQ→worker 消费→轮询/回调取结果。
- **选型**：RabbitMQ/Kafka；Spring 侧 `@Async` + 队列起步即可。

## 二、准确度类（检索 / 生成质量）

### 5. 专用 Rerank API ⭐⭐⭐ ✅ 已落地（rdx-agent rag_search：向量top10→`qwen3.7-text-rerank`→top4，实测 ~0.4s/次）
- **治什么**：LLM listwise 重排慢且贵；**专用 rerank 模型一次调用返回每块 relevance_score**。⚠️ **诚实边界**：本项目 toy 语料答案集中在 1~2 块、纯向量 MRR 已近满分，所以 rerank **不会**把 precision@4 抬起来（那 0.55 是"答案1块却保4块"的指标假象，phase2 已用数据证过）。这里实测到的真收益是：每块连续相关性分 + 把噪声块（标题行"# 示例语料"）挤出 top4 + 稳定排序。**量级收益要配 大/杂语料 + 答案分散 + hit@1/MRR 才显形**，别拿 precision@k 验收。
- **选型**：**百炼 MaaS OpenAI 兼容 `qwen3.7-text-rerank`**（零新基础设施）；端点 `POST .../compatible-api/v1/reranks`（注意 `compatible-api` 非 `compatible-mode`）。备选 Jina / Cohere / 本地 bge-reranker。
- **接哪里**：`rag/RagReranker.java`（降级安全：失败退回向量原序）+ `RagSearchTool`；A/B 探针 `GET /api/agent/retrieve?rerank=true|false`。开关 `agent.rerank.*`。

### 6. 混合检索（向量 + BM25）⭐⭐ ✅ 已落地（rdx-agent：向量KNN+FT词法→RRF→rerank）
- **治什么**：纯向量对精确词/专名/数字拉不准。落地实测确认了这条：查询 `8000`/`MBA`/`网上报名` BM25 秒中，而这些正是 embedding 易糊的。
- **选型**：**Redis Stack 自带全文检索**，KNN+BM25 同一 `rdx-rag-index`、RRF(k=60) 融合，无需外挂 ES；融合后再接 rerank。两路靠【确定性 id】对齐（入库改 `sha1(来源+正文)` 才能 keying）。
- **⚠️ 中文边界（实测，别当万能）**：RediSearch 默认按空白/标点分词，**连续中文=一个 token**，自由中文短语（"学费""通行证"）BM25 命中 0。所以词法臂只当**精确词/数字/英文增强器**用，中文仍靠向量臂；要真做中文词法得在入库侧上 jieba/IK 分词或中文分析器（未来项）。
- **实测证据**：`/api/agent/retrieve?hybrid=true|false` A/B——把向量池压到 3，开 hybrid 后有块以 `vec=-1 lex=1` 被纯词法补召回，证明融合确实扩了召回、非摆设。

### 7. Guardrails / 内容安全 ⭐（生产必接，demo 不急）
- **治什么**：prompt 注入、敏感输出、合规。当前只有系统提示约束 + 调用上限。
- **选型**：输入侧 NeMo Guardrails / Llama Guard（审核模型）；输出侧国内生产接内容安全服务（阿里云等）。
- **接哪里**：ChatClient 的 **advisor 链**加输入/输出校验节点——架构上正好是 advisor 的主场。

### 8. Langfuse Datasets + Experiments ⭐⭐ ✅ 已落地（gold 集进库 + 数据集实验 + 逐条 trace 打分）
- **治什么**："gold 集和指标手抄在 README/代码里会漂"——已解决：6 题 gold（源自 phase6 EvalService.CASES）灌进 Langfuse 数据集 `rdx-gold`，按 `id` upsert 幂等、版本化。
- **做法**：`run-langfuse-eval.py`（纯 stdlib，Basic pk:sk）→ `seed` 建集灌条；`run <runName>` 逐条问 rdx-agent `/chat`，回写 `trace-create`(ingestion) + `dataset-run-items` + `scores`(NUMERIC)。每 run 在 UI 并排可对比分数。与 phase6 gate 互补：gate 管"改没改坏（中位数阈值 pass/fail）"，Datasets 管"分数随版本历史 + 每条能点开看 trace"。
- **⚠️ 实测抓到的评测陷阱（价值就在这）**：第二次跑同样 6 题，`keyword_hit 6/6` 但 `tool_correct 0/6`、`tools=[]`——因为**语义缓存持久化了答案，同题重跑命中缓存、整条 LLM+工具链被跳过**，工具轨迹自然空。修：给 `/chat` 加 `nocache=true`（跳过缓存读写），评测恒走真实链路；加完同题重跑 `tool_correct` 稳 6/6。**教训：评测必须绕开缓存/上下文污染，否则量的是缓存不是系统**——和 phase6"先怀疑尺子"同源。
- **补了对抗题（这才是能测回归的关键）**：`rdx-gold` 从 6 题扩到 13 题——加 `similar`(硕士/博士条件近义、初试非统考)、`exact`(报名1月、学费8000、推荐信数)、`multihop`(清华有哪些学院→search_schools+list_colleges)、`refuse`(博士学费语料没有→该"以公示为准"别编)。打分器读回 Langfuse 的 expectedOutput（集=唯一真相源）+ `expect_absent` 反幻觉 + **按类型汇总通过率**。**又抓一次"尺子骗人"**：`每年报名几月` 判 FAIL 其实是答案里"1 月"带空格、`expect="1月"`没匹配上——归一化去空格后 exact 3/3。最终对抗集 pipeline 13/13（说明检索/路由/精排在易题上本就没问题，别硬吹 precision）。
- **`cachetrap` 子命令 + 两轮自我纠错（很能说明"先怀疑测错"）**：先问 A 写缓存、再问近义 B 测误命中。初版误判"省份↔城市串味"（实为两条 B 同文本自命中 + DB city 就返回浙江）。改用"**Q=E原句+追加另一维度子句**"造近重复后，**在 0.92 真碰撞并实测护栏生效**：关护栏 `同济大学学费…在哪个城市` 命中了"只答学费"的缓存、丢了"上海"（有害）；开护栏 `guardRejects=1`、走真实链路把"上海+学费"答全。已把这对固化进 `CACHE_PAIRS`。**方法论**：一个"发现的 bug"先怀疑测错、再造可控数据证伪/证实——和 precision 假象、裁判假阴性同一课。
- **基础设施**：Langfuse v3 自托管（`observability/langfuse/`，web 3000），零新增镜像。契约从运行实例的 `/generated/api/openapi.yml` 现查（v3.225.7），没照文档猜。

## 三、可观测类

### 9. OTel Collector + Prometheus + Grafana ⭐
- 现状：`/actuator/prometheus` 已开、OTel span 已进 Langfuse。补 Collector（统一路由/采样）+ Prometheus 抓取 + Grafana 面板（延迟/成本/命中率/门禁通过率大盘）。需新镜像，受 Docker Hub 限制择机。

### 10. 编排框架（phase7 用）
- **LangGraph4j** / **Spring AI Alibaba Graph**：多智能体 supervisor 的 Java 生态对应物，不必非 Python。

## 四、优先级与路线

| 优先 | 中间件 | 治的实测问题 | 新增基础设施 |
|---|---|---|---|
| ✅已做 | RAG 向量库迁 Redis Stack | 内存库重启即丢、不可共享 | 已完成：`redis-stack-server:6380` + `rdx-rag-index` |
| ✅已做 | 语义缓存迁 Redis Stack FT | 现缓存进程内、重启丢 | 已完成：同实例独立索引 `rdx-cache-index`（JSON+HNSW+TTL），跨进程持久化实测通过 |
| ✅已做 | 会话记忆迁 Redis | InMemory ChatMemory 重启丢 | 已完成：自建 `RedisChatMemoryRepository`(`rdx:chat:*`+TTL)接 `MessageWindowChatMemory`；重启后 `/history` 仍在、追问靠记忆实测通过 |
| ✅已做 | Rerank API（qwen3.7-text-rerank） | 精排/剔噪，非 precision 跳升（见#5诚实边界） | 已完成：rag_search 宽池→rerank→top4，/retrieve A/B |
| ✅已做 | 混合检索（向量+BM25 RRF→rerank） | 精确词/数字召回（中文受分词限制，见#6边界） | 已完成：`LexicalRetriever`+RRF，/retrieve?hybrid A/B 扩召回实证 |
| ✅已做 | Langfuse Datasets | gold 集漂移 + 评测污染 | 已完成：`rdx-gold` 入库+`run-langfuse-eval.py` 跑实验；`nocache` 绕缓存 |
| ✅已做 | resilience4j 换手写舱壁/熔断 | 手写熔断缺窗口/半开/指标 | 已完成：Bulkhead 换 Semaphore + CircuitBreaker 护 LLM；shedding+状态机实测 |
| ◑部分 | Higress AI 网关 | 统一入口/路由/鉴权 | 已跑起来+脚本化配了服务来源/重写路由，agent 走网关实测通过；限流/多厂商兜底留 UI |
| ✅已做 | 调试端点鉴权 | /history·/trace·/retrieve 裸奔泄内部态 | 已完成：`X-Admin-Token` 拦截器（/chat·/stream 属产品API另议），实测 401/200 |
| ✅已做 | 观测脱敏 profile | prompt/completion/工具内容全量入观测=敏感外泄 | 已完成：`application-prod.yml` 关三开关，dev 全量/prod 脱敏，启动 `[obs]` 日志可验 |
| ✅已做 | 自动化测试 | 无测试→回归裸奔 | 已完成：13 单测 + 4 真Redis集成测试(双模式:testcontainer / `-Dredis.it.port=6380`)，本机 17/17 绿；`.github/workflows/ci.yml` CI 跑 |
| 7 | Guardrails | 注入/合规 | 生产再上 |

**共同纪律**：每上一个，先定"用哪个指标证明它有效"（phase6 三路 + `/evalrag` + 命中率），**改前改后各跑一轮、取中位数**——评测闭环就是为这件事准备的。
