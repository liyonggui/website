---
title: "补测B4：SQL 注入与安全治理"
summary: "高并发系统安全必修。SQL 注入是最高危的漏洞之一：字符串拼接 SQL，恶意入参可绕过 WHERE 泄露全表、绕过认证、删库。治理核心：参数化根治 + 纵深防御（字段最小化、脱敏、…"
date: 2026-05-16
author: "小番茄程序猿"
tags:
  - MySQL
category: deep
series: "mysql"
slug: "补测B4-SQL注入与安全治理"
reading_time: 4
featured: false
draft: false
---
# 补测B4：SQL 注入与安全治理

## 一、问题背景

高并发系统安全必修。**SQL 注入**是最高危的漏洞之一：字符串拼接 SQL，恶意入参可绕过 WHERE 泄露全表、绕过认证、删库。治理核心：**参数化根治 + 纵深防御**（字段最小化、脱敏、最小权限、WAF）。

## 二、注入原理与防护

```sql
-- 危险拼接：WHERE username = 'input'
SELECT * FROM t_user WHERE username = 'user' OR '1'='1'   -- ' OR '1'='1 使条件恒真，全表泄露
SELECT * FROM t_user WHERE username = 'user'-- '          -- '-- 注释掉后续，绕过认证

-- 安全参数化：? 占位，注入字符被当作普通字符串
SELECT * FROM t_user WHERE username = ?   -- ' OR '1'='1 查不到任何行，攻击失效
```

## 三、演示接口

```bash
# 1. 初始化安全演示用户
curl "http://localhost:8080/sql-safe/init?rows=100"

# 2. 注入漏洞演示（拼接SQL，注入 ' OR '1'='1 泄露全表）
curl "http://localhost:8080/sql-safe/vuln?input='%20OR%20'1'='1"

# 3. 参数化防护（同一注入载荷，攻击失效）
curl "http://localhost:8080/sql-safe/param?input='%20OR%20'1'='1"

# 4. 敏感字段脱敏（手机号/身份证打码）
curl "http://localhost:8080/sql-safe/masking"

# 5. SELECT * 泄漏与字段最小化
curl "http://localhost:8080/sql-safe/select-star"

# 6. 安全治理建议
curl "http://localhost:8080/sql-safe/governance"
```

## 四、实测结果

| 环节 | 输入 | 结果 |
|------|------|------|
| 注入漏洞（拼接SQL） | `' OR '1'='1` | **泄露 100 行**（WHERE 恒真，全表返回） |
| 参数化防护（PreparedStatement） | 同一载荷 | **返回 0 行**（注入被当作普通字符串，攻击失效） |
| 敏感脱敏 | — | 手机号 `138****0000`、身份证 `110***********0000` |
| SELECT * | — | 泄露 password/id_card 等敏感列；应只查必要字段 |

**核心对比**：同一注入载荷 `' OR '1'='1`，拼接 SQL 泄露全表 100 行，参数化后返回 0 行 → **PreparedStatement 参数化根治注入**。

## 五、安全治理方案

1. **根治**：全部用 PreparedStatement 参数化，禁止字符串拼接 SQL。
2. **字段最小化**：SELECT 显式列，不用 `SELECT *`，避免敏感列泄露。
3. **敏感脱敏**：手机号/身份证/密码查询时脱敏，密码只存 hash。
4. **最小权限**：应用账号按需授权（SELECT/INSERT/UPDATE），不滥用 root。
5. **输入校验**：长度/白名单校验，限制注入载荷边界。
6. **WAF/过滤**：网关层过滤 SQL 注入特征关键字。
7. **审计**：开启 general_log/审计，追踪可疑 SQL。

## 六、要点速览

> **SQL 注入治理**：字符串拼接 SQL 使恶意入参（如 `' OR '1'='1`、`'--`）绕过 WHERE 泄露全表或绕过认证。实测同一载荷拼接 SQL 泄露 100 行、参数化后 0 行。根治用 PreparedStatement 参数化，纵深防御：字段最小化（不用 SELECT *）、敏感脱敏、最小权限、WAF 过滤、输入校验。
