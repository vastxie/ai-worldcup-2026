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
DEFAULT_PREDICTION_CONFIG = {
    # 未来锁档的默认融合权重：模型 0.45 + 市场 0.55。
    # 已赛锁档读取 DB 里的历史概率，不受这些默认值影响。
    "model_weight": 0.45,
    "model_weight_strong_market": 0.35,
    "strong_market_books": 15,
    "strong_market_disagreement": 0.08,
    "probability_temperature": 1.25,
    "draw_boost_max": 0.025,
    "draw_market_min": 0.27,
    "draw_close_margin": 0.12,
    "draw_favorite_max": 0.50,
    "draw_total_goals_max": 2.55,
}


OPEN_UPDATE_K = 0.08          # 赛中开放度学习率
OPEN_MIN, OPEN_MAX = 0.65, 1.5


def _prediction_config() -> dict:
    cfg = dict(DEFAULT_PREDICTION_CONFIG)
    try:
        raw = json.loads((ROOT / "data" / "config.json").read_text(
            encoding="utf-8"))
        user_cfg = raw.get("prediction", {})
    except (OSError, ValueError, TypeError):
        user_cfg = {}
    if isinstance(user_cfg, dict):
        for key in cfg:
            if key in user_cfg:
                cfg[key] = user_cfg[key]
    return cfg


def _clamp_float(value, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def load_teams() -> dict[str, dict]:
    data = json.loads((ROOT / "data" / "teams.json").read_text(encoding="utf-8"))
    teams = {t["code"]: dict(t) for t in data["teams"]}
    spath = ROOT / "data" / "strengths.json"
    if spath.exists():
        strengths = json.loads(spath.read_text(encoding="utf-8"))
        for code, t in teams.items():
            for key, val in strengths.get(code, {}).items():
                if key in {"att", "def", "eff", "open"}:
                    t[key] = val
    return teams


def outcome_of(score) -> str:
    gh, ga = score
    return "H" if gh > ga else ("A" if ga > gh else "D")


def _normalize_probs(probs: dict) -> dict:
    vals = {
        "p_home": max(float(probs.get("p_home", 0)), 0.001),
        "p_draw": max(float(probs.get("p_draw", 0)), 0.001),
        "p_away": max(float(probs.get("p_away", 0)), 0.001),
    }
    s = sum(vals.values())
    return {k: v / s for k, v in vals.items()}


def _probs_from_pred(pred: dict) -> dict:
    return {"p_home": pred["p_win"], "p_draw": pred["p_draw"],
            "p_away": pred["p_loss"]}


def _round_probs(probs: dict) -> dict:
    probs = _normalize_probs(probs)
    ph = round(probs["p_home"], 4)
    pd = round(probs["p_draw"], 4)
    pa = round(1.0 - ph - pd, 4)
    if pa < 0:
        return {k: round(v, 4) for k, v in _normalize_probs(probs).items()}
    return {"p_home": ph, "p_draw": pd, "p_away": pa}


def _we_from_probs(probs: dict) -> float:
    return probs["p_home"] + 0.5 * probs["p_draw"]


def _model_weight(model_probs: dict, market_probs: dict | None,
                  market: dict | None, cfg: dict) -> float:
    if not market_probs:
        return 1.0
    base = _clamp_float(cfg.get("model_weight"), 0.45, 0.0, 1.0)
    strong = _clamp_float(cfg.get("model_weight_strong_market"), 0.35, 0.0, 1.0)
    try:
        books = int((market or {}).get("books") or 0)
    except (TypeError, ValueError):
        books = 0
    disagreement = max(
        abs(model_probs[k] - market_probs[k])
        for k in ("p_home", "p_draw", "p_away")
    )
    if (books >= _clamp_int(cfg.get("strong_market_books"), 15, 0, 200)
            and disagreement >= _clamp_float(
                cfg.get("strong_market_disagreement"), 0.08, 0.0, 1.0)):
        return min(base, strong)
    return base


def _temperature_scale(probs: dict, temperature: float) -> dict:
    t = _clamp_float(temperature, 1.0, 0.5, 3.0)
    if abs(t - 1.0) < 1e-9:
        return _normalize_probs(probs)
    p = _normalize_probs(probs)
    scaled = {k: p[k] ** (1.0 / t)
              for k in ("p_home", "p_draw", "p_away")}
    return _normalize_probs(scaled)


def _draw_risk_boost(probs: dict, market_probs: dict | None,
                     total_goals: float | None, cfg: dict) -> dict:
    p = _normalize_probs(probs)
    max_p = max(p.values())
    draw_gap = max_p - p["p_draw"]
    market_draw = market_probs["p_draw"] if market_probs else None
    total_ok = total_goals is None or total_goals <= _clamp_float(
        cfg.get("draw_total_goals_max"), 2.55, 0.8, 5.5)
    risk = (
        (market_draw is not None
         and market_draw >= _clamp_float(cfg.get("draw_market_min"), 0.27, 0.0, 0.6))
        or draw_gap <= _clamp_float(cfg.get("draw_close_margin"), 0.12, 0.0, 0.5)
        or max_p <= _clamp_float(cfg.get("draw_favorite_max"), 0.50, 0.34, 0.8)
    )
    if not (risk and total_ok):
        return p
    boost_max = _clamp_float(cfg.get("draw_boost_max"), 0.025, 0.0, 0.08)
    if boost_max <= 0:
        return p
    # Close, low-total matches get the full boost; looser matches get less.
    closeness = max(0.0, 1.0 - draw_gap / max(
        _clamp_float(cfg.get("draw_close_margin"), 0.12, 0.01, 0.5), 0.01))
    boost = boost_max * max(0.45, closeness)
    return _shift_draw(p, boost * 100.0)


def _calibrate_probs(probs: dict, market_probs: dict | None,
                     total_goals: float | None, cfg: dict) -> dict:
    out = _temperature_scale(probs, cfg.get("probability_temperature", 1.25))
    out = _draw_risk_boost(out, market_probs, total_goals, cfg)
    return _round_probs(out)


def _blend_probs(model_probs: dict, market_probs: dict | None,
                 market: dict | None, total_goals: float | None,
                 cfg: dict) -> tuple[dict, float]:
    if not market_probs:
        probs = _calibrate_probs(model_probs, None, total_goals, cfg)
        return probs, 1.0
    weight = _model_weight(model_probs, market_probs, market, cfg)
    blended = _normalize_probs({
        k: weight * model_probs[k] + (1 - weight) * market_probs[k]
        for k in ("p_home", "p_draw", "p_away")
    })
    return _calibrate_probs(blended, market_probs, total_goals, cfg), weight


def _blend_total_goals(model_total: float, market: dict | None,
                       weight: float) -> float:
    if market and market.get("total_goals") is not None:
        return round(weight * model_total
                     + (1 - weight) * market["total_goals"], 3)
    return round(model_total, 3)


def _shift_home_edge(probs: dict, delta_pp: float) -> dict:
    p = dict(_normalize_probs(probs))
    d = float(delta_pp or 0) / 100.0
    floor = 0.01
    if d > 0:
        move = min(d, max(p["p_away"] - floor, 0), max(0.98 - p["p_home"], 0))
        p["p_home"] += move
        p["p_away"] -= move
    elif d < 0:
        move = min(-d, max(p["p_home"] - floor, 0), max(0.98 - p["p_away"], 0))
        p["p_home"] -= move
        p["p_away"] += move
    return _normalize_probs(p)


def _shift_draw(probs: dict, delta_pp: float) -> dict:
    p = dict(_normalize_probs(probs))
    d = float(delta_pp or 0) / 100.0
    floor = 0.01
    hd_sum = p["p_home"] + p["p_away"]
    if d > 0 and hd_sum > 0:
        move = min(d, max(p["p_home"] - floor, 0) + max(p["p_away"] - floor, 0),
                   max(0.6 - p["p_draw"], 0))
        for k in ("p_home", "p_away"):
            cut = move * p[k] / hd_sum
            p[k] = max(p[k] - cut, floor)
        p["p_draw"] += move
    elif d < 0:
        move = min(-d, max(p["p_draw"] - floor, 0))
        p["p_draw"] -= move
        hd_sum = p["p_home"] + p["p_away"]
        p["p_home"] += move * (p["p_home"] / hd_sum if hd_sum else 0.5)
        p["p_away"] += move * (p["p_away"] / hd_sum if hd_sum else 0.5)
    return _normalize_probs(p)


def _apply_adjust(probs: dict, total_goals: float,
                  adjust: dict | None) -> tuple[dict, float]:
    if not adjust:
        return _round_probs(probs), round(total_goals, 3)
    out = _shift_home_edge(probs, adjust.get("delta", 0))
    out = _shift_draw(out, adjust.get("draw", 0))
    total = min(max(total_goals + float(adjust.get("total", 0) or 0), 0.8), 5.5)
    return _round_probs(out), round(total, 3)


def _market_probs(market: dict | None) -> dict | None:
    if not market:
        return None
    return _normalize_probs({"p_home": market["p_home"], "p_draw": market["p_draw"],
                             "p_away": market["p_away"]})


def lock_prediction_override(lock: dict | None, base: bool = False):
    """把 DB 锁档转换成模型可消费的 override；兼容旧 we-only 锁档。"""
    if not lock:
        return None
    probs = lock.get("probs_base" if base else "probs")
    total = lock.get("total_goals_base" if base else "total_goals")
    we = lock.get("we_base" if base else "we")
    if probs:
        out = dict(probs)
        out["we"] = we if we is not None else _we_from_probs(probs)
        if total is not None:
            out["total_goals"] = total
        return out
    return we


def build_state(write_side_effects: bool = True) -> dict:
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
    n_outcome_hit = n_score_hit = n_top3_score_hit = 0
    brier_sum = 0.0
    brier_base_sum = 0.0   # 反事实基线：若无主观微调，纯引擎+市场的误差
    n_adjusted = 0

    for m in played:
        home, away = by_code[m["home"]], by_code[m["away"]]
        ko = m["stage"] != "group"
        lk = locked.get(str(m["match"]))
        pred_o = lock_prediction_override(lk)
        pred = match_probabilities(home, away, knockout=ko, we_override=pred_o)
        top_score = pred["outcome_score"][0]  # 与胜负判断一致的首选比分

        actual = outcome_of(m["score"])
        probs = {"H": pred["p_win"], "D": pred["p_draw"], "A": pred["p_loss"]}
        predicted = max(probs, key=probs.get)
        outcome_hit = predicted == actual
        score_hit = list(top_score) == list(m["score"])
        top3_score_hit = list(m["score"]) in [
            list(s) for s, _ in pred["top_scores"][:3]
        ]
        n_outcome_hit += outcome_hit
        n_score_hit += score_hit
        n_top3_score_hit += top3_score_hit
        brier_sum += sum((probs[o] - (1.0 if o == actual else 0.0)) ** 2
                         for o in "HDA")

        # 主观微调过的场次：再按反事实基线计一遍误差，量化判断增益
        fable = lk.get("fable") if lk else None
        base = None
        if fable and lk.get("we_base") is not None:
            n_adjusted += 1
            base_o = lock_prediction_override(lk, base=True)
            pred_b = match_probabilities(home, away, knockout=ko,
                                         we_override=base_o)
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
            "grid": score_grid(home, away, we_override=pred_o),
            "p_actual_score": round(
                exact_score_prob(home, away, *m["score"],
                                 we_override=pred_o), 4),
            "market": lk.get("market") if lk else None,
            "elo_home_before": round(home["elo"], 1),
            "elo_away_before": round(away["elo"], 1),
            "outcome_hit": outcome_hit,
            "score_hit": score_hit,
            "top3_score_hit": top3_score_hit,
            "fable": fable,
            "base": base,
        })

        elo_h_before, elo_a_before = home["elo"], away["elo"]
        home["elo"], away["elo"] = update_elo(
            home["elo"], away["elo"], tuple(m["score"]),
            home.get("host", False), away.get("host", False))
        if write_side_effects:
            db.log_elo_change(m["match"], [
                (m["home"], elo_h_before, home["elo"]),
                (m["away"], elo_a_before, away["elo"])])

        # 开放度随实际总进球微调（场面比预期开放 → 双方 open 上浮）
        lam_h, lam_a_ = pred["lambdas"]
        ratio = (sum(m["score"]) + 0.5) / (lam_h + lam_a_ + 0.5)
        for t in (home, away):
            t["open"] = min(max(t.get("open", 1.0) * ratio ** OPEN_UPDATE_K,
                                OPEN_MIN), OPEN_MAX)

    # ---- 市场回报系数融合：为未赛对阵生成/刷新锁定预测 ----
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    odds_cache = odds.load() or {}
    pred_cfg = _prediction_config()
    fable_adj = db.fable_adjusts()
    h2h = odds_cache.get("h2h", {})
    we_overrides = {}
    for m in matches:
        if not (m["home"] and m["away"]) or m["score"]:
            continue  # 对阵未定或已赛（已赛的锁档不再改动）
        kickoff = datetime.fromisoformat(
            m["date_utc"].replace(" ", "T")).astimezone(timezone.utc)
        if kickoff <= now_utc:
            # 已开球但比分未入库：滚球/赛后市场参考严禁覆盖赛前锁档
            lk = locked.get(str(m["match"]))
            if lk:
                we_overrides[(m["home"], m["away"])] = lock_prediction_override(lk)
            continue
        mkt = h2h.get(f"{m['home']}|{m['away']}")
        fa = fable_adj.get(m["match"])
        if mkt or fa:
            home, away = by_code[m["home"]], by_code[m["away"]]
            pred_model = match_probabilities(home, away,
                                             knockout=m["stage"] != "group")
            model_probs = _probs_from_pred(pred_model)
            model_total = sum(pred_model["lambdas"])
            adj_probs, adj_total = _apply_adjust(model_probs, model_total, fa)
            mkt_probs = _market_probs(mkt)
            blend_weight = _model_weight(adj_probs, mkt_probs, mkt, pred_cfg)
            base_weight = _model_weight(_round_probs(model_probs), mkt_probs,
                                        mkt, pred_cfg)
            total_blend = _blend_total_goals(adj_total, mkt, blend_weight)
            total_base = _blend_total_goals(round(model_total, 3), mkt,
                                            base_weight)
            probs_blend, blend_weight = _blend_probs(
                adj_probs, mkt_probs, mkt, total_blend, pred_cfg)
            probs_base, base_weight = _blend_probs(
                _round_probs(model_probs), mkt_probs, mkt, total_base, pred_cfg)
            we_blend = round(_we_from_probs(probs_blend), 4)
            we_base = round(_we_from_probs(probs_base), 4)
            fable = ({"delta": fa.get("delta", 0), "draw": fa.get("draw", 0),
                      "total": fa.get("total", 0), "note": fa["note"]}
                     if fa else None)
            locked[str(m["match"])] = {
                "we": we_blend, "we_base": we_base, "market": mkt,
                "probs": probs_blend, "probs_base": probs_base,
                "total_goals": total_blend, "total_goals_base": total_base,
                "model_weight": round(blend_weight, 3),
                "model_weight_base": round(base_weight, 3),
                "fable": fable, "ts": time.strftime("%Y-%m-%d %H:%M"),
            }
            if write_side_effects:
                db.save_lock(m["match"], we_blend, mkt, we_base, fable,
                             probs_blend, probs_base, total_blend, total_base)
        lk = locked.get(str(m["match"]))
        if lk:
            we_overrides[(m["home"], m["away"])] = lock_prediction_override(lk)

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
            "top3_score_acc": round(n_top3_score_hit / n, 4) if n else None,
            "brier": round(brier_sum / n, 4) if n else None,
            "brier_base": round(brier_base_sum / n, 4) if n else None,
            "n_adjusted": n_adjusted,
        },
    }
