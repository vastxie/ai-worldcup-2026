"""每日更新管线：同步比分 → 回放更新 Elo → 条件蒙特卡洛 → 生成网站数据。

用法（项目根目录）：
    python3 -m src.update                        # 完整更新（默认 100 万次模拟）
    python3 -m src.update --sims 100000000       # 一亿次（多进程，约 20~30 分钟）
    python3 -m src.update --no-fetch             # 跳过联网，只用本地数据重算
    python3 -m src.update --no-fetch --dry-run   # 临时 DB 试算，不发布产物

每次运行会：
1. 同步 104 场赛程与最新比分（失败自动降级为本地数据）；
2. 按时间回放已赛比赛：先记录事前预测（预测战绩），再按 eloratings 公式更新 Elo；
3. 以"已赛结果固定、未赛掷骰子"的方式做全程蒙特卡洛（自动多进程并行）；
4. 对所有未赛且对阵已知的比赛给出胜平负概率、比分概率矩阵；
5. 写 web/data.js、out/results.json，并在 data/history.json 追加当日夺冠概率快照。
发布前会拒绝用已赛场次或模拟次数回退的结果覆盖现有产物；确认覆盖可加
--force-publish。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from . import db, fetch, odds, report
from .utils import atomic_write_text
from .model import match_probabilities, score_grid
from .state import build_state
from .tournament import GROUPS, simulate_tournament

ROOT = Path(__file__).resolve().parent.parent
STAGES = ["r32", "r16", "qf", "sf", "final", "champion"]
STAGE_ZH = {"r32": "晋级32强", "r16": "进16强", "qf": "进8强",
            "sf": "进4强", "final": "进决赛", "champion": "夺冠"}


@contextmanager
def isolated_db(enabled: bool):
    """dry-run 时用临时 DB 副本承接锁档/结算/历史写入，保护真实运行数据。"""
    if not enabled:
        yield
        return
    old_path = db.DB_PATH
    with tempfile.TemporaryDirectory(prefix="wc-dry-run-") as tmp:
        tmp_path = Path(tmp) / old_path.name
        if old_path.exists():
            src = sqlite3.connect(old_path)
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        db.DB_PATH = tmp_path
        try:
            yield
        finally:
            db.DB_PATH = old_path


# ----------------------------------------------------------------- simulate --

def _run_chunk(state: dict, sims: int, seed: int) -> dict:
    """跑一段模拟，返回可合并的计数器（供单进程或子进程使用）。"""
    by_code, groups, fixed = state["by_code"], state["groups"], state["fixed"]
    rng = random.Random(seed)
    counts = {code: Counter() for code in by_code}
    group_pts = {code: 0.0 for code in by_code}
    group_rank = {code: Counter() for code in by_code}
    final_pairs: Counter = Counter()
    title_match: Counter = Counter()

    for _ in range(sims):
        result = simulate_tournament(groups, rng, fixed)
        for g in GROUPS:
            for rank, row in enumerate(result["standings"][g], start=1):
                code = row["team"]["code"]
                group_pts[code] += row["pts"]
                group_rank[code][rank] += 1
        for stage_key, codes in (("r32", result["r32"]), ("r16", result["r16"]),
                                 ("qf", result["qf"]), ("sf", result["sf"]),
                                 ("final", result["finalists"])):
            for code in codes:
                counts[code][stage_key] += 1
        counts[result["champion"]]["champion"] += 1
        final_pairs[tuple(sorted(result["finalists"]))] += 1
        title_match[(result["champion"], result["runner_up"])] += 1

    return {"counts": counts, "group_pts": group_pts,
            "group_rank": group_rank, "final_pairs": final_pairs,
            "title_match": title_match}


def _chunk_worker(args: tuple[int, int]) -> dict:
    """子进程入口：自行重建状态（由数据文件确定性推导）。"""
    sims, seed = args
    return _run_chunk(build_state(), sims, seed)


def _merge(parts: list[dict], by_code: dict) -> dict:
    merged = {"counts": {c: Counter() for c in by_code},
              "group_pts": {c: 0.0 for c in by_code},
              "group_rank": {c: Counter() for c in by_code},
              "final_pairs": Counter(), "title_match": Counter()}
    for p in parts:
        for c in by_code:
            merged["counts"][c].update(p["counts"][c])
            merged["group_pts"][c] += p["group_pts"][c]
            merged["group_rank"][c].update(p["group_rank"][c])
        merged["final_pairs"].update(p["final_pairs"])
        merged["title_match"].update(p["title_match"])
    return merged


def run_simulations(state: dict, sims: int, seed: int, workers: int) -> dict:
    by_code = state["by_code"]
    t0 = time.time()

    if workers <= 1 or sims < 50_000:
        agg = _run_chunk(state, sims, seed)
    else:
        n_chunks = workers * 8
        per = sims // n_chunks
        chunk_sims = [per] * n_chunks
        chunk_sims[-1] += sims - per * n_chunks
        tasks = [(n, seed * 1_000_003 + i) for i, n in enumerate(chunk_sims)]
        parts, done = [], 0
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for part in pool.imap_unordered(_chunk_worker, tasks):
                parts.append(part)
                done += 1
                pct = done / n_chunks * 100
                eta = (time.time() - t0) / done * (n_chunks - done)
                print(f"\r  并行模拟 {workers} 进程: {pct:5.1f}%  "
                      f"剩余约 {eta / 60:.1f} 分钟 ...", end="", flush=True)
        agg = _merge(parts, by_code)
    print(f"\r  完成 {sims:,} 次条件模拟，耗时 {(time.time() - t0) / 60:.1f} 分钟"
          + " " * 24)

    market_winner = state.get("market_winner", {})
    teams_out = []
    for code, team in by_code.items():
        live = state["live_tables"][team["group"]][code]
        entry = {
            "p_champion_market": market_winner.get(code),
            "code": code, "name_zh": team["name_zh"], "name_en": team["name_en"],
            "group": team["group"], "host": team["host"],
            "elo_base": round(team["elo_base"], 1),
            "elo": round(team["elo"], 1),
            "exp_group_pts": round(agg["group_pts"][code] / sims, 2),
            "p_group_win": round(agg["group_rank"][code][1] / sims, 6),
            "live": live,
        }
        for stage in STAGES:
            entry["p_" + stage] = round(agg["counts"][code][stage] / sims, 6)
        teams_out.append(entry)
    teams_out.sort(key=lambda t: (-t["p_champion"], -t["p_final"], -t["elo"]))

    return {
        "teams": teams_out,
        "top_finals": [{"pair": list(p), "p": round(c / sims, 6)}
                       for p, c in agg["final_pairs"].most_common(10)],
        "top_title_matches": [
            {"champion": ch, "runner_up": ru, "p": round(c / sims, 6)}
            for (ch, ru), c in agg["title_match"].most_common(10)],
    }


# ----------------------------------------------------------------- schedule --

def build_schedule(state: dict) -> list[dict]:
    """全部 104 场：已赛附事前预测与命中情况，未赛附实时预测与比分矩阵。"""
    by_code = state["by_code"]
    rec_by_match = {r["match"]: r for r in state["records"]}
    out = []
    for m in state["matches"]:
        row = {k: m[k] for k in ("match", "round", "stage", "group", "date_utc",
                                 "venue", "home", "away", "slot_home",
                                 "slot_away", "score", "winner")}
        rec = rec_by_match.get(m["match"])
        if rec:  # 已赛：用赛前预测（含市场盘口时即融合版）
            row["pred"] = {"p_home": rec["p_home"], "p_draw": rec["p_draw"],
                           "p_away": rec["p_away"],
                           "pick": rec["pick"],
                           "pred_score": rec["pred_score"],
                           "top_scores": rec["top_scores"],
                           "grid": rec["grid"],
                           "p_actual_score": rec["p_actual_score"],
                           "market": rec["market"],
                           "fable": rec.get("fable"),
                           "base": rec.get("base")}
            row["outcome_hit"] = rec["outcome_hit"]
            row["score_hit"] = rec["score_hit"]
        elif m["home"] and m["away"] and not m["score"]:  # 未赛但对阵已知
            ko = m["stage"] != "group"
            home, away = by_code[m["home"]], by_code[m["away"]]
            lk = state["locked"].get(str(m["match"]))
            we_o = lk["we"] if lk else None
            pred = match_probabilities(home, away, knockout=ko, we_override=we_o)
            row["pred"] = {
                "p_home": round(pred["p_win"], 4),
                "p_draw": round(pred["p_draw"], 4),
                "p_away": round(pred["p_loss"], 4),
                "pick": pred["outcome_pick"],
                "pred_score": list(pred["outcome_score"][0]),
                "top_scores": [{"score": list(s), "p": round(p, 4)}
                               for s, p in pred["top_scores"][:5]],
                "grid": score_grid(home, away, we_override=we_o),
                "market": lk["market"] if lk else None,
            }
            fable = lk.get("fable") if lk else None
            if fable and lk.get("we_base") is not None:
                pred_b = match_probabilities(home, away, knockout=ko,
                                             we_override=lk["we_base"])
                row["pred"]["fable"] = fable
                row["pred"]["base"] = {"p_home": round(pred_b["p_win"], 4),
                                       "p_draw": round(pred_b["p_draw"], 4),
                                       "p_away": round(pred_b["p_loss"], 4)}
            if ko:
                row["pred"]["p_adv_home"] = round(pred["p_advance_a"], 4)
                row["pred"]["p_adv_away"] = round(pred["p_advance_b"], 4)
        out.append(row)
    return out


# ------------------------------------------------------------------ history --

def update_history(sim_out: dict, played: int, sims: int) -> list[dict]:
    today = time.strftime("%Y-%m-%d")
    champion = {t["code"]: t["p_champion"] for t in sim_out["teams"][:12]}
    # 出线（晋级32强）概率存全队——本轮影响面板要查本轮参赛队，未必在夺冠 Top12
    advance = {t["code"]: round(t.get("p_r32", 0), 4) for t in sim_out["teams"]}
    db.save_champ_snapshot(today, played, sims, champion, advance)
    return db.load_champ_history()


# ------------------------------------------------------------------ outputs --

def write_outputs(payload: dict) -> None:
    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    atomic_write_text(out_dir / "results.json",
                      json.dumps(payload, ensure_ascii=False, indent=1))
    atomic_write_text(ROOT / "web" / "data.js",
                      "window.WC_DATA = "
                      + json.dumps(payload, ensure_ascii=False) + ";\n")


def validate_publish_meta(played: int, sims: int, force: bool = False) -> None:
    """避免用旧数据库/低样本 dry run 产物覆盖当前发布结果。"""
    path = ROOT / "out" / "results.json"
    if force or not path.exists():
        return
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    old_meta = prev.get("meta", {})
    problems = []
    old_played, old_sims = old_meta.get("played", 0), old_meta.get("sims", 0)
    if played < old_played:
        problems.append(f"已赛场次回退 {played} < {old_played}")
    if played <= old_played and sims < old_sims:
        problems.append(f"模拟次数回退 {sims:,} < {old_sims:,}")
    if problems:
        raise SystemExit("  [publish] 拒绝覆盖当前产物：" + "；".join(problems)
                         + "。确认要覆盖请加 --force-publish")


def print_report(payload: dict, published: bool = True) -> None:
    teams = payload["teams"]
    name = {t["code"]: t["name_zh"] for t in teams}
    stats = payload["record"]["stats"]

    print()
    print("=" * 78)
    print(f"  2026 世界杯 AI 预测  ·  已赛 {payload['meta']['played']}/104 场"
          f"  ·  更新于 {payload['meta']['updated_at']}")
    print("=" * 78)
    if stats["n"]:
        fable_note = ""
        if stats.get("n_adjusted"):
            fable_note = (f" | 纯引擎 Brier {stats['brier_base']:.3f}"
                          f"（Fable 微调 {stats['n_adjusted']} 场）")
        print(f"  预测战绩: 胜平负命中 {stats['outcome_acc'] * 100:.0f}%"
              f" | 精确比分命中 {stats['score_acc'] * 100:.0f}%"
              f" | Brier {stats['brier']:.3f}  (共 {stats['n']} 场)"
              + fable_note)
        print("-" * 78)
    print(f"  {'球队':　<8}{'组':>2}  {'Elo':>6}" + "".join(
        f"{STAGE_ZH[s]:>10}" for s in STAGES))
    print("-" * 78)
    for t in teams[:12]:
        row = f"  {t['name_zh']:　<8}{t['group']:>2}  {t['elo']:>6.0f}"
        for s in STAGES:
            row += f"{t['p_' + s] * 100:>9.2f}%"
        print(row)
    movers = sorted(teams, key=lambda t: abs(t["elo"] - t["elo_base"]),
                    reverse=True)[:5]
    if any(abs(t["elo"] - t["elo_base"]) > 0.5 for t in movers):
        print()
        print("  Elo 变化最大:")
        for t in movers:
            d = t["elo"] - t["elo_base"]
            if abs(d) > 0.5:
                print(f"    {t['name_zh']:　<8} {t['elo_base']:.0f} → "
                      f"{t['elo']:.0f}  ({d:+.0f})")
    print()
    print("  最可能的决赛对阵:")
    for fm in payload["top_finals"][:5]:
        a, b = fm["pair"]
        print(f"    {name[a]} vs {name[b]:　<8}  {fm['p'] * 100:5.2f}%")
    print()
    if published:
        print("  数据已写入 web/data.js（网站）与 out/results.json")
    else:
        print("  dry-run 仅完成试算，未写入 web/data.js 或 out/results.json")


# --------------------------------------------------------------------- main --

def run(sims: int, seed: int | None, do_fetch: bool,
        workers: int | None = None, dry_run: bool = False,
        force_publish: bool = False) -> dict:
    with isolated_db(dry_run):
        return _run(sims, seed, do_fetch, workers, dry_run, force_publish)


def _run(sims: int, seed: int | None, do_fetch: bool,
         workers: int | None, dry_run: bool, force_publish: bool) -> dict:
    db.init_db()
    # 自举：库为空（如服务器首次切换）时自动从 JSON 迁移，保护 cron 不踩空
    conn = db.connect()
    empty = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    conn.close()
    if empty:
        from . import migrate
        print("  [db] 数据库为空，自动从 JSON 迁移…")
        migrate.main()

    if do_fetch and dry_run:
        print("  [dry-run] 跳过联网同步，用本地数据试算（不消耗赔率 API 配额）")
    elif do_fetch:
        fetch.sync()
        odds.sync()

    settled = db.settle_finished_bets()
    if settled:
        print(f"  [bets] 已结算 {settled} 笔投注")

    publish_played = sum(1 for m in db.load_matches()
                         if m["score"] and m["home"] and m["away"])
    if not dry_run:
        validate_publish_meta(publish_played, sims, force_publish)

    state = build_state()
    played = len(state["records"])
    if seed is None:
        seed = int(time.strftime("%Y%m%d"))  # 每日不同但可复现
    if workers is None:
        workers = os.cpu_count() or 1

    sim_out = run_simulations(state, sims, seed, workers)
    history = update_history(sim_out, played, sims)

    payload = {
        "meta": {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sims": sims, "seed": seed,
            "played": played, "total": 104,
            "market": state.get("market_live", False),
        },
        "teams": sim_out["teams"],
        "schedule": build_schedule(state),
        "record": {"stats": state["record_stats"],
                   "list": list(reversed(state["records"]))},
        "top_finals": sim_out["top_finals"],
        "top_title_matches": sim_out["top_title_matches"],
        "history": history,
    }
    if dry_run:
        print("  [dry-run] 已完成计算，未写入真实数据库或发布产物")
    else:
        write_outputs(payload)
        report.update_all(payload)
    print_report(payload, published=not dry_run)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 世界杯 2026 每日更新")
    parser.add_argument("--sims", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--no-fetch", action="store_true", help="跳过联网同步")
    parser.add_argument("--dry-run", action="store_true",
                        help="用临时数据库计算，不写入发布产物")
    parser.add_argument("--force-publish", action="store_true",
                        help="允许覆盖已赛场次/模拟次数更高的现有产物")
    args = parser.parse_args()
    run(args.sims, args.seed, not args.no_fetch, args.workers,
        args.dry_run, args.force_publish)


if __name__ == "__main__":
    main()
