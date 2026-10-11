# 小番茄程序猿 · 个人网站

一个纯静态（HTML / CSS / JS，**零构建、零依赖**）的个人作品集 + 技术博客 + 深度技术专栏站点。可直接部署到 GitHub Pages，也可本地双击 `index.html` 打开预览。

## 站点栏目

| 页面 | 文件 | 内容 |
| --- | --- | --- |
| 首页 | `index.html` | 技术画像速览、精选项目、博客预览、联系方式 |
| 项目 | `projects.html` | 高并发后端家族（Redis / MySQL / Spring Cloud / ES / K3s / Jeepay / k6 / 秒杀）+ AI Agent 工程化家族（Spring AI phase1→6 + rdx-agent） |
| 博客 | `blog.html` | 多平台连载文章的权威稿（CSDN / 掘金 / 知乎），原文存于 `blog/` |
| 深度 | `deep.html` | 底层原理专栏，105 篇文档分 6 系列：MySQL / Redis / ES / 压测方法论 / K8s / Agent，原文存于 `deep/` |

## 目录结构

```
.
├── index.html        # 首页
├── projects.html     # 项目
├── blog.html         # 博客
├── deep.html         # 深度技术专栏（由 gen_deep.py 生成）
├── assets/
│   ├── css/style.css # 全局样式（极简、浅色、响应式）
│   ├── css/deep.css  # 深度专栏专属样式
│   └── js/comments.js# Giscus 评论区加载器
├── blog/             # 博客文章 Markdown 原文
├── deep/             # 深度专栏文档（6 系列子目录）
├── gen_deep.py       # 扫描 deep/ 自动生成 deep.html
└── fix_agent.py      # 工具脚本
```

## 本地预览

直接双击 `index.html` 即可；或启动一个静态服务器：

```bash
python -m http.server 8080
# 浏览器打开 http://localhost:8080
```

> 博客与深度专栏以 Markdown 原文形式提供，点击会打开对应的 `.md` 文件。

## 部署到 GitHub Pages

1. 推送到仓库默认分支（`main`）：

   ```bash
   git add .
   git commit -m "personal site"
   git push -u origin main
   ```

2. 仓库 **Settings → Pages → Source** 选择 `main` 分支、`/root` 目录。
3. 稍等片刻，访问 `https://<用户名>.github.io`。

## 评论区（Giscus）

全站底部「交流区」基于 [Giscus](https://giscus.app)（GitHub Discussions，无广告、无追踪）。启用步骤：

1. 仓库开启 **Discussions**（Settings → General）。
2. 安装 **Giscus App** 并授权本仓库。
3. 在 [giscus.app](https://giscus.app) 生成片段，把 `repo-id` / `category-id` 填入 `assets/js/comments.js` 顶部的 `CONFIG`。

未配置时评论区显示提示文字，不影响页面正常展示。

## 技术说明

- 纯静态、相对路径，GitHub Pages / 任意静态托管均可托管。
- 无前端框架、无构建步骤，改完即生效。
- 深度专栏文档通过 `gen_deep.py` 自动索引，向 `deep/` 新增 `.md` 后重跑脚本即可刷新 `deep.html`。
