"""Claude Code 主观微调 CLI：给单场预测加情报驱动的有界扰动。

机制：调整值（百分点）作用在引擎胜负期望上，正数偏向主队、负数偏向客队，
随后照常与市场盘口融合（市场权重继续制衡）。锁档同时保存无微调的基线值，
赛后预测战绩页双线对账——Claude Code 的主观判断是增益还是噪音，全程公开。

纪律：
- 单场幅度受 cap 限制（默认 ±5 百分点，可在 data/config.json 用 advisor_cap 调整）；
- 必须附一句话理由（会公开展示在比赛详情）；
- 开球锁档后不可再调；默认不调，只在情报明确超出引擎认知时出手。

用法：
  python3 -m src.adjust set 7 -3 --note "内马尔缺阵+安帅首秀磨合，下调巴西"
  python3 -m src.adjust list
  python3 -m src.adjust clear 7
设置后运行 ./update.sh 生效。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAP = 5.0


def _cap() -> float:
    try:
        cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
        return float(cfg.get("advisor_cap", cfg.get("fable_cap", DEFAULT_CAP)))
    except (OSError, ValueError):
        return DEFAULT_CAP


def _match(conn, no: int):
    row = conn.execute(
        "SELECT match_no, date_utc, home, away, score_home FROM matches "
        "WHERE match_no=?", (no,)).fetchone()
    return dict(row) if row else None


def cmd_set(args) -> None:
    cap = _cap()
    if abs(args.delta) > cap:
        raise SystemExit(f"  幅度超限：|{args.delta}| > ±{cap} 百分点（cap 可在 config 调整）")
    if not args.note.strip():
        raise SystemExit("  必须附 --note 理由（会公开展示）")
    conn = db.connect()
    m = _match(conn, args.match_no)
    conn.close()
    if not m:
        raise SystemExit(f"  没有第 {args.match_no} 场")
    if m["score_home"] is not None:
        raise SystemExit("  比赛已完赛，不可调整")
    ko = datetime.fromisoformat(
        m["date_utc"].replace(" ", "T").replace("Z", "+00:00"))
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    if ko <= datetime.now(timezone.utc):
        raise SystemExit("  已开球，锁档不可再动")
    db.fable_adjust_set(args.match_no, args.delta, args.note.strip())
    side = "主队" if args.delta > 0 else "客队"
    print(f"  第 {args.match_no} 场 {m['home']} vs {m['away']}: "
          f"胜负期望 {args.delta:+g} 百分点（偏向{side}）")
    print(f"  理由: {args.note.strip()}")
    print("  运行 ./update.sh 后生效并锁档留痕")


def cmd_list(_args) -> None:
    adj = db.fable_adjusts()
    if not adj:
        print("  （当前没有生效中的微调）")
        return
    conn = db.connect()
    for no, a in sorted(adj.items()):
        m = _match(conn, no) or {}
        print(f"  #{no} {m.get('home','?')} vs {m.get('away','?')} "
              f"{a['delta']:+g}pp · {a['note']} ({a['ts']})")
    conn.close()


def cmd_clear(args) -> None:
    db.fable_adjust_clear(args.match_no)
    print(f"  第 {args.match_no} 场微调已撤销（已锁档的历史记录不受影响）")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code 主观微调")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set", help="设置/覆盖单场微调")
    p_set.add_argument("match_no", type=int)
    p_set.add_argument("delta", type=float,
                       help="百分点，正偏主队负偏客队，|x|≤cap")
    p_set.add_argument("--note", required=True, help="一句话理由（公开）")
    p_set.set_defaults(func=cmd_set)
    p_list = sub.add_parser("list", help="列出生效中的微调")
    p_list.set_defaults(func=cmd_list)
    p_clear = sub.add_parser("clear", help="撤销某场微调")
    p_clear.add_argument("match_no", type=int)
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args()
    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
