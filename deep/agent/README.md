---
title: "AI Agent 项目总览：六个 Phase 的全景地图"
summary: "阶段 1 骨架：Spring Boot + Spring AI Alibaba（DashScope / 通义千问）最小 Agent 循环。 目的不是做产品，是亲手把「模型 → 选工…"
date: 2026-10-05
author: "小番茄程序猿"
tags:
  - AI Agent
  - Spring AI
category: deep
series: "agent"
slug: "README"
reading_time: 2
featured: false
draft: false
---
# phase1-minimal-agent

阶段 1 骨架：**Spring Boot + Spring AI Alibaba（DashScope / 通义千问）最小 Agent 循环**。
目的不是做产品，是亲手把「模型 → 选工具 → 执行 → 结果回填 → 再决策」这个循环跑通一次。

## 环境

- JDK 17
- Maven 3.9+
- 依赖版本：Spring Boot 3.4.7、spring-ai-alibaba 1.0.0.2（详见 `pom.xml`）

## 跑起来

```bash
# 1) 设 API Key（阿里百炼控制台申请，别写进仓库）
export DASHSCOPE_API_KEY=sk-你的key        # Windows: set DASHSCOPE_API_KEY=sk-xxx

# 2) 编译
mvn -q clean package -DskipTests

# 3) 启动
mvn spring-boot:run
```

## 验证工具调用闭环

```bash
# 流式（观测首字延迟）
curl -N "http://localhost:8080/api/agent/stream?text=帮我算(128%2B64)*3"

# 同步（跨两个工具：先算数、再查订单）
curl -X POST http://localhost:8080/api/agent/chat -H "Content-Type: text/plain" \
  -d "先算 (128+64)*3，再查订单 A1001 状态"
```

打开 `com.alibaba.cloud.ai` 的 DEBUG 日志，就能看到模型两次要求调用 `calculator` / `orderStatus`、结果被回填、最后汇总作答的完整过程。

## 本阶段刻意不做的

- 不做鉴权、不做持久化、不上微服务 —— 只要循环跑通即可。
- 不接真实订单库 —— `orderStatus` 用内存 Map 模拟，阶段 3 再换成真实后端接口。
