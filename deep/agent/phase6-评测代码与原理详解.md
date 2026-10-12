---
title: "Phase 6 · 评测代码与原理详解（三路判分·中位数·CI门禁）"
summary: "配套：AI-Agent新手教学.md 第 8 课、phase5-MCP代码与原理详解.md（trajectory 数据来源）。代码在 phase6-eval 模块。"
date: 2026-10-01
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
  - 评测
  - 源码
category: deep
series: "agent"
slug: "phase6-评测代码与原理详解"
reading_time: 8
featured: false
draft: false
---
# phase6 评测代码与原理详解（三路判分·中位数·CI门禁）

> 配套：`AI-Agent新手教学.md` 第 8 课、`phase5-MCP代码与原理详解.md`（trajectory 数据来源）。代码在 `phase6-eval` 模块。
> 一句话：**Agent 输出非确定、没有标准答案——评测就是把"感觉挺对"变成"可量化、可回归、可门禁"的那套机制。**

## 0. 文件总览

| 文件 | 职责 |
|---|---|
| `EvalService` | 三路判分（keyword/judge/trajectory）+ runs 中位数 + 离散度 |
| `EvalController` | `/api/eval/run?judge=&runs=` |
| `gate.sh` | CI 门禁：读中位数，任一维 < 阈值 → exit 1 |
| `application.yml` | 被测 agent 地址、裁判模型（temp=0） |

**它是"外部评测器"**：通过 HTTP 打被测 Agent（默认 `phase5-mcp-client:8086` 的 `/api/a5/chat` + `/api/a5/trace`），等于**端到端测「模型→MCP→后端」整条链**，而不是测某个内部函数。换被测对象只改 `eval.agent-url`。

## 1. 评测集：带"标准答案"的场景
```java
record Scenario(String q, String expect, String expectTool, String expectArg) {}
// 例：("北京排名第一的院校是谁？", "清华", "search_schools", "北京")
```
每题带四个标注：**问题、答案应含的关键事实、应该调的工具、工具参数应含的值**。6 题全部是"只有查库才知道"的事实题（排名/省份/学费）——模型背不出来，答对=真调了工具。

## 2. 三路判分（核心设计）

| 维度 | 怎么判 | 测什么 | 特性 |
|---|---|---|---|
| **keyword** | `answer.contains(expect)` | 终答含不含关键事实 | **确定性**，零成本，CI 回归基线 |
| **trajectory** | 查 `/trace`：期望工具被调了？参数含期望值？ | **过程对不对**（有没有真去查） | **Agent 专属**，keyword/judge 测不出 |
| **judge** | LLM 裁判输出 `{score:0-2, correct, reason}` | 语义对错/完整度/幻觉 | 能分"部分正确"，**但有噪声、还会判错** |

**trajectory 是这套评测的灵魂**：实测抓到过"模型凭常识直答、根本没调工具却答对"（keyword 满分、trajectory 50-67%）——这种"这次蒙对、下次就编"的风险，只有看过程才能发现。

## 3. 裁判的写法（校准前后两版）
```java
// 校准后（关键三条规则）：
"你是严格但克制的评测裁判。只要【Agent回答】正确覆盖【期望要点】且无矛盾即判正确。
 ① 排名/学费等数值以系统数据为准——你无法访问数据库，不得用自身外部知识否定系统数字；
 ② 本系统是『考研择校』，本科等域外信息不扣分；
 ③ 仅明显错误/矛盾/编造时扣分。只输出JSON：{score,correct,reason}"
```
解析做了容错（剥 ```json 围栏、取首尾大括号）——**裁判输出格式也是工程问题**。

## 4. runs 中位数 + 离散度（temp=0 也压不掉的噪声）
```java
public Map<String,Object> run(int runs, boolean useJudge) {
    for (int r = 0; r < runs; r++) {
        for (每个 case) {
            String cid = "eval-" + i + "-r" + r;   // 每轮独立会话，防记忆串味
            ...三路判分...
        }
        samples.add(new double[]{kw/n, tr/n, jr/n, avgScore});   // 本轮各维通过率
    }
    double[] med = medianEach(samples, 4);   // 逐维取中位数
    out.put("median", ...); out.put("spread", minMax per dim); out.put("perRun", ...);
}
```
**为什么必须多轮**：实测 temp=0 下 judge 类指标**仍抖 ±0.05**（perRun 0.86/0.87/0.91）。
**纪律：比 spread 小的差异不算数**——曾用它撤回过"rerank 让 precision 变差"的误判。
**再深一层（04 号文的第三次反转）**：spread 是**组内**方差；跨批次比较要用**组间**方差（实测组间范围是组内的 2.2 倍）。混用会把噪声读成结论、或把结论误判成噪声。

## 5. 评测→指标→门禁（闭环）
```java
counter("keyword", kHit); counter("trajectory", tHit); counter("judge", correct);
// → Micrometer: agent.eval.result{dimension,pass} → /actuator/metrics 可监控
```
`gate.sh`：`RUNS=3 MIN=90` → 读 median 的 `*PassRate` → 任一维低于阈值 **exit 1**（CI 阻断合并）。

## 6. 实测战果（全栈：DB→后端→mcp-server→client→eval）
| 事件 | 数字 |
|---|---|
| trajectory 暴露"凭常识直答不调工具" | 50-67% → 收紧提示词 → **100%**（eval→fix→复测闭环） |
| 门禁稳定报 judge 83%（中位数，非噪声） | 归因：**裁判假阴性**（外部先验否定 DB 真值 + 要求域外"本科"） |
| 校准裁判 rubric（不改系统！） | judge 83% → **100%，门禁 exit 0 转绿** |
**评测不过的正确顺序：先怀疑量具，再怀疑裁判，最后才怀疑系统。**

## 7. 坑清单
1. 每轮必须**独立 cid**（`eval-i-r{r}`）——否则记忆串味，第 2 轮"作弊"知道第 1 轮答案；
2. 裁判 JSON 要容错解析（围栏/前后缀废话）；
3. 慢评测 curl **别设短超时 + 别自动重试**（超时→重试=重复烧调用，踩过）；
4. 门禁读 median 不读单次；跨批次比较用组间方差；
5. trajectory 依赖被测方暴露 `/trace`（phase5 的 RecordingToolCallback）——ChatMemory 不存工具消息。

## 8. 诚实边界
- 6 题小样本：**当回归绊线可以，当基准不行**（要精确就扩 gold 集）；
- 裁判仍是同家族模型，有自我偏好风险——生产要**多裁判/人工抽检校准**；
- 无 Langfuse 式数据集版本化（gold 集还在代码里写死）；
- 门禁阈值一刀切 90%，实际应**分维度设**（keyword 可 100，judge 80 起步）。
