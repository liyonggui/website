---
title: "RAG 代码与原理详解（phase2 深读 · 新手向）"
summary: "配套 AI-Agent新手教学.md 第 3 课的深挖版。对照 D:\\ai-agent-project\\phase2-rag 的代码读。 > 目录：① LinkedHashMap …"
date: 2026-10-11
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
  - RAG
  - 源码
category: deep
series: "agent"
slug: "RAG代码与原理详解"
reading_time: 12
featured: false
draft: false
---
# RAG 代码与原理详解（phase2 深读 · 新手向）

> 配套 `AI-Agent新手教学.md` 第 3 课的深挖版。对照 `D:\ai-agent-project\phase2-rag` 的代码读。
> 目录：① LinkedHashMap vs HashMap ② phase2 逐层 ③ Document ④ SimpleVectorStore ⑤ embedding 原理 ⑥ 换 RedisVectorStore ⑦ 混合检索+rerank

---

## ① 为什么用 LinkedHashMap 不用 HashMap
- `LinkedHashMap` 保持 **put 的插入顺序**；`HashMap` 键顺序按哈希散列、**不固定**。
- 这里的 Map 会被 Jackson 转成 JSON 返回，用 LinkedHashMap 让输出键序**稳定可读**（`question→answer→citations`、每条引用 `idx→source→score→snippet`），利于调试/评测 diff。
- 成本：LinkedHashMap = HashMap + 一条记录插入顺序的双链表，get/put 仍 O(1)。
- 选法：不关心顺序→HashMap；要插入顺序(输出/日志)→LinkedHashMap；按键排序→TreeMap。

## ② phase2 逐层（从 RagController 往下）
标准 Spring Boot 分层：**Controller(HTTP) → Service(业务) → Config(装配Bean) → Ingestor(启动灌数据) → yml(配置)**。

**RagController**（薄，只翻译 HTTP）：`@RestController`+`@RequestMapping("/api/rag")`；`@GetMapping("/ask")` 把 `q` 转交 `RagService`；返回 `Map` 自动序列化成 JSON。`RagService` 靠**构造注入**（Spring 的 IoC/DI），不自己 new。

**RagService**（RAG 核心）：持有 `VectorStore`(检索) + `ChatClient`(对话)，构造里 `builder.defaultSystem("只能依据资料回答…")` 预设约束。
- `retrieve(q,topK)`：`vectorStore.similaritySearch(SearchRequest.builder().query(q).topK(k).similarityThreshold(0.0).build())` → 返回最近的 topK 个 `Document`。
- `ask(q)` 三段：① retrieve 拿 4 块 → ② 拼 `【资料N｜来源:x】正文` 上下文 + 攒引用清单 refs → ③ `chatClient.prompt().user("只根据资料回答并标(资料N)…"+ctx+问题).call().content()`。返回 `{question,answer,citations}`。
- `eval()`：造带"期望关键词"的题，只看关键词**在不在检索到的原文里**（不依赖生成→确定性、可复现，量的是"检索好不好"）。

**RagConfig**：`@Bean VectorStore = SimpleVectorStore.builder(embeddingModel).build()`。`embeddingModel` 由 spring-ai openai starter 按 yml 自动配。

**KnowledgeIngestor**：`implements ApplicationRunner`，启动后读 `classpath:kb/*.md` → 按空行切段 → 每段 `Document.builder().text(chunk).metadata({source:文件名}).build()` → `vectorStore.add(全部)`（触发 embedding 网络调用，故启动慢几秒）。日志"入库完成：10 个语料块"就是它。

**一次 /ask 流转**：curl → RagController.ask → RagService.ask →(similaritySearch: embed(q)→算余弦→排序取topK→挂score)→拼上下文→chatClient.call(kimi)→Map→JSON。（库里 10 块是启动时 Ingestor 灌的。）

## ③ Document（Spring AI 自带类）
`org.springframework.ai.document.Document` = 一个文本块的标准载体，三样：`text`(正文 getText())、`metadata`(Map，存 source 等)、`score`(相似度 Double)。
- **score 不是入库时有的、也不是 Document 自己算的**：入库时 builder 没设→null；**只有 `similaritySearch` 返回时，向量库现算相似度挂上去**。
- 版本坑：1.0 取正文用 `getText()`（更早叫 `getContent()`）。

## ④ SimpleVectorStore 内部（score 从哪来）
= 内存两张表 + 每次搜索暴力遍历算余弦。
- 存：`documents: id→Document`、`embeddings: id→double[]`（它自己不向量化，调 `EmbeddingModel`）。
- `add(docs)`：每块 `embeddingModel.embed(正文)`→存向量。
- `similaritySearch(req)`：① `embed(query)`→问题向量 ② 遍历所有已存向量算**余弦相似度** ③ 排序取 topK ④ 给每个 Document 挂 `score`。
- 余弦：比**方向夹角**（忽略模长），值∈[-1,1]，越接近1越像；内部算"余弦距离=1−相似度"，`score=1−距离=相似度`（越大越相关，约0~1）。
- **不是关键词匹配**：字面不同意思近能召回，字面一样意思不同可能不召回。
- `similarityThreshold(0.0)`=不过滤只取topK；生产常设 0.2~0.4 去噪。
- **暴力 O(N)**：每次扫全部向量。10块没事，百万块慢死 → 生产换带 ANN 索引的（HNSW/IVF）。

## ⑤ embedding 为什么能把语义变成方向
不是魔法，是喂海量文本训出来的几何：
- **训练目标决定"意思近→方向近"**：一类是"预测上下文"(词/句的意思由它常出现的语境定义)；一类是"对比学习"(把近义句拉近、不相关推远，做句子向量最常用)。训完，语义距离≈向量夹角。
- **向量算术**(国王−男人+女人≈女王)：某些语义属性训成了大致固定的方向。⚠️ 是近似示意，真实高维空间嘈杂，不是每个词都能精确加减。
- **用余弦(方向)不用模长**：长度受文本长短干扰，方向才承载语义。
- **决定 RAG 上限**：检索好不好，天花板是 embedding 对你领域语义的掌握。它没学过你的黑话(如"港澳台联考""院系所")就会偏 → 所以需要混合检索/rerank/微调 embedding 来补。

## ⑥ 生产换 RedisVectorStore：改哪些
核心：**业务代码面向 `VectorStore` 接口，换实现基本只动 依赖+Bean+配置，RagService/KnowledgeIngestor 零改动**。
1. pom 加 `org.springframework.ai:spring-ai-starter-vector-store-redis`（版本由 spring-ai BOM 管；接口包 spring-ai-vector-store 保留）。
2. 基础设施必须是 **Redis Stack**（带 RediSearch 向量模块，普通 redis 不行）：`docker run -d -p 6379:6379 redis/redis-stack-server:latest`。
3. yml：`spring.data.redis.host/port` + `spring.ai.vectorstore.redis.initialize-schema=true` + `index`/`prefix`。
4. Bean：把 `SimpleVectorStore.builder(...)` 换成 `RedisVectorStore.builder(...).embeddingModel(同一个).indexName/prefix/initializeSchema(true).build()`；或直接用 starter 自动配的、删掉自己的 @Bean。EmbeddingModel 不变。
5. 业务代码不改（retrieve/add 面向接口）。
6. 注意：索引向量维度要和 embedding 一致(1024)，换 embedding 要重建索引；metadata 想按 source 过滤要声明为可索引字段；生产设 similarityThreshold+过滤；Redis 顺带解决"重启即失"和暴力 O(N)。
- 替代：PgVector(Postgres+pgvector，运维熟)、Milvus/Qdrant(大规模)。同套路：加 starter+换 Bean+配连接。

## ⑦ 混合检索 + rerank（补 embedding 短板）
**embedding 单用的毛病**：精确词/稀有词/专名/数字(如 `02:00`、`string2ll`、`t_string.c`)拉不准；只认"像不像"不认"到没到位"→ 召回一堆主题相关但不答这题的块（=你 precision@4=0.55 的根因）。

**药一 混合检索(向量+BM25)**：向量强在语义/换说法，BM25 强在精确词/稀有词/数字，能力互补。两路各召回一批 → **RRF 倒数排名融合**。例：问 string2ll，向量给泛块、BM25 精确命中含该词的块 → 融合后排上来。

**药二 rerank(精排)**：
- 召回用 **bi-encoder**：query 和 doc 各自独立编码，快但糙。
- rerank 用 **cross-encoder**：把 query+候选doc 拼一起逐对精算相关性，准但贵。
- 标准两阶段：`召回(向量+BM25) top20 → rerank 精排 → 取 top4 喂 LLM`。你 MaaS 空间有 `qwen3.7-text-rerank`。
- 效果：召回20块里只3块真相关，rerank 把3块顶到前3、只取top3 → precision 逼近1。

**诚实边界**：你那个 toy 语料 **MRR 已 1.0**(答案块排第一)，precision@4 低很大程度是"答案只在1块却保留4块"的**指标假象**。所以：小库/答案集中→先收紧 topK+阈值就够；大库/答案分散/有精确词→混合检索+rerank 才是刚需。**先判断问题属于哪种，再决定上不上重武器**（别为显先进堆架构）。

**落地结构**：`Query → 向量topK20 + BM25 topK20 → RRF融合 → cross-encoder rerank → top4 → 喂LLM`。Spring AI 有模块化 RAG(`RetrievalAugmentationAdvisor`/`DocumentRetriever`/`QueryTransformer`查询改写/rerank)；BM25 生产向量库(Redis/PgVector)自带混合，不用手搓。

---
**一句话总纲**：embedding 把语义压成方向、余弦量像不像(RAG地基与天花板)；SimpleVectorStore 是内存暴力版，生产换 RedisVectorStore 只动依赖+Bean+配置；混合检索补精确词、rerank 补排序精度，但先分清"系统差 vs 指标/数据特性"再决定上不上重武器。
