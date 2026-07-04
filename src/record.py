"""手动录入比分（数据源不可用或滞后时的兜底）。

用法：
    python3 -m src.record 1 2-1              # 第 1 场，主队 2:1 获胜
    python3 -m src.record 89 1-1 --winner FRA  # 点球大战需指明晋级方
    python3 -m src.record 86 3-2 --winner ARG --score-type final_aet --settle-score 1-1
    python3 -m src.record --list              # 查看最近已录入

直接写入数据库（source=manual，优先级高于自动抓取）；
录入后跑 `python3 -m src.update --no-fetch` 即可刷新预测。
"""

from __future__ import annotations

import argparse
import sys

from . import db


def main() -> None:
    parser = argparse.ArgumentParser(description="手动录入世界杯比分")
    parser.add_argument("match", nargs="?", type=int, help="场次编号 1~104")
    parser.add_argument("score", nargs="?", help="比分，如 2-1（含加时）")
    parser.add_argument("--winner", help="点球胜者三字码（平局时必填）")
    parser.add_argument("--settle-score", help="投注结算比分，如 1-1；淘汰赛加时/点球时填90分钟比分")
    parser.add_argument("--score-type", default="regular",
                        choices=("regular", "final_aet", "penalties"),
                        help="score 的口径：regular / final_aet / penalties")
    parser.add_argument("--list", action="store_true", help="查看已录入")
    args = parser.parse_args()

    db.init_db()

    if args.list or args.match is None:
        conn = db.connect()
        rows = conn.execute("SELECT * FROM matches WHERE source='manual' "
                            "ORDER BY match_no").fetchall()
        conn.close()
        if not rows:
            print("  尚无手动录入。")
        for r in rows:
            settle = ""
            if (r["settle_score_home"] is not None
                    and r["settle_score_away"] is not None):
                settle = f"  结算 {r['settle_score_home']}-{r['settle_score_away']}"
            print(f"  第{r['match_no']}场: {r['score_home']}-{r['score_away']}"
                  + settle
                  + (f"  点球胜者 {r['winner']}" if r["winner"] else ""))
        return

    if not (1 <= args.match <= 104):
        sys.exit("场次编号需在 1~104")
    try:
        gh, ga = (int(x) for x in args.score.replace(":", "-").split("-"))
    except (AttributeError, ValueError):
        sys.exit("比分格式: 主队得分-客队得分，如 2-1")
    if gh == ga and args.match > 72 and not args.winner:
        sys.exit("淘汰赛平局需用 --winner 指明晋级方（三字码）")
    settle_score = None
    if args.settle_score:
        try:
            settle_score = tuple(
                int(x) for x in args.settle_score.replace(":", "-").split("-"))
            if len(settle_score) != 2:
                raise ValueError
        except ValueError:
            sys.exit("结算比分格式: 主队得分-客队得分，如 1-1")

    db.record_manual_score(args.match, gh, ga,
                           args.winner.upper() if args.winner else None,
                           settle_score=settle_score,
                           score_type=args.score_type)
    winner_label = "点球胜者" if args.score_type == "penalties" else "晋级方"
    print(f"  已录入第 {args.match} 场 {gh}-{ga}"
          + (f"（结算 {settle_score[0]}-{settle_score[1]}）" if settle_score else "")
          + (f"（{winner_label} {args.winner.upper()}）" if args.winner else ""))
    print("  运行 python3 -m src.update --no-fetch 刷新预测")


if __name__ == "__main__":
    main()
