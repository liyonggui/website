---
title: "phase4 护栏代码与原理详解（可观测·降级·舱壁·熔断·缓存）"
summary: "配套：AI-Agent新手教学.md 第 7 课。代码在 D:\\ai-agent-project\\phase4-agent-ops。 > 一句话：把 LLM 当不靠谱下游服务，用后…"
date: 2026-09-22
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
  - 源码
  - 缓存
category: deep
series: "agent"
slug: "phase4-护栏代码与原理详解"
reading_time: 9
featured: false
draft: false
---
# phase4 护栏代码与原理详解（可观测·降级·舱壁·熔断·缓存）

> 配套：`AI-Agent新手教学.md` 第 7 课。代码在 `D:\ai-agent-project\phase4-agent-ops`。
> 一句话：**把 LLM 当不靠谱下游服务，用后端老本行（超时/降级/限流/熔断/缓存/指标）把它治理成可依赖系统。**

## 0. 文件总览（8 个文件各管一件事）

| 文件 | 职责 |
|---|---|
| `config/AgentConfig` | 带**双超时**的 RestClient + 挂工具的 ChatClient（含降级话术） |
| `ops/AgentService` | **LLM 舱壁**(Semaphore) + token 成本 + 结果埋点 |
| `ops/AgentMetrics` | 延迟/成败/缓存/熔断/token 全量指标 + snapshot |
| `tool/RuidaxingTools` | 3 个 `@Tool`，只管"构造请求+解析"（护栏逻辑已下沉） |
| `tool/ToolGateway` | 所有工具调用的唯一咽喉：缓存→熔断→真调→降级 |
| `tool/Breaker` | CLOSED/OPEN/HALF_OPEN 状态机（手写，生产可换 resilience4j） |
| `ops/OpsController` | /chat /school /eval /snapshot |
| `application.yml` | 超时/舱壁大小/缓存TTL/单价 全 env 可调 |

与 phase3 的差别一句话：phase3 工具直连下游、零保护；**phase4 把所有调用收进一条带护栏的管道**。

## 1. AgentConfig：双超时 + 降级话术
```java
SimpleClientHttpRequestFactory rf = new SimpleClientHttpRequestFactory();
rf.setConnectTimeout(1500);        // 连不上 1.5s 失败
rf.setReadTimeout(timeoutMs);      // 连上但不响应 2.5s 失败
```
**没有超时的 HTTP 客户端是生产事故之源**：下游挂 → 请求各卡 30s → 200 个 Tomcat 线程迅速占光。
系统提示配套一句："若工具返回 unavailable…如实告知用户'该数据源暂时不可用'，不要凭空作答"——**降级机制要配话术约束，模型才懂得如实转述**。

## 2. AgentService：舱壁四设计点
```java
if (!bulkhead.tryAcquire()) { metrics.countOutcome("rejected"); return Result("rejected","系统繁忙"); }
long t0 = System.nanoTime();
try {
    ChatResponse resp = chatClient.prompt().user(q).call().chatResponse();  // 注意不是 content()
    var u = resp.getMetadata().getUsage();                                  // 多拿一层 Usage 算成本
    metrics.addUsage(u.getPromptTokens(), u.getCompletionTokens());
    metrics.recordChatOk(System.nanoTime() - t0);
} catch (Exception e) { metrics.countOutcome("error"); ... }
finally { bulkhead.release(); }
```
- **`tryAcquire` 非阻塞**：抢不到立即 503，**绝不排队**（排队=用户干等+线程照占）。实测 6 并发/上限 3 → 3×503 秒拒 + 2×200（mean 11.8s/max 18.1s）+1 客户端超时。
- **`finally release`**：异常也归还，否则许可泄漏几次后永久 503。
- **`chatResponse()` 而非 `content()`**：为拿 `Usage`（token 数）→ 成本可见（实测 cost.estimated 0.032）。
- **rejected/error 不进延迟分布**：0ms 的拒绝会污染 p50（踩过的坑）。

## 3. ToolGateway：五步顺序（所有工具的咽喉）
```java
if (!br.allowRequest()) { recordBreakerReject; return unavailable("circuit-open"); }  // ① 熔断
if (cache != null) { hit=cache.getIfPresent(key); if(hit!=null){recordCache(hit); return hit;} } // ② 缓存
try { result=loader.get(); br.onSuccess(); recordTool(ok); cache.put(key,result); return result; } // ③④ 真调+写缓存
catch (RuntimeException e) { br.onFailure(); recordTool(fail); return unavailable(e); }             // ⑤ 降级
```
- **失败不写缓存**（坏数据不污染）；
- **降级返回结构化** `{unavailable:true, tool, reason}`——不是抛异常，是让 LLM 看得懂；
- 缓存 `CACHE_TTL_MS<=0` 可关 → 做 A/B 基线。

## 4. Breaker：30 行状态机
CLOSED（放行，连续失败≥5→OPEN）→ OPEN（冷却 5s 内全拒）→ HALF_OPEN（**只放 1 个探针**）→ 成功 CLOSED / 失败再 OPEN。
原理：**用"停手一段时间"换下游喘息**，防"刚缓过来又被积压请求打死"的雪崩。实测：死端口连打 15 次 → 前 5 次真试失败 → OPEN → **11 次 `circuit-open` 秒拒**。

## 5. AgentMetrics：三类指标 + 一个坑
Timer(延迟)/Counter(工具成败·缓存·熔断·结果)/AtomicLong(token 累加+占位单价算成本)；`/actuator/metrics` 可抓。
**坑**：Micrometer `Timer` percentile 单位歧义算出 5.8e12ms → 改**应用内自采 mean/max**，p95/p99 交给 k6。

## 6. OpsController 四端点
| 端点 | 用途 |
|---|---|
| `/chat?q` | 问答（超舱壁→503） |
| `/school?city` | **编排层压测专用**：只走工具→下游→降级，不经 LLM |
| `/eval` | 场景成功率 |
| `/snapshot` | 指标总览 + 熔断实时状态（`breaker.state`） |

## 7. 三条链路（对照实测）
```
正常：/chat →舱壁✓→LLM→工具→[缓存miss→真调→写缓存]→答(带成本)
下游挂：CLOSED真试→超时/拒绝→fail计数→降级；连续5次→OPEN→之后秒拒(实测11次)
并发超限：6并发→3个tryAcquire失败→503秒回(实测3×503)
```

## 8. 压测数字（k6 开环，见 k6/ 目录与 README）
- orchestration 100/s×20s：2000 请求 0 失败，med 5ms；**p95 836ms 长尾在下游非网关**；
- 缓存 A/B：p95 **836→12.34ms**（↓98.5%），命中率 97.7%；
- llm-guardrail 6/s vs 舱壁3：**90.27% 秒挡 503**（k6 把 503 计 failed 属口径，用自定义 Rate）。

## 9. 诚实边界
记忆仍是进程内；舱壁无排队；熔断无探针超时；成本是占位价；缓存无防击穿(single-flight)/主动刷新；k6 阈值"红"是诚实信号不是 bug。
