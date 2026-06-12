"""SQLite 数据层：单文件 data/worldcup.db，WAL 模式，纯标准库。

设计原则：
- 数据库是唯一事实源；web/data.js 等静态产物由 DB 导出生成。
- 核心赛事表（matches/locks/...）由更新管线读写；
  竞技场表（users/bets/...）由 API 服务与 Agent 调度器读写。
- 所有写入走事务；投注/结算相关操作必须用 transaction() 包裹。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "worldcup.db"

SCHEMA = """
-- ============ 核心赛事数据 ============
CREATE TABLE IF NOT EXISTS matches (
  match_no   INTEGER PRIMARY KEY,
  round      INTEGER,
  stage      TEXT,
  grp        TEXT,
  date_utc   TEXT,
  venue      TEXT,
  home       TEXT,
  away       TEXT,
  slot_home  TEXT,
  slot_away  TEXT,
  score_home INTEGER,
  score_away INTEGER,
  winner     TEXT,
  source     TEXT DEFAULT 'feed'      -- feed / manual
);

CREATE TABLE IF NOT EXISTS locks (    -- 赛前锁档（开球后只读）
  match_no INTEGER PRIMARY KEY,
  we       REAL NOT NULL,             -- 模型+市场融合后的胜负期望
  mkt_home REAL, mkt_draw REAL, mkt_away REAL,
  books    INTEGER,
  ts       TEXT
);

CREATE TABLE IF NOT EXISTS elo_history (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  match_no  INTEGER NOT NULL,
  team      TEXT NOT NULL,
  elo_before REAL, elo_after REAL,
  ts        TEXT
);
CREATE INDEX IF NOT EXISTS idx_elo_team ON elo_history(team);
CREATE UNIQUE INDEX IF NOT EXISTS uq_elo_match_team ON elo_history(match_no, team);

CREATE TABLE IF NOT EXISTS reports (
  no      INTEGER PRIMARY KEY,
  date    TEXT, time TEXT,
  played  INTEGER,
  report  TEXT, comment TEXT
);

CREATE TABLE IF NOT EXISTS blurbs (
  match_no INTEGER PRIMARY KEY,
  text     TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS champ_history (  -- 夺冠概率每日快照（走势图）
  date    TEXT PRIMARY KEY,
  played  INTEGER, sims INTEGER,
  champion_json TEXT
);

CREATE TABLE IF NOT EXISTS odds_snapshots ( -- 盘口快照存档（审计）
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   TEXT, kind TEXT,                     -- h2h / winner
  payload_json TEXT
);

-- ============ 竞技场：用户 / 投注 / 账本 ============
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL DEFAULT 'human', -- human / agent
  github_id  INTEGER UNIQUE,
  login      TEXT,
  name       TEXT,
  avatar_url TEXT,
  model      TEXT,                          -- agent：网关模型标识
  persona    TEXT,                          -- agent：人设
  balance    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS bets (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id   INTEGER NOT NULL REFERENCES users(id),
  match_no  INTEGER NOT NULL,
  pick      TEXT NOT NULL CHECK (pick IN ('H','D','A')),
  stake     INTEGER NOT NULL CHECK (stake > 0),
  odds      REAL NOT NULL,                  -- 下注瞬间锁定的赔率
  placed_at TEXT,
  settled   INTEGER NOT NULL DEFAULT 0,
  payout    INTEGER NOT NULL DEFAULT 0,     -- 派彩含本金，输=0
  settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_match ON bets(match_no, settled);
CREATE INDEX IF NOT EXISTS idx_bets_user  ON bets(user_id);

CREATE TABLE IF NOT EXISTS wallet_ledger (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta   INTEGER NOT NULL,
  reason  TEXT,                             -- init / bet / payout / bonus
  ref_id  INTEGER,
  ts      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON wallet_ledger(user_id);

-- ============ 竞技场：Agent 私有/公共内容 ============
CREATE TABLE IF NOT EXISTS agent_notes (    -- 私有笔记，仅本 agent 可见
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   INTEGER NOT NULL REFERENCES users(id),
  title      TEXT,
  content    TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_agent ON agent_notes(agent_id);

CREATE TABLE IF NOT EXISTS agent_posts (    -- 战报圆桌跟评（公共）
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id  INTEGER NOT NULL REFERENCES users(id),
  report_no INTEGER,
  content   TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS gateway_usage (  -- 网关用量与成本记账
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT, agent TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  ok     INTEGER, note TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


@contextmanager
def transaction():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- matches --

def match_row_to_dict(r: sqlite3.Row) -> dict:
    """转成管线沿用的 dict 结构（兼容原 JSON 字段名）。"""
    score = ([r["score_home"], r["score_away"]]
             if r["score_home"] is not None and r["score_away"] is not None
             else None)
    return {
        "match": r["match_no"], "round": r["round"], "stage": r["stage"],
        "group": r["grp"], "date_utc": r["date_utc"], "venue": r["venue"],
        "home": r["home"], "away": r["away"],
        "slot_home": r["slot_home"], "slot_away": r["slot_away"],
        "score": score, "winner": r["winner"],
    }


def load_matches(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    rows = conn.execute("SELECT * FROM matches ORDER BY match_no").fetchall()
    if own:
        conn.close()
    return [match_row_to_dict(r) for r in rows]


def upsert_matches(matches: list[dict], source: str = "feed") -> None:
    """整批写入赛程。手动录入(source=manual)的比分不被 feed 覆盖。"""
    with transaction() as conn:
        for m in matches:
            score = m.get("score") or [None, None]
            existing = conn.execute(
                "SELECT source, score_home FROM matches WHERE match_no=?",
                (m["match"],)).fetchone()
            keep_manual = (existing and existing["source"] == "manual"
                           and existing["score_home"] is not None
                           and source == "feed")
            conn.execute("""
                INSERT INTO matches (match_no, round, stage, grp, date_utc,
                    venue, home, away, slot_home, slot_away,
                    score_home, score_away, winner, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_no) DO UPDATE SET
                  round=excluded.round, stage=excluded.stage, grp=excluded.grp,
                  date_utc=excluded.date_utc, venue=excluded.venue,
                  home=COALESCE(excluded.home, home),
                  away=COALESCE(excluded.away, away),
                  slot_home=excluded.slot_home, slot_away=excluded.slot_away,
                  score_home=CASE WHEN ? THEN score_home ELSE excluded.score_home END,
                  score_away=CASE WHEN ? THEN score_away ELSE excluded.score_away END,
                  winner=CASE WHEN ? THEN winner ELSE COALESCE(excluded.winner, winner) END,
                  source=CASE WHEN ? THEN source ELSE excluded.source END
            """, (m["match"], m.get("round"), m.get("stage"), m.get("group"),
                  m.get("date_utc"), m.get("venue"), m.get("home"),
                  m.get("away"), m.get("slot_home"), m.get("slot_away"),
                  score[0], score[1], m.get("winner"), source,
                  keep_manual, keep_manual, keep_manual, keep_manual))


def record_manual_score(match_no: int, gh: int, ga: int,
                        winner: str | None = None) -> None:
    with transaction() as conn:
        conn.execute("""UPDATE matches SET score_home=?, score_away=?,
                        winner=COALESCE(?, winner), source='manual'
                        WHERE match_no=?""", (gh, ga, winner, match_no))


# ------------------------------------------------------------------ locks --

def load_locks(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    rows = conn.execute("SELECT * FROM locks").fetchall()
    if own:
        conn.close()
    out = {}
    for r in rows:
        market = (None if r["mkt_home"] is None else
                  {"p_home": r["mkt_home"], "p_draw": r["mkt_draw"],
                   "p_away": r["mkt_away"], "books": r["books"]})
        out[str(r["match_no"])] = {"we": r["we"], "market": market,
                                   "ts": r["ts"]}
    return out


def save_lock(match_no: int, we: float, market: dict | None) -> None:
    with transaction() as conn:
        conn.execute("""
            INSERT INTO locks (match_no, we, mkt_home, mkt_draw, mkt_away, books, ts)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(match_no) DO UPDATE SET
              we=excluded.we, mkt_home=excluded.mkt_home,
              mkt_draw=excluded.mkt_draw, mkt_away=excluded.mkt_away,
              books=excluded.books, ts=excluded.ts
        """, (match_no, we,
              market and market.get("p_home"), market and market.get("p_draw"),
              market and market.get("p_away"), market and market.get("books"),
              now()))


def delete_lock(match_no: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM locks WHERE match_no=?", (match_no,))


# ------------------------------------------------- reports/blurbs/history --

def load_reports() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM reports ORDER BY no").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_report(entry: dict) -> None:
    with transaction() as conn:
        conn.execute("""INSERT OR REPLACE INTO reports
                        (no, date, time, played, report, comment)
                        VALUES (?,?,?,?,?,?)""",
                     (entry["no"], entry["date"], entry["time"],
                      entry["played"], entry["report"], entry.get("comment")))


def load_blurbs() -> dict:
    conn = connect()
    rows = conn.execute("SELECT * FROM blurbs").fetchall()
    conn.close()
    return {str(r["match_no"]): {"text": r["text"], "ts": r["ts"]} for r in rows}


def save_blurb(match_no: int, text: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO blurbs (match_no, text, ts) "
                     "VALUES (?,?,?)", (match_no, text, now()))


def load_champ_history() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM champ_history ORDER BY date").fetchall()
    conn.close()
    return [{"date": r["date"], "played": r["played"], "sims": r["sims"],
             "champion": json.loads(r["champion_json"])} for r in rows]


def save_champ_snapshot(date: str, played: int, sims: int, champion: dict) -> None:
    with transaction() as conn:
        conn.execute("""INSERT OR REPLACE INTO champ_history
                        (date, played, sims, champion_json) VALUES (?,?,?,?)""",
                     (date, played, sims, json.dumps(champion)))


def log_elo_change(match_no: int, changes: list[tuple]) -> None:
    """changes: [(team, before, after), ...]。幂等：同场同队只记一次。"""
    with transaction() as conn:
        for team, before, after in changes:
            conn.execute("""INSERT OR IGNORE INTO elo_history
                            (match_no, team, elo_before, elo_after, ts)
                            VALUES (?,?,?,?,?)""",
                         (match_no, team, round(before, 2), round(after, 2),
                          now()))


def snapshot_odds(kind: str, payload: dict) -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO odds_snapshots (ts, kind, payload_json) "
                     "VALUES (?,?,?)", (now(), kind, json.dumps(payload)))


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
