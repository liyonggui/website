# 小番茄程序猿 · 个人网站

一个纯静态（HTML / CSS / JS，**零框架、零构建**）的个人作品集 + 技术博客 + 深度技术专栏 + 工程化知识库站点。托管于 GitHub Pages：`https://liyonggui.github.io/website/`。

## 站点栏目

| 页面 | 文件 | 内容 |
| --- | --- | --- |
| 首页 | `index.html` | 技术画像、精选项目、**最近更新**（自动索引）、联系方式 |
| 项目 | `projects.html` | 高并发后端家族 + AI Agent 工程化家族（含实测指标可视化） |
| 博客 | `blog.html` | 多平台连载文章（CSDN / 掘金 / 知乎），渲染后的文章页 |
| 深度 | `deep.html` | 底层原理专栏 104+ 篇，分 MySQL / Redis / ES / 压测 / K8s / Agent 六系列 |
| 标签 | `tags.html` | 按标签筛选全部文章（数据驱动） |
| 归档 | `archives.html` | 按年份时间线浏览 |
| 搜索 | `search.html` | 纯客户端全文检索（标题/摘要/标签/系列） |
| RSS | `rss.xml` | 最新 20 篇订阅源 |

## 内容工程化管线

站点是"内容工程化"的知识库，核心是 **front matter 元数据 + 脚本生成**：

```
.md（含 YAML front matter）
  │  title / summary / date / tags / category / series / reading_time / draft
  ▼
add_front_matter.py   # 给新文章注入元数据（幂等）
gen_articles.py       # 渲染 106 篇带排版/TOC/评论区/暗色模式的文章页
gen_deep.py           # 生成 deep.html 深度专栏索引
generate_index.py     # 生成 data/*.json（文章/标签/归档/搜索索引）+ rss.xml
verify_titles.py      # 全站校验：元数据完整性 + HTML 结构 + 产物对应 + JSON 可解析
```

**新增一篇文章的标准流程**：把 `.md` 放进 `blog/` 或 `deep/<系列>/` → 依次运行 `add_front_matter.py`、`gen_articles.py`、`gen_deep.py`、`generate_index.py`、`verify_titles.py` → 首页最近更新、标签、归档、搜索、RSS 全部自动刷新，无需手改任何索引页。

## 目录结构

```
.
├── index.html / projects.html / blog.html   # 手写页面
├── tags.html / archives.html / search.html  # 数据驱动的发现页
├── deep.html                                # 深度专栏索引（生成）
├── rss.xml / sitemap.xml
├── data/                                    # articles/tags/archives/search_index.json
├── blog/ deep/                              # 文章源（.md 带 front matter）+ 渲染产物（.html）
├── assets/                                  # css（含暗色模式）/ js / img / logo.svg
├── add_front_matter.py gen_articles.py gen_deep.py generate_index.py
├── verify_titles.py                         # 站点校验（CI 同款）
└── .github/workflows/site-check.yml         # push 时自动：生成 → 校验
```

## 本地预览

```bash
python -m http.server 8080
# 浏览器打开 http://localhost:8080
```

> 标签/归档/搜索/首页最近更新通过 `fetch` 读取 `data/*.json`，需经 http 访问（`file://` 下浏览器会拦截 fetch）。

## 部署

推送到 `main` 分支即可，GitHub Pages（Settings → Pages → main / root）自动发布；GitHub Actions 会先跑一遍「生成 + 校验」。

## 评论区（Giscus）

全站文章页底部基于 [Giscus](https://giscus.app)（GitHub Discussions，无广告、无追踪），已启用。

## 技术说明

- 纯静态、相对路径、无框架；样式体系见 `assets/css/style.css`（设计 token + 暗色模式 `[data-theme=dark]`）。
- 文章元数据存于每个 `.md` 的 YAML front matter；`draft: true` 的文章不进任何索引。
- CI（`.github/workflows/site-check.yml`）在每次 push 时自动重建并校验，防手滑。
