"""一次性迁移：data/*.json → data/worldcup.db。

幂等：重复运行结果一致（全部 upsert）。迁移后打印校验摘要。
teams.json / strengths.json / config.json 保持 JSON（静态配置与密钥）。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _load(name: str, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def main() -> None:
    db.init_db()

    # 1) 赛程（含手动录入合并：manual_results 优先并标记 source=manual）
    matches = _load("matches.json", [])
    manual = _load("manual_results.json", {})
    db.upsert_matches(matches, source="feed")
    for no, v in manual.items():
        score = v.get("score")
        if score:
            db.record_manual_score(int(no), score[0], score[1], v.get("winner"))

    # 2) 赛前锁档
    locks = _load("locked_preds.json", {})
    for no, lk in locks.items():
        db.save_lock(int(no), lk["we"], lk.get("market"))

    # 3) 战报 / 看点 / 夺冠快照
    for r in _load("reports.json", []):
        db.save_report(r)
    for no, b in _load("blurbs.json", {}).items():
        db.save_blurb(int(no), b["text"])
    for h in _load("history.json", []):
        db.save_champ_snapshot(h["date"], h.get("played", 0),
                               h.get("sims", 0), h["champion"])

    # 4) 当前盘口缓存存一份审计快照
    odds = _load("odds.json", None)
    if odds:
        db.snapshot_odds("h2h", odds.get("h2h", {}))
        db.snapshot_odds("winner", odds.get("winner", {}))

    # ---- 校验 ----
    conn = db.connect()
    for table in ("matches", "locks", "reports", "blurbs",
                  "champ_history", "odds_snapshots"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<16} {n} 行")
    played = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE score_home IS NOT NULL").fetchone()[0]
    manual_n = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE source='manual'").fetchone()[0]
    print(f"  已赛 {played} 场（手动录入 {manual_n} 场）")
    conn.close()


if __name__ == "__main__":
    main()
