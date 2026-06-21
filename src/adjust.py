"""Codex 主观微调 CLI：给单场预测加情报驱动的有界扰动。

机制：默认调整值（百分点）作用在主/客方向上，正数偏向主队、负数偏向客队；
也可用 --draw 单独调平局概率、--total 调总进球期望。随后照常与市场市场参考融合
（市场权重继续制衡）。锁档同时保存无微调的基线值，赛后预测战绩页双线对账。

纪律：
- 单场幅度受 cap 限制（默认 ±5 百分点，可在 data/config.json 用 advisor_cap 调整）；
- 总进球幅度受 advisor_total_cap 限制（默认 ±0.6 球）；
- 必须附一句话理由（会公开展示在比赛详情）；
- 开球锁档后不可再调；默认不调，只在情报明确超出引擎认知时出手。

用法：
  python3 -m src.adjust set 7 -3 --note "内马尔缺阵+安帅首秀磨合，下调巴西"
  python3 -m src.adjust set 10 --draw 2 --total -0.25 --note "两队首轮谨慎"
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
DEFAULT_TOTAL_CAP = 0.6


def _cap() -> float:
    try:
        cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
        return float(cfg.get("advisor_cap", cfg.get("fable_cap", DEFAULT_CAP)))
    except (OSError, ValueError):
        return DEFAULT_CAP


def _total_cap() -> float:
    try:
        cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
        return float(cfg.get("advisor_total_cap", DEFAULT_TOTAL_CAP))
    except (OSError, ValueError):
        return DEFAULT_TOTAL_CAP


def _match(conn, no: int):
    row = conn.execute(
        "SELECT match_no, date_utc, home, away, score_home FROM matches "
        "WHERE match_no=?", (no,)).fetchone()
    return dict(row) if row else None


def cmd_set(args) -> None:
    cap = _cap()
    total_cap = _total_cap()
    if args.delta is not None and args.home_edge is not None:
        raise SystemExit("  位置参数 delta 与 --home-edge 二选一即可")
    home_edge = args.home_edge if args.home_edge is not None else (args.delta or 0.0)
    draw = args.draw or 0.0
    total = args.total or 0.0
    if abs(home_edge) > cap:
        raise SystemExit(f"  主客幅度超限：|{home_edge}| > ±{cap} 百分点")
    if abs(draw) > cap:
        raise SystemExit(f"  平局幅度超限：|{draw}| > ±{cap} 百分点")
    if abs(total) > total_cap:
        raise SystemExit(f"  总进球幅度超限：|{total}| > ±{total_cap} 球")
    if home_edge == 0 and draw == 0 and total == 0:
        raise SystemExit("  至少设置一个调整：delta / --home-edge / --draw / --total")
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
    db.fable_adjust_set(args.match_no, home_edge, args.note.strip(), draw, total)
    bits = []
    if home_edge:
        bits.append(f"主客 {home_edge:+g}pp")
    if draw:
        bits.append(f"平局 {draw:+g}pp")
    if total:
        bits.append(f"总球 {total:+g}")
    print(f"  第 {args.match_no} 场 {m['home']} vs {m['away']}: "
          + " / ".join(bits))
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
        bits = []
        if a.get("delta"):
            bits.append(f"主客 {a['delta']:+g}pp")
        if a.get("draw"):
            bits.append(f"平局 {a['draw']:+g}pp")
        if a.get("total"):
            bits.append(f"总球 {a['total']:+g}")
        print(f"  #{no} {m.get('home','?')} vs {m.get('away','?')} "
              f"{' / '.join(bits)} · {a['note']} ({a['ts']})")
    conn.close()


def cmd_clear(args) -> None:
    db.fable_adjust_clear(args.match_no)
    print(f"  第 {args.match_no} 场微调已撤销（已锁档的历史记录不受影响）")


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 主观微调")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set", help="设置/覆盖单场微调")
    p_set.add_argument("match_no", type=int)
    p_set.add_argument("delta", type=float, nargs="?",
                       help="兼容旧用法：主客百分点，正偏主队负偏客队")
    p_set.add_argument("--home-edge", type=float, default=None,
                       help="主客百分点，正偏主队负偏客队，|x|≤cap")
    p_set.add_argument("--draw", type=float, default=0.0,
                       help="平局概率百分点，正数防平，|x|≤cap")
    p_set.add_argument("--total", type=float, default=0.0,
                       help="总进球期望调整，单位=球")
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
