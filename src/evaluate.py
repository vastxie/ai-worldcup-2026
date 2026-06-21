"""只读预测回测：按服务器当前 DB 复盘已赛锁档。

用法：
  python3 -m src.evaluate
  python3 -m src.evaluate --json

不会联网、不会结算预测、不会写入 DB；适合在服务器上每天跑完
ops_update 后快速看纯模型 / 市场 / 融合锁档的 Brier 与命中率。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter

from . import db
from .elo import update_elo
from .fetch import load_matches
from .model import match_probabilities
from .state import (_market_probs, build_state, load_teams,
                    lock_prediction_override, outcome_of)


def _pred_probs(pred: dict) -> dict:
    return {"H": pred["p_win"], "D": pred["p_draw"], "A": pred["p_loss"]}


def _pick(probs: dict) -> str:
    return max(probs, key=probs.get)


def _brier(probs: dict, actual: str) -> float:
    return sum((probs[o] - (1.0 if o == actual else 0.0)) ** 2
               for o in "HDA")


def _logloss(probs: dict, actual: str) -> float:
    return -math.log(max(probs[actual], 1e-9))


def _metric(rows: list[dict], key: str) -> dict | None:
    vals = [(r[key], r["actual"]) for r in rows if r.get(key)]
    if not vals:
        return None
    n = len(vals)
    top1 = sum(_pick(p) == actual for p, actual in vals)
    top2 = sum(actual in sorted(p, key=p.get, reverse=True)[:2]
               for p, actual in vals)
    return {
        "n": n,
        "top1_acc": round(top1 / n, 4),
        "top2_acc": round(top2 / n, 4),
        "brier": round(sum(_brier(p, actual) for p, actual in vals) / n, 4),
        "logloss": round(sum(_logloss(p, actual) for p, actual in vals) / n, 4),
        "avg_draw": round(sum(p["D"] for p, _ in vals) / n, 4),
    }


def _replay_rows() -> list[dict]:
    teams = load_teams()
    for t in teams.values():
        t["elo_base"] = t["elo"]
    locks = db.load_locks()
    matches = [m for m in load_matches()
               if m.get("score") and m.get("home") and m.get("away")]
    matches.sort(key=lambda m: (m["date_utc"], m["match"]))

    rows = []
    for m in matches:
        home, away = teams[m["home"]], teams[m["away"]]
        actual = outcome_of(m["score"])
        knockout = m["stage"] != "group"
        lock = locks.get(str(m["match"]))

        pure = _pred_probs(match_probabilities(home, away, knockout=knockout))
        locked = _pred_probs(match_probabilities(
            home, away, knockout=knockout,
            we_override=lock_prediction_override(lock)))
        market = None
        if lock and lock.get("market"):
            mk = _market_probs(lock["market"])
            market = {"H": mk["p_home"], "D": mk["p_draw"],
                      "A": mk["p_away"]}
        full_lock = None
        if lock and lock.get("probs"):
            p = lock["probs"]
            full_lock = {"H": p["p_home"], "D": p["p_draw"],
                         "A": p["p_away"]}

        rows.append({
            "match": m["match"],
            "date_utc": m["date_utc"],
            "stage": m["stage"],
            "home": m["home"],
            "away": m["away"],
            "score": m["score"],
            "actual": actual,
            "pure": pure,
            "locked": locked,
            "market": market,
            "full_lock": full_lock,
            "has_full_lock": full_lock is not None,
            "adjusted": bool(lock and lock.get("fable")),
        })

        home["elo"], away["elo"] = update_elo(
            home["elo"], away["elo"], tuple(m["score"]),
            home.get("host", False), away.get("host", False))
    return rows


def evaluate() -> dict:
    rows = _replay_rows()
    state = build_state(write_side_effects=False)
    records = state["records"]
    n_records = len(records)
    global_score_hit = sum(
        r["top_scores"] and r["top_scores"][0]["score"] == r["score"]
        for r in records)
    direction_score_hit = sum(r["score_hit"] for r in records)
    top3_score_hit = sum(r["top3_score_hit"] for r in records)

    segments = {}
    for name, subset in {
        "legacy_lock": [r for r in rows if not r["has_full_lock"]],
        "full_lock": [r for r in rows if r["has_full_lock"]],
        "adjusted": [r for r in rows if r["adjusted"]],
        "not_adjusted": [r for r in rows if not r["adjusted"]],
    }.items():
        if subset:
            segments[name] = {
                "n": len(subset),
                "outcomes": dict(Counter(r["actual"] for r in subset)),
                "locked": _metric(subset, "locked"),
                "market": _metric(subset, "market"),
            }

    misses = []
    for r in rows:
        locked = r["locked"]
        if _pick(locked) == r["actual"]:
            continue
        misses.append({
            "match": r["match"],
            "home": r["home"],
            "away": r["away"],
            "score": r["score"],
            "actual": r["actual"],
            "pick": _pick(locked),
            "p_actual": round(locked[r["actual"]], 4),
            "brier": round(_brier(locked, r["actual"]), 4),
            "market_pick": _pick(r["market"]) if r.get("market") else None,
            "adjusted": r["adjusted"],
            "has_full_lock": r["has_full_lock"],
        })

    return {
        "n": len(rows),
        "outcomes": dict(Counter(r["actual"] for r in rows)),
        "metrics": {
            "pure": _metric(rows, "pure"),
            "market": _metric(rows, "market"),
            "locked": _metric(rows, "locked"),
            "full_lock": _metric(rows, "full_lock"),
        },
        "score_metrics": {
            "n": n_records,
            "global_score_acc": (round(global_score_hit / n_records, 4)
                                 if n_records else None),
            "direction_score_acc": (round(direction_score_hit / n_records, 4)
                                    if n_records else None),
            "top3_score_acc": (round(top3_score_hit / n_records, 4)
                               if n_records else None),
        },
        "segments": segments,
        "misses": misses,
    }


def _print_table(result: dict, limit_misses: int) -> None:
    print(f"已赛样本: {result['n']}  胜/平/负: {result['outcomes']}")
    print("\n胜平负：")
    for name, metric in result["metrics"].items():
        if not metric:
            continue
        print(
            f"  {name:<9} n={metric['n']:>2} "
            f"Top1={metric['top1_acc'] * 100:5.1f}% "
            f"Top2={metric['top2_acc'] * 100:5.1f}% "
            f"Brier={metric['brier']:.4f} "
            f"LogLoss={metric['logloss']:.4f} "
            f"AvgDraw={metric['avg_draw']:.3f}"
        )
    sm = result["score_metrics"]
    print("\n比分："
          f" 全局首选 {sm['global_score_acc'] * 100:.1f}%"
          f" / 方向首选 {sm['direction_score_acc'] * 100:.1f}%"
          f" / Top3 {sm['top3_score_acc'] * 100:.1f}%")

    print("\n分段：")
    for name, seg in result["segments"].items():
        locked = seg.get("locked")
        market = seg.get("market")
        bits = [f"  {name:<13} n={seg['n']:>2}",
                f"outcomes={seg['outcomes']}"]
        if locked:
            bits.append(f"locked Brier={locked['brier']:.4f}")
        if market:
            bits.append(f"market Brier={market['brier']:.4f}")
        print("  ".join(bits))

    if limit_misses:
        print("\n锁档 Top1 未命中：")
        for miss in result["misses"][:limit_misses]:
            print(
                f"  #{miss['match']} {miss['home']}-{miss['away']} "
                f"{miss['score']} actual={miss['actual']} pick={miss['pick']} "
                f"p_actual={miss['p_actual']:.3f} brier={miss['brier']:.3f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="只读预测回测")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--limit-misses", type=int, default=12)
    args = parser.parse_args()

    result = evaluate()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_table(result, max(0, args.limit_misses))


if __name__ == "__main__":
    main()
