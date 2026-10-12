---
title: "Phase 5 · MCP 代码与原理详解（server + client · 含失控案例）"
summary: "配套：AI-Agent新手教学.md 第 6 课、全链路.md。代码在 phase5-mcp-server 与 phase5-mcp-cli…"
date: 2026-09-23
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
  - MCP
  - 源码
category: deep
series: "agent"
slug: "phase5-MCP代码与原理详解"
reading_time: 10
featured: false
draft: false
---
# phase5 MCP 代码与原理详解（server + client · 含失控案例）

> 配套：`AI-Agent新手教学.md` 第 6 课、`全链路.md`。代码在 `phase5-mcp-server` 与 `phase5-mcp-client` 模块。
> 一句话：**把"工具"从 Agent 代码里搬进协议里**——server 声明能力，client 运行时发现，两端解耦。

## 0. 为什么是独立服务（不是内嵌后端）
后端框架 是 Spring Boot **3.2.10**，Spring AI（含 MCP starter）要求 **3.4+/1.1.x**——版本硬冲突。独立 MCP Server 用 RestClient 代理后端 REST，边界干净，也是"适配器模式"的活例。有人问"为什么不塞进老后端"，这就是答案。

## 1. 文件总览

| 端 | 文件 | 职责 |
|---|---|---|
| Server (8085) | `tool/SchoolTools` | 4 个 `@Tool`：search_schools / search_majors / **list_colleges** / list_courses |
| | `config/McpToolConfig` | RestClient(超时) + **ToolCallbackProvider**（发布工具的关键 Bean） |
| | `application.yml` | `protocol: STREAMABLE` + server 名/版本 |
| Client (8086) | `config/AgentConfig` | ChatMemory + ChatClient（`defaultToolCallbacks`＝MCP 发现来的工具） |
| | `tool/RecordingToolCallback` | 装饰器：记轨迹 + **单次请求调用上限**（防失控） |
| | `tool/ToolCallRecorder` | 按 cid 记录工具调用；`overLimit()` 护栏 |
| | `web/AgentController` | /chat(带 memory 开关) /stream /history /trace |
| | `application.yml` | `streamable-http.connections.app.url` + `agent.max-tool-calls: 25` |

## 2. Server 端：发布机制只有一个 Bean
```java
@Bean
public ToolCallbackProvider appToolCallbackProvider(SchoolTools schoolTools) {
    return MethodToolCallbackProvider.builder().toolObjects(schoolTools).build();
}
```
`MethodToolCallbackProvider` 扫描 `@Tool` 方法 → 每个方法转成 `ToolCallback`（名字/描述/**参数 JSON Schema** 由方法签名生成）→ MCP server 自动配置把它们发布成 `tools/list` 的清单。**协议代码（握手/会话/分发）全是 starter 做的，你零代码。** 启动日志 `Registered tools: 4` 即证据。
`SchoolTools` 的方法体与 phase4 相同：RestClient 调 `/wxma/*` + `pick()` 裁字段（**工具返回值全变文本喂模型，裁剪=省 token**）。

## 3. 协议四步（curl 实录）
```
① POST /mcp {"method":"initialize","params":{"protocolVersion":"2025-06-18",...}}
   ← 200 + 头 Mcp-Session-Id + {"serverInfo":{"name":"school-tools","version":"1.0.0"}}
② POST /mcp {"method":"notifications/initialized"}（带 Session 头）  ← 202
③ POST /mcp {"method":"tools/list"}   ← 工具名+描述+参数 schema（= @Tool 的 description 穿透协议）
④ POST /mcp {"method":"tools/call","params":{"name":"search_schools","arguments":{"city":"北京"}}}
   ← event:message data:{"result":{"content":[{"type":"text","text":"[{…清华…}]"}],"isError":false}}
```
要点：**JSON-RPC 信封**；**Mcp-Session-Id** 是会话钥匙；arguments 反序列化为方法入参、返回值序列化为 `content[].text`；工具异常以 `isError:true` 协议内冒泡；响应可 JSON 或 SSE 帧（Streamable HTTP 的"二态"）。

## 4. Client 端：与 phase3 只差一行
```java
ToolCallback[] wrapped = Arrays.stream(mcpTools.getToolCallbacks())
        .map(tc -> new RecordingToolCallback(tc, recorder))   // 包一层：记轨迹+上限
        .toArray(ToolCallback[]::new);
return builder.defaultToolCallbacks(wrapped)...               // phase3 是 defaultTools(@Tool Bean)
```
**从"编译期写死"变成"运行时发现"**，模型/循环/记忆/Controller 全不变。
yml：`spring.ai.mcp.client.streamable-http.connections.app.url`（**前缀是 `streamable-http` 不是 `streamable`**）+ `toolcallback.enabled: true`。
⚠️ **急切初始化**：client 启动时就握手+拉清单，**server 没起 client 直接启动失败**（`Client failed to initialize by explicit API call`）。

## 5. 失控案例与修复（本文件最值钱的一节）
问："中山大学有哪些学院？**请尽量列全**"——当时 server 没有"列学院"工具：
- **失控**：模型拿 `search_majors` 把能想到的专业关键词**穷举 100+ 次**（计算机/医学/考古/口腔…），180s 超时无答案，成本爆炸。
- **根因三条**：① 工具清单缺能力，模型硬凑；② **spring-ai 1.1.8 的工具循环没有 maxIterations 上限**（javap 反编译确认），开放式指令无兜底；③ 终答评测看不见过程——只有 trajectory 能抓到。
- **修复三处**：① server 补 `list_colleges(schoolId)`（后端 `/wxma/colleges` 本来就有）；② client `RecordingToolCallback` 加**单次请求硬上限**（默认 25，超限返回 `toolCallLimitReached` 停止指令让模型收敛）；③ 系统提示"学院用 list_colleges 一次查全，禁止穷举"。
- **前后对比**：100+ 次/180s 无答案 → **2 次调用（search_schools 133ms → list_colleges 79ms）、10.6s、完整学院列表 20+ 个**。

## 6. 测试矩阵（全部实测）
| 实验 | 结果 |
|---|---|
| 复合问题（中大排名/省/学院/专业） | 2 次调用 42.8s，四问全答；工具仅 90ms，**99.8% 耗时在 LLM 轮次** |
| 缩写歧义（中南大学） | 按全名正确查询（湖南/27 名） |
| 域外要求（列全学院） | 修复前穷举失控 → 修复后 2 次收敛 |
| 不存在的大学（亚特兰蒂斯） | 先查后答"不存在"，**未编造业务数据**；顺带补常识背景（严格场景应约束） |

## 7. 坑清单
1. `streamable-http` 前缀写成 `streamable` → 0 连接 → 0 工具 → **模型臆造假工具名**（`functions.search_university_info` + `<|tool_calls|>` 原文）；
2. client 急切初始化：server 挂了 client 起不来；
3. spring-ai 1.1.8 工具循环无上限 → 必须应用层自建（上限+停止指令）；
4. ChatMemory **不存中间 tool 消息**（只见 USER+ASSISTANT）→ 轨迹要自建 RecordingToolCallback + /trace；
5. 中文测试：Git Bash `curl -d 中文` 按 GBK 发出 → 一律 UTF-8 文件 `--data @file`。

## 8. 诚实边界
- server 无鉴权（本机演示可以；暴露必须 OAuth/API-Key——MCP"输在安全层"）；
- SYNC 同步执行，工具慢占线程（phase4 的超时/熔断思路应搬到 server 侧）；
- 上限是"软收敛"（返回停止指令），不是框架级硬中断；
- 评测集小样本，数字当机制演示不当基准。
