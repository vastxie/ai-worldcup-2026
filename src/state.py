"""动态赛事状态：基准 Elo + 已赛结果 → 当前 Elo / 模型战绩 / 条件模拟输入。

核心原则：按时间顺序回放每场已赛比赛——先用"赛前"的 Elo 生成预测
（保证战绩统计是真正的事前预测，不偷看结果），再用真实比分更新 Elo。
整个状态由 teams.json + matches.json 确定性推导，无需额外存档。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import db, odds
from .elo import update_elo
from .fetch import load_matches
from .model import (effective_elo, exact_score_prob, match_probabilities,
                    score_grid, win_expectancy)
from .tournament import GROUPS

ROOT = Path(__file__).resolve().parent.parent
MODEL_WEIGHT = 0.7   # 融合权重：模型 0.7 + 市场 0.3


OPEN_UPDATE_K = 0.08          # 赛中开放度学习率
OPEN_MIN, OPEN_MAX = 0.65, 1.5


def load_teams() -> dict[str, dict]:
    data = json.loads((ROOT / "data" / "teams.json").read_text(encoding="utf-8"))
    teams = {t["code"]: dict(t) for t in data["teams"]}
    spath = ROOT / "data" / "strengths.json"
    if spath.exists():
        strengths = json.loads(spath.read_text(encoding="utf-8"))
        for code, t in teams.items():
            t["open"] = strengths.get(code, {}).get("open", 1.0)
    return teams


def outcome_of(score) -> str:
    gh, ga = score
    return "H" if gh > ga else ("A" if ga > gh else "D")


def build_state() -> dict:
    """回放全部已赛比赛，返回当前完整状态。"""
    by_code = load_teams()
    for t in by_code.values():
        t["elo_base"] = t["elo"]  # 保留开赛日快照，elo 字段动态演化

    matches = load_matches()
    played = [m for m in matches if m["score"] and m["home"] and m["away"]]
    played.sort(key=lambda m: (m["date_utc"], m["match"]))

    # 赛前锁定的「模型+市场」融合预测（开赛前最后一次更新写入，赛后冻结）
    locked = db.load_locks()

    records = []           # 已赛比赛的事前预测 vs 实际
    n_outcome_hit = n_score_hit = 0
    brier_sum = 0.0
    brier_base_sum = 0.0   # 反事实基线：若无主观微调，纯引擎+市场的误差
    n_adjusted = 0

    for m in played:
        home, away = by_code[m["home"]], by_code[m["away"]]
        ko = m["stage"] != "group"
        lk = locked.get(str(m["match"]))
        we_o = lk["we"] if lk else None
        pred = match_probabilities(home, away, knockout=ko, we_override=we_o)
        top_score = pred["outcome_score"][0]  # 与胜负判断一致的首选比分

        actual = outcome_of(m["score"])
        probs = {"H": pred["p_win"], "D": pred["p_draw"], "A": pred["p_loss"]}
        predicted = max(probs, key=probs.get)
        outcome_hit = predicted == actual
        score_hit = list(top_score) == list(m["score"])
        n_outcome_hit += outcome_hit
        n_score_hit += score_hit
        brier_sum += sum((probs[o] - (1.0 if o == actual else 0.0)) ** 2
                         for o in "HDA")

        # 主观微调过的场次：再按反事实基线计一遍误差，量化判断增益
        fable = lk.get("fable") if lk else None
        base = None
        if fable and lk.get("we_base") is not None:
            n_adjusted += 1
            pred_b = match_probabilities(home, away, knockout=ko,
                                         we_override=lk["we_base"])
            probs_b = {"H": pred_b["p_win"], "D": pred_b["p_draw"],
                       "A": pred_b["p_loss"]}
            base = {k: round(v, 4) for k, v in
                    zip(("p_home", "p_draw", "p_away"),
                        (probs_b["H"], probs_b["D"], probs_b["A"]))}
        else:
            probs_b = probs
        brier_base_sum += sum((probs_b[o] - (1.0 if o == actual else 0.0)) ** 2
                              for o in "HDA")

        records.append({
            "match": m["match"], "stage": m["stage"], "date_utc": m["date_utc"],
            "home": m["home"], "away": m["away"], "score": m["score"],
            "winner": m["winner"],
            "p_home": round(pred["p_win"], 4),
            "p_draw": round(pred["p_draw"], 4),
            "p_away": round(pred["p_loss"], 4),
            "pick": pred["outcome_pick"],
            "pred_score": list(top_score),
            "top_scores": [{"score": list(s), "p": round(p, 4)}
                           for s, p in pred["top_scores"][:5]],
            "grid": score_grid(home, away, we_override=we_o),
            "p_actual_score": round(
                exact_score_prob(home, away, *m["score"], we_override=we_o), 4),
            "market": lk.get("market") if lk else None,
            "elo_home_before": round(home["elo"], 1),
            "elo_away_before": round(away["elo"], 1),
            "outcome_hit": outcome_hit,
            "score_hit": score_hit,
            "fable": fable,
            "base": base,
        })

        elo_h_before, elo_a_before = home["elo"], away["elo"]
        home["elo"], away["elo"] = update_elo(
            home["elo"], away["elo"], tuple(m["score"]),
            home.get("host", False), away.get("host", False))
        db.log_elo_change(m["match"], [
            (m["home"], elo_h_before, home["elo"]),
            (m["away"], elo_a_before, away["elo"])])

        # 开放度随实际总进球微调（场面比预期开放 → 双方 open 上浮）
        lam_h, lam_a_ = pred["lambdas"]
        ratio = (sum(m["score"]) + 0.5) / (lam_h + lam_a_ + 0.5)
        for t in (home, away):
            t["open"] = min(max(t.get("open", 1.0) * ratio ** OPEN_UPDATE_K,
                                OPEN_MIN), OPEN_MAX)

    # ---- 市场赔率融合：为未赛对阵生成/刷新锁定预测 ----
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    odds_cache = odds.load() or {}
    fable_adj = db.fable_adjusts()
    h2h = odds_cache.get("h2h", {})
    we_overrides = {}
    for m in matches:
        if not (m["home"] and m["away"]) or m["score"]:
            continue  # 对阵未定或已赛（已赛的锁档不再改动）
        kickoff = datetime.fromisoformat(
            m["date_utc"].replace(" ", "T")).astimezone(timezone.utc)
        if kickoff <= now_utc:
            # 已开球但比分未入库：滚球/赛后盘口严禁覆盖赛前锁档
            lk = locked.get(str(m["match"]))
            if lk:
                we_overrides[(m["home"], m["away"])] = lk["we"]
            continue
        mkt = h2h.get(f"{m['home']}|{m['away']}")
        fa = fable_adj.get(m["match"])
        if mkt or fa:
            home, away = by_code[m["home"]], by_code[m["away"]]
            we_model = win_expectancy(effective_elo(home), effective_elo(away))
            # Claude Code 主观微调：情报驱动的有界扰动——先调引擎，再融市场，
            # 市场权重天然制衡；无微调的基线值同存，赛后双线对账
            we_adj = (min(max(we_model + fa["delta"] / 100.0, 0.05), 0.95)
                      if fa else we_model)
            if mkt:
                we_mkt = mkt["p_home"] + 0.5 * mkt["p_draw"]
                we_blend = round(MODEL_WEIGHT * we_adj
                                 + (1 - MODEL_WEIGHT) * we_mkt, 4)
                we_base = round(MODEL_WEIGHT * we_model
                                + (1 - MODEL_WEIGHT) * we_mkt, 4)
            else:
                we_blend, we_base = round(we_adj, 4), round(we_model, 4)
            fable = ({"delta": fa["delta"], "note": fa["note"]} if fa else None)
            locked[str(m["match"])] = {
                "we": we_blend, "we_base": we_base, "market": mkt,
                "fable": fable, "ts": time.strftime("%Y-%m-%d %H:%M"),
            }
            db.save_lock(m["match"], we_blend, mkt, we_base, fable)
        lk = locked.get(str(m["match"]))
        if lk:
            we_overrides[(m["home"], m["away"])] = lk["we"]

    # ---- 条件模拟所需的固定结果 ----
    group_results = {(m["home"], m["away"]): tuple(m["score"])
                     for m in played if m["stage"] == "group"}
    ko_teams = {m["match"]: (m["home"], m["away"])
                for m in matches
                if m["stage"] != "group" and m["home"] and m["away"]}
    ko_winners = {}
    for m in played:
        if m["stage"] == "group":
            continue
        gh, ga = m["score"]
        winner = (m["home"] if gh > ga else m["away"] if ga > gh
                  else m["winner"])  # 平局须由 winner 字段给出点球胜者
        if winner:
            ko_winners[m["match"]] = winner

    # ---- 小组实时积分表 ----
    live_tables = {g: {} for g in GROUPS}
    for code, t in by_code.items():
        live_tables[t["group"]][code] = {"pts": 0, "gf": 0, "ga": 0, "played": 0}
    for m in played:
        if m["stage"] != "group":
            continue
        gh, ga = m["score"]
        th = live_tables[by_code[m["home"]]["group"]][m["home"]]
        ta = live_tables[by_code[m["away"]]["group"]][m["away"]]
        th["gf"] += gh; th["ga"] += ga; th["played"] += 1
        ta["gf"] += ga; ta["ga"] += gh; ta["played"] += 1
        if gh > ga:
            th["pts"] += 3
        elif ga > gh:
            ta["pts"] += 3
        else:
            th["pts"] += 1; ta["pts"] += 1

    n = len(records)
    return {
        "by_code": by_code,
        "groups": {g: [t for t in by_code.values() if t["group"] == g]
                   for g in GROUPS},
        "matches": matches,
        "fixed": {"group_results": group_results,
                  "ko_teams": ko_teams,
                  "ko_winners": ko_winners,
                  "we_overrides": we_overrides},
        "locked": locked,
        "market_winner": odds_cache.get("winner", {}),
        "market_live": bool(we_overrides),
        "live_tables": live_tables,
        "records": records,
        "record_stats": {
            "n": n,
            "outcome_acc": round(n_outcome_hit / n, 4) if n else None,
            "score_acc": round(n_score_hit / n, 4) if n else None,
            "brier": round(brier_sum / n, 4) if n else None,
            "brier_base": round(brier_base_sum / n, 4) if n else None,
            "n_adjusted": n_adjusted,
        },
    }
