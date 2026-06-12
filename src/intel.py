"""情报区 CLI：维护 AI 选手可选择性读取的赛事情报库。

情报来源是人工（或战报工作流）收集的真实信息：伤病、首发、状态、剧情线。
公共区只展示索引，选手用 read_intel 申请全文——求知有成本，信息有价值。

用法：
  python3 -m src.intel add "标题" "正文..." [--source 来源]
  python3 -m src.intel add "标题" --file intel.txt
  python3 -m src.intel list
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import db


def main() -> None:
    parser = argparse.ArgumentParser(description="情报区管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("title")
    p_add.add_argument("content", nargs="?")
    p_add.add_argument("--file", help="从文件读正文")
    p_add.add_argument("--source", default="")
    sub.add_parser("list")

    args = parser.parse_args()
    db.init_db()
    if args.cmd == "add":
        content = (Path(args.file).read_text(encoding="utf-8")
                   if args.file else args.content)
        if not content:
            parser.error("需要正文（位置参数或 --file）")
        iid = db.intel_add(args.title, content, args.source)
        print(f"  情报 #{iid} 已入库: {args.title}")
    else:
        for it in db.intel_index(50):
            print(f"  #{it['id']} [{it['date']}] {it['title']}")


if __name__ == "__main__":
    main()
