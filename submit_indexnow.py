# -*- coding: utf-8 -*-
"""
IndexNow 提交脚本：把 sitemap.xml 全部 URL 一次性推给 IndexNow（Bing/Yandex 等）。
用法：
  1. 先确保密钥文件 <key>.txt 已 push 上线（GitHub Pages 部署完成）
  2. python submit_indexnow.py
Google 不支持 IndexNow，本脚本只服务 Bing 生态，与 GSC 队列互不干扰。
"""
import io
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent

KEY = "29a3cecaa50d3380ab6badd1003d3c6b"
KEY_FILE = KEY + ".txt"
HOST = "liyonggui.github.io"
KEY_LOCATION = f"https://{HOST}/website/{KEY_FILE}"
ENDPOINT = "https://api.indexnow.org/indexnow"


def main():
    key_path = ROOT / KEY_FILE
    if not key_path.exists():
        print(f"密钥文件 {KEY_FILE} 不存在")
        raise SystemExit(1)
    if key_path.read_text(encoding="utf-8").strip() != KEY:
        print(f"密钥文件内容与 KEY 不一致")
        raise SystemExit(1)

    tree = ET.parse(ROOT / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text for loc in tree.getroot().findall("s:url/s:loc", ns)]
    if not urls:
        print("sitemap.xml 中没有 URL")
        raise SystemExit(1)
    print(f"待提交 URL：{len(urls)} 个")

    payload = {
        "host": HOST,
        "key": KEY,
        "keyLocation": KEY_LOCATION,
        "urlList": urls,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"IndexNow 响应：HTTP {resp.status} {resp.reason}")
            body = resp.read().decode("utf-8", errors="ignore").strip()
            if body:
                print(body)
    except urllib.error.HTTPError as e:
        print(f"IndexNow 拒绝：HTTP {e.code} {e.reason}")
        print(e.read().decode("utf-8", errors="ignore")[:500])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
