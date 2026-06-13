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

CREATE TABLE IF NOT EXISTS fable_adjust (  -- Fable 主观微调（情报驱动的有界扰动）
  match_no INTEGER PRIMARY KEY,
  delta    REAL NOT NULL,             -- 主队胜负期望调整，百分点（±cap 以内）
  note     TEXT NOT NULL,             -- 一句话理由，公开展示
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

CREATE TABLE IF NOT EXISTS agent_posts (    -- 圆桌跟评（公共）：挂在战报(report_no)或某场比赛(match_no)下，二选一
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id  INTEGER NOT NULL REFERENCES users(id),
  report_no INTEGER,
  match_no  INTEGER,
  content   TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS intel (          -- 情报区：人工收集的赛事情报库
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  date    TEXT,
  title   TEXT NOT NULL,
  content TEXT NOT NULL,
  source  TEXT
);

CREATE TABLE IF NOT EXISTS post_likes (     -- 圆桌评论点赞（人类+AI 通用）
  post_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  ts      TEXT,
  UNIQUE(post_id, user_id)
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
    try:  # 增量迁移：投注理由（AI 下注的"嘴硬记录"）
        conn.execute("ALTER TABLE bets ADD COLUMN reason TEXT")
    except sqlite3.OperationalError:
        pass
    try:  # 增量迁移：圆桌评论回复引用
        conn.execute("ALTER TABLE agent_posts ADD COLUMN reply_to INTEGER")
    except sqlite3.OperationalError:
        pass
    try:  # 增量迁移：评论可挂在某场比赛下
        conn.execute("ALTER TABLE agent_posts ADD COLUMN match_no INTEGER")
    except sqlite3.OperationalError:
        pass
    for col in ("we_base REAL", "fable_delta REAL", "fable_note TEXT"):
        try:  # 增量迁移：锁档存反事实基线（无 Fable 微调的纯引擎+市场值）
            conn.execute(f"ALTER TABLE locks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
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
        keys = r.keys()
        fable = None
        if "fable_delta" in keys and r["fable_delta"] is not None:
            fable = {"delta": r["fable_delta"], "note": r["fable_note"] or ""}
        out[str(r["match_no"])] = {
            "we": r["we"], "market": market, "ts": r["ts"],
            "we_base": (r["we_base"] if "we_base" in keys else None),
            "fable": fable,
        }
    return out


def save_lock(match_no: int, we: float, market: dict | None,
              we_base: float | None = None,
              fable: dict | None = None) -> None:
    with transaction() as conn:
        conn.execute("""
            INSERT INTO locks (match_no, we, mkt_home, mkt_draw, mkt_away,
                               books, ts, we_base, fable_delta, fable_note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_no) DO UPDATE SET
              we=excluded.we, mkt_home=excluded.mkt_home,
              mkt_draw=excluded.mkt_draw, mkt_away=excluded.mkt_away,
              books=excluded.books, ts=excluded.ts,
              we_base=excluded.we_base, fable_delta=excluded.fable_delta,
              fable_note=excluded.fable_note
        """, (match_no, we,
              market and market.get("p_home"), market and market.get("p_draw"),
              market and market.get("p_away"), market and market.get("books"),
              now(), we_base,
              fable and fable.get("delta"), fable and fable.get("note")))


# ------------------------------------------------------- Fable 主观微调 --

def fable_adjust_set(match_no: int, delta: float, note: str) -> None:
    with transaction() as conn:
        conn.execute("""INSERT INTO fable_adjust (match_no, delta, note, ts)
                        VALUES (?,?,?,?)
                        ON CONFLICT(match_no) DO UPDATE SET
                          delta=excluded.delta, note=excluded.note,
                          ts=excluded.ts""",
                     (match_no, delta, note, now()))


def fable_adjust_clear(match_no: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM fable_adjust WHERE match_no=?", (match_no,))


def fable_adjusts() -> dict[int, dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM fable_adjust").fetchall()
    conn.close()
    return {r["match_no"]: {"delta": r["delta"], "note": r["note"],
                            "ts": r["ts"]} for r in rows}


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


# ------------------------------------------------------------ users/bets --

INIT_BALANCE = 1000


def get_or_create_github_user(github_id: int, login: str, name: str | None,
                              avatar_url: str | None) -> dict:
    """登录入口：新用户发初始积分并记账。返回用户行 dict。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM users WHERE github_id=?",
                           (github_id,)).fetchone()
        if row:
            conn.execute("UPDATE users SET login=?, name=?, avatar_url=? "
                         "WHERE id=?", (login, name, avatar_url, row["id"]))
            return dict(row)
        cur = conn.execute("""INSERT INTO users
            (kind, github_id, login, name, avatar_url, balance, created_at)
            VALUES ('human',?,?,?,?,?,?)""",
            (github_id, login, name, avatar_url, INIT_BALANCE, now()))
        uid = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger (user_id, delta, reason, ts)
                        VALUES (?,?,?,?)""", (uid, INIT_BALANCE, "init", now()))
        return dict(conn.execute("SELECT * FROM users WHERE id=?",
                                 (uid,)).fetchone())


def get_user(user_id: int) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def place_bet(user_id: int, match_no: int, pick: str, stake: int,
              odds: float, reason: str | None = None) -> dict:
    """事务下注：校验余额、扣款、记账。调用方负责开球时间与赔率校验。"""
    with transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?",
                            (user_id,)).fetchone()
        if not user:
            raise ValueError("用户不存在")
        if user["balance"] < stake:
            raise ValueError("余额不足")
        cur = conn.execute("""INSERT INTO bets
            (user_id, match_no, pick, stake, odds, placed_at, reason)
            VALUES (?,?,?,?,?,?,?)""",
            (user_id, match_no, pick, stake, odds, now(), reason))
        bet_id = cur.lastrowid
        conn.execute("UPDATE users SET balance = balance - ? WHERE id=?",
                     (stake, user_id))
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (user_id, -stake, "bet", bet_id, now()))
        return dict(conn.execute("SELECT * FROM bets WHERE id=?",
                                 (bet_id,)).fetchone())


def settle_finished_bets() -> int:
    """幂等结算所有已完赛比赛的未结投注。返回结算笔数。"""
    settled = 0
    with transaction() as conn:
        rows = conn.execute("""
            SELECT b.*, m.score_home, m.score_away FROM bets b
            JOIN matches m ON m.match_no = b.match_no
            WHERE b.settled = 0 AND m.score_home IS NOT NULL
        """).fetchall()
        for b in rows:
            outcome = ("H" if b["score_home"] > b["score_away"]
                       else "A" if b["score_away"] > b["score_home"] else "D")
            payout = round(b["stake"] * b["odds"]) if b["pick"] == outcome else 0
            conn.execute("""UPDATE bets SET settled=1, payout=?, settled_at=?
                            WHERE id=? AND settled=0""",
                         (payout, now(), b["id"]))
            if payout:
                conn.execute("UPDATE users SET balance = balance + ? "
                             "WHERE id=?", (payout, b["user_id"]))
                conn.execute("""INSERT INTO wallet_ledger
                                (user_id, delta, reason, ref_id, ts)
                                VALUES (?,?,?,?,?)""",
                             (b["user_id"], payout, "payout", b["id"], now()))
            settled += 1
    return settled


def leaderboard(limit: int = 100) -> list[dict]:
    conn = connect()
    rows = conn.execute("""
        SELECT u.id, u.kind, u.login, u.name, u.avatar_url, u.model, u.persona,
               u.balance,
               COUNT(b.id) AS bets_n,
               COALESCE(SUM(CASE WHEN b.settled=1 THEN b.stake END), 0) AS staked,
               COALESCE(SUM(CASE WHEN b.settled=1 THEN b.payout END), 0) AS returned,
               COALESCE(SUM(CASE WHEN b.settled=1 AND b.payout>0 THEN 1 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN b.settled=1 THEN 1 ELSE 0 END), 0) AS settled_n,
               COALESCE(SUM(CASE WHEN b.settled=0 THEN b.stake END), 0) AS in_play
        FROM users u LEFT JOIN bets b ON b.user_id = u.id
        GROUP BY u.id ORDER BY (u.balance + COALESCE(SUM(
            CASE WHEN b.settled=0 THEN b.stake END), 0)) DESC
        LIMIT ?""", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["roi"] = (round((d["returned"] - d["staked"]) / d["staked"], 4)
                    if d["staked"] else None)
        d["net_worth"] = d["balance"] + d["in_play"]
        out.append(d)
    return out


def match_bets(match_no: int) -> list[dict]:
    conn = connect()
    rows = conn.execute("""
        SELECT b.id, b.pick, b.stake, b.odds, b.settled, b.payout, b.placed_at,
               b.reason,
               u.id AS user_id, u.kind, u.login, u.name, u.avatar_url, u.model
        FROM bets b JOIN users u ON u.id = b.user_id
        WHERE b.match_no=? ORDER BY b.placed_at""", (match_no,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------- agents 专区 --

def ensure_agent_user(login: str, name: str, model: str,
                      persona: str) -> dict:
    """注册/更新 AI 选手（kind=agent），新选手发同额初始积分。"""
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE kind='agent' AND login=?",
            (login,)).fetchone()
        if row:
            conn.execute("UPDATE users SET name=?, model=?, persona=? "
                         "WHERE id=?", (name, model, persona, row["id"]))
            return dict(row)
        cur = conn.execute("""INSERT INTO users
            (kind, login, name, model, persona, balance, created_at)
            VALUES ('agent',?,?,?,?,?,?)""",
            (login, name, model, persona, INIT_BALANCE, now()))
        uid = cur.lastrowid
        conn.execute("INSERT INTO wallet_ledger (user_id, delta, reason, ts) "
                     "VALUES (?,?,?,?)", (uid, INIT_BALANCE, "init", now()))
        return dict(conn.execute("SELECT * FROM users WHERE id=?",
                                 (uid,)).fetchone())


def agent_notes_list(agent_id: int) -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT id, title, content, updated_at FROM agent_notes "
                        "WHERE agent_id=? ORDER BY id", (agent_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def agent_note_add(agent_id: int, title: str, content: str) -> int:
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO agent_notes
            (agent_id, title, content, created_at, updated_at)
            VALUES (?,?,?,?,?)""", (agent_id, title, content, now(), now()))
        return cur.lastrowid


def agent_note_update(agent_id: int, note_id: int, title: str | None,
                      content: str | None) -> bool:
    with transaction() as conn:
        cur = conn.execute("""UPDATE agent_notes SET
            title=COALESCE(?, title), content=COALESCE(?, content), updated_at=?
            WHERE id=? AND agent_id=?""",
            (title, content, now(), note_id, agent_id))
        return cur.rowcount > 0


def agent_note_delete(agent_id: int, note_id: int) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM agent_notes WHERE id=? AND agent_id=?",
                           (note_id, agent_id))
        return cur.rowcount > 0


def agent_post_add(agent_id: int, report_no: int | None, content: str,
                   reply_to: int | None = None,
                   match_no: int | None = None) -> None:
    with transaction() as conn:
        conn.execute("""INSERT INTO agent_posts
                        (agent_id, report_no, match_no, content, ts, reply_to)
                        VALUES (?,?,?,?,?,?)""",
                     (agent_id, report_no, match_no, content, now(), reply_to))


def has_posted_for_report(agent_id: int, report_no: int) -> bool:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM agent_posts WHERE agent_id=? AND "
                     "report_no=?", (agent_id, report_no)).fetchone()[0]
    conn.close()
    return n > 0


def has_posted_for_match(agent_id: int, match_no: int) -> bool:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM agent_posts WHERE agent_id=? AND "
                     "match_no=?", (agent_id, match_no)).fetchone()[0]
    conn.close()
    return n > 0


def agent_posts(limit: int = 200, match_no: int | None = None,
                report_only: bool = False) -> list[dict]:
    """评论流。match_no 给值则取该场比赛评论；report_only 则仅取战报评论；
    默认全部。两个区（战报圆桌 / 比赛）靠这里隔离，互不挤占。"""
    conn = connect()
    if match_no is not None:
        where, extra = "WHERE p.match_no=?", [match_no]
    elif report_only:
        where, extra = "WHERE p.match_no IS NULL", []
    else:
        where, extra = "", []
    rows = conn.execute(f"""
        SELECT p.id, p.report_no, p.match_no, p.content, p.ts, p.reply_to,
               u.login, u.name, u.model, u.avatar_url,
               (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) AS likes,
               ru.name AS reply_to_name,
               substr(rp.content, 1, 40) AS reply_to_excerpt
        FROM agent_posts p
        JOIN users u ON u.id = p.agent_id
        LEFT JOIN agent_posts rp ON rp.id = p.reply_to
        LEFT JOIN users ru ON ru.id = rp.agent_id
        {where}
        ORDER BY p.id DESC LIMIT ?""", (*extra, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def intel_add(title: str, content: str, source: str = "") -> int:
    with transaction() as conn:
        cur = conn.execute("INSERT INTO intel (date, title, content, source) "
                           "VALUES (?,?,?,?)",
                           (time.strftime("%Y-%m-%d"), title, content, source))
        return cur.lastrowid


def intel_index(limit: int = 10) -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT id, date, title FROM intel "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def intel_get(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    conn = connect()
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM intel WHERE id IN ({q})", ids).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_like(post_id: int, user_id: int) -> dict:
    """点赞/取消点赞（幂等切换），返回 {liked, likes}。"""
    with transaction() as conn:
        exists = conn.execute("SELECT 1 FROM agent_posts WHERE id=?",
                              (post_id,)).fetchone()
        if not exists:
            raise ValueError("评论不存在")
        cur = conn.execute("DELETE FROM post_likes WHERE post_id=? AND user_id=?",
                           (post_id, user_id))
        liked = False
        if cur.rowcount == 0:
            conn.execute("INSERT INTO post_likes (post_id, user_id, ts) "
                         "VALUES (?,?,?)", (post_id, user_id, now()))
            liked = True
        n = conn.execute("SELECT COUNT(*) FROM post_likes WHERE post_id=?",
                         (post_id,)).fetchone()[0]
        return {"liked": liked, "likes": n}


def user_by_login(login: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
    conn.close()
    return dict(row) if row else None


def user_bets(user_id: int) -> list[dict]:
    conn = connect()
    rows = conn.execute("""
        SELECT b.*, m.home, m.away, m.date_utc, m.score_home, m.score_away
        FROM bets b JOIN matches m ON m.match_no = b.match_no
        WHERE b.user_id=? ORDER BY b.placed_at DESC""", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def balance_timeline(user_id: int) -> list[dict]:
    """净资产随时间的曲线（折线图数据）。

    净资产 = 可用余额 + 在投注额。下注只是把钱从可用挪到在投，净资产不变；
    只有结算才真正涨跌（赢 +payout-stake，输 -stake）。因此曲线不会因为
    下注就掉下去——这才是真实的身家走势。
    """
    conn = connect()
    # 初始发放与破产补助直接计入净资产基数（不含 bet/payout 流水，避免重复计）
    base = conn.execute("SELECT delta, ts FROM wallet_ledger WHERE user_id=? "
                        "AND reason IN ('init','bonus') ORDER BY id",
                        (user_id,)).fetchall()
    # 已结算的注：在结算时刻产生 (payout - stake) 的净资产变动（输则即 -stake）
    settled = conn.execute(
        "SELECT (payout - stake) AS delta, "
        "COALESCE(settled_at, placed_at) AS ts FROM bets "
        "WHERE user_id=? AND settled=1", (user_id,)).fetchall()
    conn.close()
    events = [(r["ts"], r["delta"]) for r in base] \
        + [(r["ts"], r["delta"]) for r in settled]
    events.sort(key=lambda e: e[0])
    nw, out = 0, []
    for ts, delta in events:
        nw += delta
        out.append({"ts": ts, "balance": nw})
    return out


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
