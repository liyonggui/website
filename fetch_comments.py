# -*- coding: utf-8 -*-
"""
fetch_comments.py —— 拉取全站 Giscus 评论

原理：Giscus 把每个页面的评论存为仓库 Discussions（mapping=pathname，
讨论标题即页面路径）。本脚本通过 GitHub REST API 分页拉取全部讨论与
评论（含嵌套回复），映射回站内页面，输出：
  - data/comments.json   结构化数据（供后续程序使用）
  - data/comments.md     人类可读的评论报告

用法：
  python fetch_comments.py            # 匿名拉取（公开仓库即可，60 次/时限速）
  GITHUB_TOKEN=xxx python ...         # 带 token 提升限速（私有仓库必须）

说明：评论区数据源在 GitHub，本脚本只读，不改动任何 Discussions。
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OWNER, REPO = "liyonggui", "website"
CATEGORY_SLUG = "announcements"          # comments.js 里配置的 Giscus 分类
API = "https://api.github.com"
SITE_PREFIX = f"/{REPO}/"                # pathname 映射的讨论标题形如 /website/xxx.html

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


def api(url):
    """GET 一个 API 地址，自动分页返回完整列表。"""
    items, url = [], url
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "fetch-comments-script"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            items.extend(json.loads(resp.read().decode("utf-8")))
            link = resp.headers.get("Link", "")
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1:part.find(">")]
        if url:
            time.sleep(0.3)  # 温和限速
    return items


def page_of(disc):
    """讨论标题（页面路径）→ 站内相对路径，映射不上的原样返回。"""
    title = disc.get("title", "")
    if title.startswith(SITE_PREFIX):
        return urllib.parse.unquote(title[len(SITE_PREFIX) - 1:])
    return title


def main():
    discussions = api(
        f"{API}/repos/{OWNER}/{REPO}/discussions?per_page=100")
    out, total_comments = [], 0
    for d in discussions:
        if (d.get("category") or {}).get("slug") != CATEGORY_SLUG:
            continue
        comments = api(
            f"{API}/repos/{OWNER}/{REPO}/discussions/{d['number']}/comments?per_page=100")
        cmts = []
        for c in comments:
            author = ((c.get("user") or {}).get("login")) or "ghost（已注销）"
            # 嵌套回复
            replies = api(
                f"{API}/repos/{OWNER}/{REPO}/discussions/{d['number']}"
                f"/comments/{c['id']}/replies?per_page=100")
            cmts.append({
                "author": author,
                "created_at": c.get("created_at"),
                "body": c.get("body", ""),
                "url": c.get("html_url"),
                "replies": [
                    {
                        "author": ((r.get("user") or {}).get("login")) or "ghost（已注销）",
                        "created_at": r.get("created_at"),
                        "body": r.get("body", ""),
                        "url": r.get("html_url"),
                    } for r in replies
                ],
            })
        total_comments += len(cmts)
        out.append({
            "page": page_of(d),
            "title": d.get("title"),
            "number": d["number"],
            "url": d.get("html_url"),
            "created_at": d.get("created_at"),
            "comment_count": len(cmts),
            "comments": cmts,
        })

    # 排序：有评论的在前，再按页面路径
    out.sort(key=lambda x: (-x["comment_count"], x["page"]))

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "comments.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人类可读报告
    lines = [f"# 全站评论报告", "",
             f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"评论线程：{len(out)} 个页面 ｜ 评论总数：{total_comments}（含回复）", ""]
    for d in out:
        if not d["comments"]:
            continue
        lines.append(f"## {d['page']}")
        lines.append(f"线程：{d['url']} ｜ {d['comment_count']} 条")
        lines.append("")
        for c in d["comments"]:
            when = (c["created_at"] or "")[:10]
            lines.append(f"- **{c['author']}**（{when}）：{c['body'][:120].replace(chr(10), ' ')}")
            for r in c["replies"]:
                when_r = (r["created_at"] or "")[:10]
                lines.append(f"  - ↳ **{r['author']}**（{when_r}）：{r['body'][:100].replace(chr(10), ' ')}")
        lines.append("")
    (data_dir / "comments.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"讨论线程：{len(out)}（页面映射成功 "
          f"{sum(1 for d in out if d['page'].endswith('.html'))} 个）")
    print(f"评论总数：{total_comments}")
    for d in out:
        if d["comment_count"]:
            print(f"  {d['page']}  ->  {d['comment_count']} 条")
    print("输出：data/comments.json + data/comments.md")


if __name__ == "__main__":
    main()
