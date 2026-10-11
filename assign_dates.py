# -*- coding: utf-8 -*-
"""
一次性工具：把全部文章的 date 按「类型分组 + 随机打散」重写进 front matter。

规则（用户指定，2026-10-11）：
- 分组顺序：blog → deep/mysql → redis → es → bench → k8s → agent
- 时间窗：2026-02-01 至 2026-10-11，按各组文章数占总量比例切分时间片
- 组内按文件名升序，随机取升序日期（场景编号大的日期靠后，符合创作顺序直觉）
- 固定随机种子：重复运行结果完全一致（幂等）
"""
import io
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent
START = date(2026, 2, 1)
END = date(2026, 10, 11)
SEED = 20260201

GROUPS = [
    ("blog", ROOT / "blog"),
    ("mysql", ROOT / "deep" / "mysql"),
    ("redis", ROOT / "deep" / "redis"),
    ("es", ROOT / "deep" / "es"),
    ("bench", ROOT / "deep" / "bench"),
    ("k8s", ROOT / "deep" / "k8s"),
    ("agent", ROOT / "deep" / "agent"),
]

DATE_RE = re.compile(r"^date:.*$", re.M)


def assign():
    rng = random.Random(SEED)
    groups_files = []
    for name, d in GROUPS:
        files = sorted(p for p in d.glob("*.md"))
        groups_files.append((name, files))

    total = sum(len(fs) for _, fs in groups_files)
    total_days = (END - START).days  # 含头不含尾的偏移基数
    changed = 0

    offset = 0
    for name, files in groups_files:
        n = len(files)
        if n == 0:
            continue
        # 该组时间片：按文章数占比切分
        slice_days = total_days * n / total
        base = int(offset)
        # 组内取 n 个升序随机日偏移
        picks = sorted(rng.sample(range(int(slice_days)), n))
        for f, pick in zip(files, picks):
            d = START + timedelta(days=base + pick)
            text = f.read_text(encoding="utf-8")
            new_text, cnt = DATE_RE.subn(f"date: {d.isoformat()}", text, count=1)
            if cnt != 1:
                print(f"!! 未找到 date 行: {f.relative_to(ROOT)}")
                continue
            if new_text != text:
                f.write_text(new_text, encoding="utf-8")
                changed += 1
        offset += slice_days
        print(f"{name}: {n} 篇 -> {START + timedelta(days=base)} ~ {START + timedelta(days=base + int(slice_days))}")
    print(f"共更新 {changed} 个文件")


if __name__ == "__main__":
    assign()
