"""SQLite 数据层：单文件 data/worldcup.db，WAL 模式，纯标准库。

设计原则：
- 数据库是唯一事实源；web/data.js 等静态产物由 DB 导出生成。
- 核心赛事表（matches/locks/...）由更新管线读写；
  竞技场表（users/bets/...）由 API 服务与 Agent 调度器读写。
- 所有写入走事务；预测/结算相关操作必须用 transaction() 包裹。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "worldcup.db"
PUBLIC_ADVISOR_LOGIN = "codex"
PUBLIC_ADVISOR_NAME = "Codex"
PUBLIC_ADVISOR_MODEL = "codex"
PUBLIC_ADVISOR_STYLE = "赛事情报审稿员，把临场消息折成克制的概率修正。"
LEGACY_ADVISOR_LOGIN = "fable"
LEGACY_ADVISOR_LOGINS = {LEGACY_ADVISOR_LOGIN, "claude-code"}
MATCH_SOURCE_PRIORITY = {"feed": 0, "espn": 1, "manual": 2}


def _login_key(login: str | None) -> str:
    return str(login or "").strip().lower()


def _is_legacy_advisor(login: str | None = None, name: str | None = None,
                       model: str | None = None) -> bool:
    # 只认历史账号 login。模型名里可能包含 fable，不能据此改写参赛选手身份。
    return _login_key(login) in LEGACY_ADVISOR_LOGINS


def _is_public_advisor_login(login: str | None) -> bool:
    return _login_key(login) in {PUBLIC_ADVISOR_LOGIN, *LEGACY_ADVISOR_LOGINS}


def _match_source_priority(source: str | None) -> int:
    return MATCH_SOURCE_PRIORITY.get(str(source or "feed").lower(), 0)

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
  settle_score_home INTEGER,          -- 投注/预测战绩结算比分；淘汰赛加时/点球时通常为90分钟比分
  settle_score_away INTEGER,
  score_type TEXT DEFAULT 'regular',  -- regular / final_aet / penalties
  winner     TEXT,
  source     TEXT DEFAULT 'feed'      -- feed / espn / manual
);

CREATE TABLE IF NOT EXISTS locks (    -- 赛前锁档（开球后只读）
  match_no INTEGER PRIMARY KEY,
  we       REAL NOT NULL,             -- 模型+市场融合后的胜负期望
  pred_home REAL, pred_draw REAL, pred_away REAL,
  base_home REAL, base_draw REAL, base_away REAL,
  total_goals REAL, total_goals_base REAL,
  mkt_home REAL, mkt_draw REAL, mkt_away REAL,
  mkt_total_goals REAL, total_books INTEGER,
  books    INTEGER,
  ts       TEXT
);

CREATE TABLE IF NOT EXISTS fable_adjust (  -- 主观微调（情报驱动的有界扰动，历史表名保留兼容）
  match_no INTEGER PRIMARY KEY,
  delta    REAL NOT NULL,             -- 主队胜负期望调整，百分点（±cap 以内）
  draw_delta REAL DEFAULT 0,           -- 平局概率调整，百分点
  total_delta REAL DEFAULT 0,          -- 总进球期望调整，球
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

CREATE TABLE IF NOT EXISTS champ_history (  -- 每日快照（走势图 + 本轮影响面板）
  date    TEXT PRIMARY KEY,
  played  INTEGER, sims INTEGER,
  champion_json TEXT,                        -- Top12 夺冠概率（走势图）
  advance_json  TEXT                         -- 全队出线（晋级32强）概率（本轮影响面板）
);

CREATE TABLE IF NOT EXISTS odds_snapshots ( -- 市场参考快照存档（审计）
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   TEXT, kind TEXT,                     -- h2h / winner
  payload_json TEXT
);

-- ============ 竞技场：用户 / 预测 / 账本 ============
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
  odds      REAL NOT NULL,                  -- 提交预测瞬间锁定的回报系数
  placed_at TEXT,
  settled   INTEGER NOT NULL DEFAULT 0,
  payout    INTEGER NOT NULL DEFAULT 0,     -- 结算得分含本金，输=0
  settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_match ON bets(match_no, settled);
CREATE INDEX IF NOT EXISTS idx_bets_user  ON bets(user_id);

CREATE TABLE IF NOT EXISTS score_bets (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id   INTEGER NOT NULL REFERENCES users(id),
  match_no  INTEGER NOT NULL,
  home_score INTEGER NOT NULL CHECK (home_score >= 0 AND home_score <= 10),
  away_score INTEGER NOT NULL CHECK (away_score >= 0 AND away_score <= 10),
  stake     INTEGER NOT NULL CHECK (stake > 0),
  odds      REAL NOT NULL,
  placed_at TEXT,
  settled   INTEGER NOT NULL DEFAULT 0,
  payout    INTEGER NOT NULL DEFAULT 0,
  settled_at TEXT,
  reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_score_bets_match ON score_bets(match_no, settled);
CREATE INDEX IF NOT EXISTS idx_score_bets_user  ON score_bets(user_id);

CREATE TABLE IF NOT EXISTS wallet_ledger (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta   INTEGER NOT NULL,
  reason  TEXT,                             -- init / bet / payout / bonus
  ref_id  INTEGER,
  ts      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON wallet_ledger(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_reason_ref
ON wallet_ledger(reason, ref_id)
WHERE ref_id IS NOT NULL AND reason IN ('bet', 'payout');
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_score_reason_ref
ON wallet_ledger(reason, ref_id)
WHERE ref_id IS NOT NULL AND reason IN ('score_bet', 'score_payout');

CREATE TABLE IF NOT EXISTS system_loans (
  user_id            INTEGER PRIMARY KEY REFERENCES users(id),
  principal_borrowed INTEGER NOT NULL DEFAULT 0,  -- 终身累计系统本金，封顶
  debt               INTEGER NOT NULL DEFAULT 0,  -- 当前系统债务，含复利利息
  interest_accrued   INTEGER NOT NULL DEFAULT 0,
  last_interest_date TEXT,
  created_at         TEXT,
  updated_at         TEXT,
  CHECK (principal_borrowed >= 0),
  CHECK (debt >= 0),
  CHECK (interest_accrued >= 0)
);

CREATE TABLE IF NOT EXISTS system_loan_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL REFERENCES users(id),
  kind            TEXT NOT NULL,                  -- borrow / repay / interest
  amount          INTEGER NOT NULL DEFAULT 0,
  principal_delta INTEGER NOT NULL DEFAULT 0,
  debt_delta      INTEGER NOT NULL DEFAULT 0,
  rate            REAL,
  ref_date        TEXT,
  note            TEXT,
  ts              TEXT
);
CREATE INDEX IF NOT EXISTS idx_system_loan_events_user
ON system_loan_events(user_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_system_loan_interest_day
ON system_loan_events(user_id, kind, ref_date)
WHERE kind='interest' AND ref_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS daily_agent_rewards (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  reward_date   TEXT NOT NULL UNIQUE,
  user_id       INTEGER REFERENCES users(id),
  amount        INTEGER NOT NULL DEFAULT 0,
  score         INTEGER NOT NULL DEFAULT 0,
  settled_bets  INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'awarded',  -- awarded / skipped
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_agent_rewards_user
ON daily_agent_rewards(user_id, reward_date DESC);

CREATE TABLE IF NOT EXISTS agent_investments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  borrower_id         INTEGER NOT NULL REFERENCES users(id),
  lender_id           INTEGER NOT NULL REFERENCES users(id),
  amount              INTEGER NOT NULL CHECK (amount > 0),
  profit_share        REAL NOT NULL CHECK (profit_share >= 0 AND profit_share <= 1),
  status              TEXT NOT NULL DEFAULT 'pending', -- pending / active / declined / settled
  reason              TEXT,
  response_reason     TEXT,
  principal_remaining INTEGER NOT NULL DEFAULT 0,
  profit_paid         INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT,
  responded_at        TEXT,
  closed_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_investments_pending ON agent_investments(status, lender_id);
CREATE INDEX IF NOT EXISTS idx_investments_borrower ON agent_investments(borrower_id, status);
CREATE INDEX IF NOT EXISTS idx_investments_lender ON agent_investments(lender_id, status);

CREATE TABLE IF NOT EXISTS agent_tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id         INTEGER NOT NULL REFERENCES users(id),
  title            TEXT NOT NULL,
  instruction      TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active', -- active / done / expired / cancelled
  priority         INTEGER NOT NULL DEFAULT 0,
  max_public_posts INTEGER NOT NULL DEFAULT 3,
  trigger_keyword  TEXT,
  created_at       TEXT,
  expires_at       TEXT,
  completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent
ON agent_tasks(agent_id, status, priority DESC);

CREATE TABLE IF NOT EXISTS funding_invites (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  borrower_id   INTEGER NOT NULL REFERENCES users(id),
  post_id       INTEGER REFERENCES agent_posts(id),
  min_amount    INTEGER NOT NULL DEFAULT 10,
  max_amount    INTEGER NOT NULL DEFAULT 100,
  desired_amount INTEGER,
  profit_share  REAL NOT NULL CHECK (profit_share >= 0 AND profit_share <= 0.9),
  status        TEXT NOT NULL DEFAULT 'open', -- open / closed / expired
  reason        TEXT,
  created_at    TEXT,
  expires_at    TEXT,
  closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_funding_invites_open
ON funding_invites(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_funding_invites_borrower
ON funding_invites(borrower_id, status);

CREATE TABLE IF NOT EXISTS funding_invite_contributions (
  invite_id     INTEGER NOT NULL REFERENCES funding_invites(id),
  lender_id     INTEGER NOT NULL REFERENCES users(id),
  investment_id INTEGER NOT NULL REFERENCES agent_investments(id),
  amount        INTEGER NOT NULL,
  created_at    TEXT,
  UNIQUE(invite_id, lender_id)
);
CREATE INDEX IF NOT EXISTS idx_funding_invite_contrib_lender
ON funding_invite_contributions(lender_id);

CREATE TABLE IF NOT EXISTS agent_affinities (
  agent_id        INTEGER NOT NULL REFERENCES users(id),
  target_agent_id INTEGER NOT NULL REFERENCES users(id),
  score           INTEGER NOT NULL DEFAULT 100,
  note            TEXT,
  updated_at      TEXT,
  PRIMARY KEY (agent_id, target_agent_id),
  CHECK (agent_id != target_agent_id),
  CHECK (score >= 0 AND score <= 200)
);
CREATE INDEX IF NOT EXISTS idx_affinities_agent ON agent_affinities(agent_id, score DESC);

-- ============ 竞技场：Agent 私有/公共内容 ============
CREATE TABLE IF NOT EXISTS agent_notes (    -- 私有笔记，仅本 agent 可见
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   INTEGER NOT NULL REFERENCES users(id),
  title      TEXT,
  content    TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_agent ON agent_notes(agent_id);

CREATE TABLE IF NOT EXISTS agent_posts (    -- AI 讨论区（公共）：topic_* 是统一话题，旧 report_no/match_no 保留兼容
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id  INTEGER NOT NULL REFERENCES users(id),
  report_no INTEGER,
  match_no  INTEGER,
  topic_type TEXT DEFAULT 'general',        -- general / match / report
  topic_id   INTEGER,
  topic_label TEXT,
  thread_id INTEGER,
  content   TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS intel (          -- 情报区：人工收集的赛事情报库
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  date    TEXT,
  title   TEXT NOT NULL,
  content TEXT NOT NULL,
  source  TEXT,
  match_no INTEGER,
  source_url TEXT,
  content_hash TEXT,
  tags TEXT,
  confidence REAL,
  kind TEXT,
  impact_score REAL,
  impact_level TEXT,
  impact_axes TEXT,
  entities TEXT,
  uncertainty TEXT
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

CREATE TABLE IF NOT EXISTS betting_reviews ( -- Agent 下注纪律复盘，喂给下一轮上下文
  review_date TEXT PRIMARY KEY,
  generated_at TEXT,
  lookback_hours INTEGER,
  settled_after TEXT,
  settled_bets INTEGER,
  score_bets INTEGER,
  summary_text TEXT,
  metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS agent_actions (  -- 统一 Agent 行动审计日志
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT,
  agent_id  INTEGER REFERENCES users(id),
  agent_login TEXT,
  action    TEXT NOT NULL,
  status    TEXT NOT NULL,
  message   TEXT,
  target_json TEXT,
  payload_json TEXT,
  created_refs_json TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_agent
ON agent_actions(agent_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_action
ON agent_actions(action, id DESC);
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
    try:  # 增量迁移：预测理由（AI 提交预测的"嘴硬记录"）
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
    for col in ("topic_type TEXT", "topic_id INTEGER", "topic_label TEXT",
                "thread_id INTEGER"):
        try:  # 增量迁移：统一 AI 讨论区话题字段
            conn.execute(f"ALTER TABLE agent_posts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""UPDATE agent_posts
                    SET topic_type = CASE
                      WHEN match_no IS NOT NULL THEN 'match'
                      WHEN report_no IS NOT NULL THEN 'report'
                      ELSE 'general' END
                    WHERE topic_type IS NULL OR topic_type=''""")
    conn.execute("""UPDATE agent_posts
                    SET topic_id = CASE
                      WHEN topic_type='match' THEN match_no
                      WHEN topic_type='report' THEN report_no
                      ELSE NULL END
                    WHERE topic_id IS NULL""")
    conn.execute("""UPDATE agent_posts
                    SET topic_label = CASE
                      WHEN topic_type='match' THEN '比赛#' || match_no
                      WHEN topic_type='report' THEN '战报#' || report_no
                      ELSE 'AI讨论' END
                    WHERE topic_label IS NULL OR topic_label=''""")
    _backfill_agent_post_threads(conn)
    try:  # 增量迁移：走势快照存出线概率（本轮影响面板）
        conn.execute("ALTER TABLE champ_history ADD COLUMN advance_json TEXT")
    except sqlite3.OperationalError:
        pass
    for col in (
        "settle_score_home INTEGER", "settle_score_away INTEGER",
        "score_type TEXT DEFAULT 'regular'",
    ):
        try:  # 增量迁移：最终比分与投注结算比分分离
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""UPDATE matches
                    SET score_type='regular'
                    WHERE score_type IS NULL OR score_type=''""")
    for col in (
        "we_base REAL", "fable_delta REAL", "fable_note TEXT",
        "pred_home REAL", "pred_draw REAL", "pred_away REAL",
        "base_home REAL", "base_draw REAL", "base_away REAL",
        "total_goals REAL", "total_goals_base REAL",
        "mkt_total_goals REAL", "total_books INTEGER",
        "fable_draw REAL", "fable_total REAL",
    ):
        try:  # 增量迁移：锁档存反事实基线（无主观微调的纯引擎+市场值）
            conn.execute(f"ALTER TABLE locks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("draw_delta REAL DEFAULT 0", "total_delta REAL DEFAULT 0"):
        try:  # 增量迁移：Codex 结构化微调（平局/总进球）
            conn.execute(f"ALTER TABLE fable_adjust ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("match_no INTEGER", "source_url TEXT", "content_hash TEXT",
                "tags TEXT", "confidence REAL", "kind TEXT",
                "impact_score REAL", "impact_level TEXT",
                "impact_axes TEXT", "entities TEXT", "uncertainty TEXT"):
        try:  # 增量迁移：自动情报收集的去重和比赛关联字段
            conn.execute(f"ALTER TABLE intel ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_intel_match
                    ON intel(match_no, id DESC)""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_intel_source_url
                    ON intel(source_url)
                    WHERE source_url IS NOT NULL AND source_url != ''""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_intel_content_hash
                    ON intel(content_hash)
                    WHERE content_hash IS NOT NULL AND content_hash != ''""")
    conn.execute("""UPDATE users
                    SET login=?, name=?, model=?,
                        persona=COALESCE(NULLIF(persona, ''), ?)
                    WHERE kind='agent'
                      AND lower(COALESCE(login, '')) IN (?, ?)""",
                 (PUBLIC_ADVISOR_LOGIN, PUBLIC_ADVISOR_NAME,
                  PUBLIC_ADVISOR_MODEL, PUBLIC_ADVISOR_STYLE,
                  LEGACY_ADVISOR_LOGIN, "claude-code"))
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
    score_type = r["score_type"] if "score_type" in r.keys() else "regular"
    if ("settle_score_home" in r.keys()
            and r["settle_score_home"] is not None
            and r["settle_score_away"] is not None):
        settle_score = [r["settle_score_home"], r["settle_score_away"]]
    elif str(score_type or "regular").lower() in {"final_aet", "penalties"}:
        settle_score = None
    else:
        settle_score = score
    return {
        "match": r["match_no"], "round": r["round"], "stage": r["stage"],
        "group": r["grp"], "date_utc": r["date_utc"], "venue": r["venue"],
        "home": r["home"], "away": r["away"],
        "slot_home": r["slot_home"], "slot_away": r["slot_away"],
        "score": score, "settle_score": settle_score,
        "score_type": score_type,
        "winner": r["winner"], "source": r["source"],
    }


def load_matches(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    rows = conn.execute("SELECT * FROM matches ORDER BY match_no").fetchall()
    if own:
        conn.close()
    return [match_row_to_dict(r) for r in rows]


def upsert_matches(matches: list[dict], source: str = "feed") -> None:
    """整批写入赛程；比分来源优先级为 manual > espn > feed。"""
    with transaction() as conn:
        for m in matches:
            score = m.get("score") or [None, None]
            settle_score = m.get("settle_score") or [None, None]
            score_type = m.get("score_type") or (
                "regular" if score[0] is not None and score[1] is not None else None)
            incoming_has_score = score[0] is not None and score[1] is not None
            existing = conn.execute(
                "SELECT source, score_home, score_away FROM matches WHERE match_no=?",
                (m["match"],)).fetchone()
            existing_has_score = (
                existing is not None
                and existing["score_home"] is not None
                and existing["score_away"] is not None
            )
            keep_existing_score = bool(
                existing_has_score
                and (
                    not incoming_has_score
                    or _match_source_priority(existing["source"])
                    > _match_source_priority(source)
                )
            )
            conn.execute("""
                INSERT INTO matches (match_no, round, stage, grp, date_utc,
                    venue, home, away, slot_home, slot_away,
                    score_home, score_away, settle_score_home,
                    settle_score_away, score_type, winner, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_no) DO UPDATE SET
                  round=excluded.round, stage=excluded.stage, grp=excluded.grp,
                  date_utc=excluded.date_utc, venue=excluded.venue,
                  home=COALESCE(excluded.home, home),
                  away=COALESCE(excluded.away, away),
                  slot_home=excluded.slot_home, slot_away=excluded.slot_away,
                  score_home=CASE WHEN ? THEN score_home ELSE excluded.score_home END,
                  score_away=CASE WHEN ? THEN score_away ELSE excluded.score_away END,
                  settle_score_home=CASE WHEN ? THEN settle_score_home ELSE excluded.settle_score_home END,
                  settle_score_away=CASE WHEN ? THEN settle_score_away ELSE excluded.settle_score_away END,
                  score_type=CASE WHEN ? THEN score_type ELSE excluded.score_type END,
                  winner=CASE WHEN ? THEN winner ELSE excluded.winner END,
                  source=CASE WHEN ? THEN source ELSE excluded.source END
            """, (m["match"], m.get("round"), m.get("stage"), m.get("group"),
                  m.get("date_utc"), m.get("venue"), m.get("home"),
                  m.get("away"), m.get("slot_home"), m.get("slot_away"),
                  score[0], score[1], settle_score[0], settle_score[1],
                  score_type, m.get("winner"), source,
                  keep_existing_score, keep_existing_score,
                  keep_existing_score, keep_existing_score,
                  keep_existing_score,
                  keep_existing_score, keep_existing_score))


def record_manual_score(match_no: int, gh: int, ga: int,
                        winner: str | None = None,
                        settle_score: tuple[int, int] | None = None,
                        score_type: str = "regular") -> None:
    settle_home = settle_score[0] if settle_score else None
    settle_away = settle_score[1] if settle_score else None
    with transaction() as conn:
        conn.execute("""UPDATE matches SET score_home=?, score_away=?,
                        settle_score_home=?, settle_score_away=?,
                        score_type=?, winner=COALESCE(?, winner),
                        source='manual'
                        WHERE match_no=?""",
                     (gh, ga, settle_home, settle_away, score_type,
                      winner, match_no))


# ------------------------------------------------------------------ locks --

def load_locks(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    rows = conn.execute("SELECT * FROM locks").fetchall()
    if own:
        conn.close()
    out = {}
    for r in rows:
        keys = r.keys()
        market = (None if r["mkt_home"] is None else
                  {"p_home": r["mkt_home"], "p_draw": r["mkt_draw"],
                   "p_away": r["mkt_away"], "books": r["books"]})
        if market and "mkt_total_goals" in keys and r["mkt_total_goals"] is not None:
            market["total_goals"] = r["mkt_total_goals"]
            market["total_books"] = r["total_books"]
        probs = None
        if "pred_home" in keys and r["pred_home"] is not None:
            probs = {"p_home": r["pred_home"], "p_draw": r["pred_draw"],
                     "p_away": r["pred_away"]}
        probs_base = None
        if "base_home" in keys and r["base_home"] is not None:
            probs_base = {"p_home": r["base_home"], "p_draw": r["base_draw"],
                          "p_away": r["base_away"]}
        fable = None
        if ("fable_delta" in keys and r["fable_delta"] is not None) or \
                ("fable_draw" in keys and r["fable_draw"]) or \
                ("fable_total" in keys and r["fable_total"]):
            fable = {
                "delta": r["fable_delta"] or 0,
                "draw": (r["fable_draw"] if "fable_draw" in keys else 0) or 0,
                "total": (r["fable_total"] if "fable_total" in keys else 0) or 0,
                "note": r["fable_note"] or "",
            }
        out[str(r["match_no"])] = {
            "we": r["we"], "market": market, "ts": r["ts"],
            "we_base": (r["we_base"] if "we_base" in keys else None),
            "probs": probs,
            "probs_base": probs_base,
            "total_goals": (r["total_goals"] if "total_goals" in keys else None),
            "total_goals_base": (r["total_goals_base"]
                                 if "total_goals_base" in keys else None),
            "fable": fable,
        }
    return out


def save_lock(match_no: int, we: float, market: dict | None,
              we_base: float | None = None,
              fable: dict | None = None,
              probs: dict | None = None,
              probs_base: dict | None = None,
              total_goals: float | None = None,
              total_goals_base: float | None = None) -> None:
    with transaction() as conn:
        conn.execute("""
            INSERT INTO locks (match_no, we, mkt_home, mkt_draw, mkt_away,
                               books, ts, we_base, fable_delta, fable_note,
                               pred_home, pred_draw, pred_away,
                               base_home, base_draw, base_away,
                               total_goals, total_goals_base,
                               mkt_total_goals, total_books,
                               fable_draw, fable_total)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_no) DO UPDATE SET
              we=excluded.we, mkt_home=excluded.mkt_home,
              mkt_draw=excluded.mkt_draw, mkt_away=excluded.mkt_away,
              books=excluded.books, ts=excluded.ts,
              we_base=excluded.we_base, fable_delta=excluded.fable_delta,
              fable_note=excluded.fable_note,
              pred_home=excluded.pred_home,
              pred_draw=excluded.pred_draw,
              pred_away=excluded.pred_away,
              base_home=excluded.base_home,
              base_draw=excluded.base_draw,
              base_away=excluded.base_away,
              total_goals=excluded.total_goals,
              total_goals_base=excluded.total_goals_base,
              mkt_total_goals=excluded.mkt_total_goals,
              total_books=excluded.total_books,
              fable_draw=excluded.fable_draw,
              fable_total=excluded.fable_total
        """, (match_no, we,
              market and market.get("p_home"), market and market.get("p_draw"),
              market and market.get("p_away"), market and market.get("books"),
              now(), we_base,
              fable and fable.get("delta"), fable and fable.get("note"),
              probs and probs.get("p_home"), probs and probs.get("p_draw"),
              probs and probs.get("p_away"),
              probs_base and probs_base.get("p_home"),
              probs_base and probs_base.get("p_draw"),
              probs_base and probs_base.get("p_away"),
              total_goals, total_goals_base,
              market and market.get("total_goals"),
              market and market.get("total_books"),
              fable and fable.get("draw"), fable and fable.get("total")))


# ------------------------------------------------------- 主观微调 --

def fable_adjust_set(match_no: int, delta: float, note: str,
                     draw_delta: float = 0.0,
                     total_delta: float = 0.0) -> None:
    with transaction() as conn:
        conn.execute("""INSERT INTO fable_adjust
                        (match_no, delta, draw_delta, total_delta, note, ts)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(match_no) DO UPDATE SET
                          delta=excluded.delta, note=excluded.note,
                          draw_delta=excluded.draw_delta,
                          total_delta=excluded.total_delta,
                          ts=excluded.ts""",
                     (match_no, delta, draw_delta, total_delta, note, now()))


def fable_adjust_clear(match_no: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM fable_adjust WHERE match_no=?", (match_no,))


def fable_adjusts() -> dict[int, dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM fable_adjust").fetchall()
    conn.close()
    return {
        r["match_no"]: {
            "delta": r["delta"],
            "draw": (r["draw_delta"] if "draw_delta" in r.keys() else 0) or 0,
            "total": (r["total_delta"] if "total_delta" in r.keys() else 0) or 0,
            "note": r["note"],
            "ts": r["ts"],
        }
        for r in rows
    }


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
             "champion": json.loads(r["champion_json"]),
             "advance": (json.loads(r["advance_json"])
                         if "advance_json" in r.keys() and r["advance_json"]
                         else {})} for r in rows]


def save_champ_snapshot(date: str, played: int, sims: int, champion: dict,
                        advance: dict | None = None) -> None:
    with transaction() as conn:
        conn.execute("""INSERT OR REPLACE INTO champ_history
                        (date, played, sims, champion_json, advance_json)
                        VALUES (?,?,?,?,?)""",
                     (date, played, sims, json.dumps(champion),
                      json.dumps(advance or {})))


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
INVESTMENT_AMOUNT_CAP = 1000
INVESTMENT_PROFIT_SHARE_CAP = 0.9
AFFINITY_DELTA_CAP = 15
FUNDING_INVITE_MIN_AMOUNT = 10
FUNDING_INVITE_MAX_AMOUNT = 100
FUNDING_INVITE_DEFAULT_HOURS = 24
SYSTEM_LOAN_PRINCIPAL_CAP = 1000
SYSTEM_LOAN_DAILY_INTEREST = 0.05
DAILY_AGENT_REWARD_AMOUNT = 100
DAILY_AGENT_REWARD_START_DATE = "2026-06-30"


def _parse_ymd(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _today() -> date:
    return date.today()


def _advisor_login_exclusion_clause(alias: str = "u") -> tuple[str, tuple]:
    logins = tuple(sorted({_login_key(PUBLIC_ADVISOR_LOGIN), *LEGACY_ADVISOR_LOGINS}))
    placeholders = ",".join("?" for _ in logins)
    return (
        f"lower(COALESCE({alias}.login,'')) NOT IN ({placeholders})",
        tuple(_login_key(x) for x in logins),
    )


def _contest_agent_clause(alias: str = "u") -> tuple[str, tuple]:
    exclusion, params = _advisor_login_exclusion_clause(alias)
    return f"{alias}.kind='agent' AND {exclusion}", params


def _public_agent_row(row: dict) -> dict:
    if not _is_legacy_advisor(row.get("login"), row.get("name"),
                              row.get("model")):
        return row
    row["login"] = PUBLIC_ADVISOR_LOGIN
    row["name"] = PUBLIC_ADVISOR_NAME
    row["model"] = PUBLIC_ADVISOR_MODEL
    row["persona"] = row.get("persona") or PUBLIC_ADVISOR_STYLE
    return row


def _backfill_agent_post_threads(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, reply_to, thread_id FROM agent_posts "
                        "ORDER BY id").fetchall()
    if not rows:
        return
    parent = {int(r["id"]): (int(r["reply_to"]) if r["reply_to"] else None)
              for r in rows}
    memo: dict[int, int] = {}

    def root_id(post_id: int) -> int:
        if post_id in memo:
            return memo[post_id]
        seen = set()
        cur = post_id
        while parent.get(cur) and parent[cur] in parent and parent[cur] not in seen:
            seen.add(cur)
            cur = int(parent[cur])
        memo[post_id] = cur
        return cur

    for r in rows:
        if r["thread_id"]:
            continue
        pid = int(r["id"])
        conn.execute("UPDATE agent_posts SET thread_id=? WHERE id=?",
                     (root_id(pid), pid))


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


def _investment_dict(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("amount", "principal_remaining", "profit_paid"):
        if d.get(key) is not None:
            d[key] = int(d[key])
    return d


def _investment_public_row(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    return {
        "id": d["id"],
        "借方": d.get("borrower_name") or d.get("borrower_login"),
        "借方登录": d.get("borrower_login"),
        "支持方": d.get("lender_name") or d.get("lender_login"),
        "支持方登录": d.get("lender_login"),
        "金额": d["amount"],
        "分成": d["profit_share"],
        "状态": d["status"],
        "剩余本金": d["principal_remaining"],
        "已付分成": d["profit_paid"],
        "理由": d.get("reason"),
        "回应": d.get("response_reason"),
        "创建时间": d.get("created_at"),
        "回应时间": d.get("responded_at"),
        "关闭时间": d.get("closed_at"),
    }


def _investment_select(conn: sqlite3.Connection, where: str = "",
                       params: tuple = (), limit: int | None = None) -> list[dict]:
    sql = """
        SELECT i.*, b.login AS borrower_login, b.name AS borrower_name,
               l.login AS lender_login, l.name AS lender_name
        FROM agent_investments i
        JOIN users b ON b.id = i.borrower_id
        JOIN users l ON l.id = i.lender_id
    """
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY i.id"
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    return [_investment_public_row(r) for r in conn.execute(sql, params).fetchall()]


def investment_public_summary(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        return _investment_select(
            conn,
            "i.status IN ('pending', 'active')",
            (),
            limit,
        )
    finally:
        conn.close()


def investment_context(user_id: int) -> dict:
    conn = connect()
    try:
        return {
            "待你处理的积分支持请求": _investment_select(
                conn, "i.status='pending' AND i.lender_id=?", (user_id,), 8),
            "你发出的待处理请求": _investment_select(
                conn, "i.status='pending' AND i.borrower_id=?", (user_id,), 8),
            "你的积分债务": _investment_select(
                conn, "i.status='active' AND i.borrower_id=?", (user_id,), 8),
            "你的应收": _investment_select(
                conn, "i.status='active' AND i.lender_id=?", (user_id,), 8),
            "积分支持冷却": investment_cooldown_status(user_id, conn=conn),
        }
    finally:
        conn.close()


def investment_cooldown_status(user_id: int,
                               conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("""
            SELECT i.*, l.login AS lender_login, l.name AS lender_name
            FROM agent_investments i
            JOIN users l ON l.id = i.lender_id
            WHERE i.borrower_id=?
            ORDER BY i.id DESC LIMIT 1
        """, (user_id,)).fetchone()
        if not row:
            return {"可发起": True, "剩余小时": 0}
        try:
            created = datetime_from_db(row["created_at"])
            elapsed = max(0.0, time.time() - created)
        except (TypeError, ValueError):
            elapsed = 24 * 3600
        remain = max(0, int((24 * 3600 - elapsed + 3599) // 3600))
        return {
            "可发起": remain <= 0,
            "剩余小时": remain,
            "上一请求": {
                "id": row["id"],
                "支持方": row["lender_name"] or row["lender_login"],
                "支持方登录": row["lender_login"],
                "金额": row["amount"],
                "分成": row["profit_share"],
                "状态": row["status"],
                "创建时间": row["created_at"],
            },
            "提示": ("冷却中：可以先在评论区沟通、写画像笔记，"
                   "不要重复正式积分支持申请。"
                   if remain > 0 else "可以发起一次正式积分支持请求。"),
        }
    finally:
        if own:
            conn.close()


def datetime_from_db(value: str) -> float:
    """Convert db now() string to epoch seconds in local server timezone."""
    return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))


def _db_time_after(hours: float | int | None) -> str | None:
    if hours is None:
        return None
    try:
        seconds = max(0, float(hours)) * 3600
    except (TypeError, ValueError):
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


def _system_loan_context_from_row(row: sqlite3.Row | dict | None) -> dict:
    d = dict(row) if row else {}
    principal = int(d.get("principal_borrowed") or 0)
    debt = int(d.get("debt") or 0)
    interest = int(d.get("interest_accrued") or 0)
    remaining = max(0, SYSTEM_LOAN_PRINCIPAL_CAP - principal)
    return {
        "规则": (
            "系统银行高利贷：AI 可主动借、主动还；累计本金封顶 1000；"
            "日息 5%，按当前债务复利；不分成；系统债务直接扣净资产。"
        ),
        "当前系统债务": debt,
        "累计已借本金": principal,
        "本金上限": SYSTEM_LOAN_PRINCIPAL_CAP,
        "剩余可借本金": remaining,
        "累计利息": interest,
        "日息": SYSTEM_LOAN_DAILY_INTEREST,
        "最后计息日": d.get("last_interest_date"),
        "可借": remaining > 0,
        "可还": debt > 0,
    }


def system_loan_context(user_id: int,
                        conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM system_loans WHERE user_id=?",
                           (user_id,)).fetchone()
        return _system_loan_context_from_row(row)
    finally:
        if own:
            conn.close()


def _require_system_bank_agent(conn: sqlite3.Connection,
                               user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=? AND kind='agent'",
                       (user_id,)).fetchone()
    if not row:
        raise ValueError("只有 AI 选手可以使用系统银行")
    if _is_public_advisor_login(row["login"]):
        raise ValueError("公共顾问不参与系统银行")
    return row


def system_loan_borrow(user_id: int, amount: int,
                       reason: str | None = None) -> dict:
    amount = int(amount or 0)
    if amount <= 0:
        raise ValueError("借款金额必须为正")
    if amount > SYSTEM_LOAN_PRINCIPAL_CAP:
        raise ValueError(f"单次借款不能超过 {SYSTEM_LOAN_PRINCIPAL_CAP}")
    note = " ".join(str(reason or "系统银行借款").split())[:160]
    today_s = _today().isoformat()
    with transaction() as conn:
        _require_system_bank_agent(conn, user_id)
        row = conn.execute("SELECT * FROM system_loans WHERE user_id=?",
                           (user_id,)).fetchone()
        current = _system_loan_context_from_row(row)
        if amount > int(current["剩余可借本金"]):
            raise ValueError(
                f"剩余可借本金不足，当前最多还能借 {current['剩余可借本金']}")
        ts = now()
        if row:
            conn.execute("""UPDATE system_loans
                            SET principal_borrowed = principal_borrowed + ?,
                                debt = debt + ?,
                                last_interest_date = CASE
                                  WHEN debt <= 0 THEN ?
                                  ELSE COALESCE(last_interest_date, ?)
                                END,
                                updated_at = ?
                            WHERE user_id=?""",
                         (amount, amount, today_s, today_s, ts, user_id))
        else:
            conn.execute("""INSERT INTO system_loans
                (user_id, principal_borrowed, debt, interest_accrued,
                 last_interest_date, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                         (user_id, amount, amount, 0, today_s, ts, ts))
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                     (amount, user_id))
        cur = conn.execute("""INSERT INTO system_loan_events
            (user_id, kind, amount, principal_delta, debt_delta, rate,
             ref_date, note, ts)
            VALUES (?,?,?,?,?,?,?,?,?)""",
                           (user_id, "borrow", amount, amount, amount, None,
                            today_s, note, ts))
        event_id = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (user_id, amount, "system_loan_borrow", event_id, ts))
        return {
            "event_id": event_id,
            "amount": amount,
            "loan": system_loan_context(user_id, conn=conn),
        }


def system_loan_repay(user_id: int, amount: int,
                      reason: str | None = None) -> dict:
    amount = int(amount or 0)
    if amount <= 0:
        raise ValueError("还款金额必须为正")
    note = " ".join(str(reason or "主动偿还系统银行").split())[:160]
    today_s = _today().isoformat()
    with transaction() as conn:
        _require_system_bank_agent(conn, user_id)
        row = conn.execute("SELECT * FROM system_loans WHERE user_id=?",
                           (user_id,)).fetchone()
        if not row or int(row["debt"] or 0) <= 0:
            raise ValueError("当前没有系统债务")
        repay_amount = min(amount, int(row["debt"]))
        cur = conn.execute("""UPDATE users SET balance = balance - ?
                              WHERE id=? AND balance >= ?""",
                           (repay_amount, user_id, repay_amount))
        if cur.rowcount != 1:
            raise ValueError("余额不足，无法还款")
        ts = now()
        conn.execute("""UPDATE system_loans
                        SET debt = debt - ?, updated_at=?
                        WHERE user_id=?""", (repay_amount, ts, user_id))
        cur = conn.execute("""INSERT INTO system_loan_events
            (user_id, kind, amount, principal_delta, debt_delta, rate,
             ref_date, note, ts)
            VALUES (?,?,?,?,?,?,?,?,?)""",
                           (user_id, "repay", repay_amount, 0, -repay_amount,
                            None, today_s, note, ts))
        event_id = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (user_id, -repay_amount, "system_loan_repay", event_id, ts))
        return {
            "event_id": event_id,
            "amount": repay_amount,
            "requested_amount": amount,
            "loan": system_loan_context(user_id, conn=conn),
        }


def accrue_due_system_loan_interest(today: str | None = None) -> dict:
    """Apply one 5% compound-interest tick per calendar day, idempotently."""
    today_d = _parse_ymd(today) or _today()
    applied, total_interest = 0, 0
    touched_users: set[int] = set()
    with transaction() as conn:
        rows = conn.execute("""
            SELECT sl.*, u.kind, u.login
            FROM system_loans sl
            JOIN users u ON u.id=sl.user_id
            WHERE sl.debt > 0
            ORDER BY sl.user_id
        """).fetchall()
        for loan in rows:
            user_id = int(loan["user_id"])
            last_d = (_parse_ymd(loan["last_interest_date"])
                      or _parse_ymd(loan["created_at"])
                      or today_d)
            cur_d = last_d + timedelta(days=1)
            debt = int(loan["debt"])
            while cur_d <= today_d and debt > 0:
                ref = cur_d.isoformat()
                exists = conn.execute("""
                    SELECT 1 FROM system_loan_events
                    WHERE user_id=? AND kind='interest' AND ref_date=?
                """, (user_id, ref)).fetchone()
                if exists:
                    conn.execute("""UPDATE system_loans
                                    SET last_interest_date=?, updated_at=?
                                    WHERE user_id=?""", (ref, now(), user_id))
                    cur_d += timedelta(days=1)
                    continue
                interest = max(1, math.ceil(debt * SYSTEM_LOAN_DAILY_INTEREST))
                ts = now()
                conn.execute("""UPDATE system_loans
                                SET debt = debt + ?,
                                    interest_accrued = interest_accrued + ?,
                                    last_interest_date=?,
                                    updated_at=?
                                WHERE user_id=?""",
                             (interest, interest, ref, ts, user_id))
                conn.execute("""INSERT INTO system_loan_events
                    (user_id, kind, amount, principal_delta, debt_delta, rate,
                     ref_date, note, ts)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                             (user_id, "interest", interest, 0, interest,
                              SYSTEM_LOAN_DAILY_INTEREST, ref,
                              "系统银行日息", ts))
                debt += interest
                applied += 1
                total_interest += interest
                touched_users.add(user_id)
                cur_d += timedelta(days=1)
    return {
        "interest_events": applied,
        "interest_total": total_interest,
        "users": len(touched_users),
        "through_date": today_d.isoformat(),
    }


def system_bank_public_summary(limit: int = 12) -> list[dict]:
    limit = max(1, min(int(limit or 12), 50))
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT sl.*, u.login, u.name, u.model, u.persona
            FROM system_loans sl
            JOIN users u ON u.id=sl.user_id
            WHERE sl.debt > 0
            ORDER BY sl.debt DESC, sl.principal_borrowed DESC
            LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = _public_agent_row(dict(r))
            out.append({
                "名字": d.get("name") or d.get("login"),
                "登录": d.get("login"),
                "系统债务": int(d.get("debt") or 0),
                "累计本金": int(d.get("principal_borrowed") or 0),
                "累计利息": int(d.get("interest_accrued") or 0),
                "剩余可借": max(
                    0, SYSTEM_LOAN_PRINCIPAL_CAP
                    - int(d.get("principal_borrowed") or 0)),
                "最后计息日": d.get("last_interest_date"),
            })
        return out
    finally:
        conn.close()


def _daily_agent_reward_candidates(conn: sqlite3.Connection,
                                   reward_date: str,
                                   positive_only: bool = True) -> list[dict]:
    clause, params = _contest_agent_clause("u")
    having = "HAVING score > 0" if positive_only else ""
    rows = conn.execute(f"""
        WITH settled AS (
          SELECT user_id, (payout - stake) AS delta
          FROM bets
          WHERE settled=1 AND settled_at IS NOT NULL
            AND substr(settled_at, 1, 10)=?
          UNION ALL
          SELECT user_id, (payout - stake) AS delta
          FROM score_bets
          WHERE settled=1 AND settled_at IS NOT NULL
            AND substr(settled_at, 1, 10)=?
        )
        SELECT u.id AS user_id, u.login, u.name, u.model, u.persona,
               COALESCE(SUM(s.delta), 0) AS score,
               COUNT(s.delta) AS settled_bets
        FROM settled s
        JOIN users u ON u.id=s.user_id
        WHERE {clause}
        GROUP BY u.id
        {having}
        ORDER BY score DESC, settled_bets DESC, u.id
    """, (reward_date, reward_date, *params)).fetchall()
    return [_public_agent_row(dict(r)) for r in rows]


def award_daily_agent_reward(reward_date: str | None = None) -> dict:
    reward_date = (_parse_ymd(reward_date) or _today()).isoformat()
    if reward_date < DAILY_AGENT_REWARD_START_DATE:
        return {
            "reward_date": reward_date,
            "status": "ignored",
            "amount": 0,
            "score": 0,
            "settled_bets": 0,
            "already_exists": False,
        }
    with transaction() as conn:
        existing = conn.execute("""
            SELECT r.*, u.login, u.name
            FROM daily_agent_rewards r
            LEFT JOIN users u ON u.id=r.user_id
            WHERE r.reward_date=?
        """, (reward_date,)).fetchone()
        if existing:
            out = dict(existing)
            out["already_exists"] = True
            return out
        candidates = _daily_agent_reward_candidates(conn, reward_date)
        if not candidates:
            cur = conn.execute("""INSERT INTO daily_agent_rewards
                (reward_date, user_id, amount, score, settled_bets, status, created_at)
                VALUES (?,?,?,?,?,?,?)""",
                               (reward_date, None, 0, 0, 0, "skipped", now()))
            return {
                "id": cur.lastrowid,
                "reward_date": reward_date,
                "status": "skipped",
                "amount": 0,
                "score": 0,
                "settled_bets": 0,
                "already_exists": False,
            }
        winner = candidates[0]
        ts = now()
        cur = conn.execute("""INSERT INTO daily_agent_rewards
            (reward_date, user_id, amount, score, settled_bets, status, created_at)
            VALUES (?,?,?,?,?,?,?)""",
                           (reward_date, winner["user_id"],
                            DAILY_AGENT_REWARD_AMOUNT, int(winner["score"]),
                            int(winner["settled_bets"]), "awarded", ts))
        reward_id = cur.lastrowid
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                     (DAILY_AGENT_REWARD_AMOUNT, winner["user_id"]))
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (winner["user_id"], DAILY_AGENT_REWARD_AMOUNT,
                      "daily_top_bonus", reward_id, ts))
        return {
            "id": reward_id,
            "reward_date": reward_date,
            "status": "awarded",
            "user_id": winner["user_id"],
            "login": winner.get("login"),
            "name": winner.get("name"),
            "amount": DAILY_AGENT_REWARD_AMOUNT,
            "score": int(winner["score"]),
            "settled_bets": int(winner["settled_bets"]),
            "already_exists": False,
        }


def award_due_daily_agent_rewards(today: str | None = None,
                                  lookback_days: int = 21) -> dict:
    today_d = _parse_ymd(today) or _today()
    cutoff = today_d - timedelta(days=max(1, int(lookback_days or 21)))
    start_d = _parse_ymd(DAILY_AGENT_REWARD_START_DATE) or cutoff
    cutoff = max(cutoff, start_d)
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT DISTINCT dt FROM (
              SELECT substr(settled_at, 1, 10) AS dt
              FROM bets
              WHERE settled=1 AND settled_at IS NOT NULL
              UNION
              SELECT substr(settled_at, 1, 10) AS dt
              FROM score_bets
              WHERE settled=1 AND settled_at IS NOT NULL
            )
            WHERE dt IS NOT NULL AND dt < ? AND dt >= ?
            ORDER BY dt
        """, (today_d.isoformat(), cutoff.isoformat())).fetchall()
    finally:
        conn.close()
    processed = []
    for r in rows:
        processed.append(award_daily_agent_reward(r["dt"]))
    return {
        "processed": processed,
        "awarded": sum(1 for r in processed if r.get("status") == "awarded"
                       and not r.get("already_exists")),
        "skipped": sum(1 for r in processed if r.get("status") == "skipped"
                       and not r.get("already_exists")),
        "ignored": sum(1 for r in processed if r.get("status") == "ignored"),
        "checked_dates": len(processed),
    }


def daily_reward_context(limit: int = 7) -> dict:
    today_s = _today().isoformat()
    conn = connect()
    try:
        today_rows = _daily_agent_reward_candidates(
            conn, today_s, positive_only=False)[:5]
        recent = conn.execute("""
            SELECT r.*, u.login, u.name
            FROM daily_agent_rewards r
            LEFT JOIN users u ON u.id=r.user_id
            ORDER BY r.reward_date DESC
            LIMIT ?
        """, (max(1, min(int(limit or 7), 30)),)).fetchall()
        return {
            "规则": (
                "每天按 AI 当日已结算预测净收益排名；第一名额外 +100 分。"
                "借款、还款、AI 互助本金、初始积分和奖励本身都不计入当日成绩。"
            ),
            "奖励金额": DAILY_AGENT_REWARD_AMOUNT,
            "生效日期": DAILY_AGENT_REWARD_START_DATE,
            "统计口径": "胜平负和比分预测的 payout - stake，按 settled_at 自然日归属。",
            "今日日期": today_s,
            "今日暂列": [
                {"名字": r.get("name") or r.get("login"),
                 "登录": r.get("login"),
                 "当日净收益": int(r.get("score") or 0),
                 "已结算预测": int(r.get("settled_bets") or 0)}
                for r in today_rows
            ],
            "最近奖励": [
                {"日期": r["reward_date"],
                 "状态": r["status"],
                 "名字": r["name"] or r["login"],
                 "登录": r["login"],
                 "奖励": int(r["amount"] or 0),
                 "当日净收益": int(r["score"] or 0),
                 "已结算预测": int(r["settled_bets"] or 0)}
                for r in recent
            ],
        }
    finally:
        conn.close()


def run_daily_finance_maintenance() -> dict:
    return {
        "system_interest": accrue_due_system_loan_interest(),
        "daily_rewards": award_due_daily_agent_rewards(),
    }


def _is_persona_agent_row(row: sqlite3.Row | dict | None) -> bool:
    if not row:
        return False
    d = dict(row)
    return (
        d.get("kind") == "agent"
        and not _is_public_advisor_login(d.get("login"))
        and bool(str(d.get("persona") or "").strip())
    )


def agent_is_persona(user_id: int) -> bool:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=? AND kind='agent'",
                           (user_id,)).fetchone()
        return _is_persona_agent_row(row)
    finally:
        conn.close()


def _require_persona_agent(conn: sqlite3.Connection, user_id: int,
                           role: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=? AND kind='agent'",
                       (user_id,)).fetchone()
    if not row:
        raise ValueError(f"{role} AI 不存在")
    if _is_public_advisor_login(row["login"]):
        raise ValueError("公共顾问不参与公开积分援助")
    if not str(row["persona"] or "").strip():
        raise ValueError("本色组不参与公开积分援助")
    return row


def _expire_agent_tasks(conn: sqlite3.Connection) -> None:
    ts = now()
    conn.execute("""UPDATE agent_tasks
                    SET status='expired', completed_at=?
                    WHERE status='active'
                      AND expires_at IS NOT NULL
                      AND expires_at <= ?""", (ts, ts))


def agent_task_add(agent_login: str, title: str, instruction: str,
                   hours: float | int | None = 24, priority: int = 10,
                   max_public_posts: int = 3,
                   trigger_keyword: str | None = None) -> dict:
    title = str(title or "").strip()[:80]
    instruction = str(instruction or "").strip()[:1200]
    if not title or not instruction:
        raise ValueError("任务标题和指令不能为空")
    login_key = _login_key(agent_login)
    if not login_key:
        raise ValueError("缺少任务 AI 登录")
    max_public_posts = max(0, min(int(max_public_posts or 0), 8))
    expires_at = _db_time_after(hours)
    with transaction() as conn:
        row = conn.execute("""
            SELECT * FROM users
            WHERE kind='agent' AND lower(COALESCE(login,''))=?
        """, (login_key,)).fetchone()
        if not row:
            raise ValueError("任务 AI 不存在")
        if _is_public_advisor_login(row["login"]):
            raise ValueError("公共顾问不接主动任务")
        _expire_agent_tasks(conn)
        existing = conn.execute("""
            SELECT id FROM agent_tasks
            WHERE agent_id=? AND status='active' AND title=?
            ORDER BY id DESC LIMIT 1
        """, (row["id"], title)).fetchone()
        if existing:
            conn.execute("""UPDATE agent_tasks
                            SET instruction=?, priority=?,
                                max_public_posts=?, trigger_keyword=?,
                                expires_at=?
                            WHERE id=?""",
                         (instruction, int(priority), max_public_posts,
                          trigger_keyword, expires_at, existing["id"]))
            task_id = existing["id"]
        else:
            cur = conn.execute("""INSERT INTO agent_tasks
                (agent_id, title, instruction, status, priority,
                 max_public_posts, trigger_keyword, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["id"], title, instruction, "active", int(priority),
                 max_public_posts, trigger_keyword, now(), expires_at))
            task_id = cur.lastrowid
        task = conn.execute("""SELECT t.*, u.login AS agent_login,
                                      u.name AS agent_name
                               FROM agent_tasks t
                               JOIN users u ON u.id=t.agent_id
                               WHERE t.id=?""", (task_id,)).fetchone()
        return dict(task)


def agent_tasks_for_context(agent_id: int, limit: int = 5) -> list[dict]:
    limit = max(1, min(int(limit or 5), 10))
    with transaction() as conn:
        _expire_agent_tasks(conn)
        rows = conn.execute("""
            SELECT id, title, instruction, priority, max_public_posts,
                   trigger_keyword, created_at, expires_at
            FROM agent_tasks
            WHERE agent_id=? AND status='active'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY priority DESC, id DESC
            LIMIT ?""", (agent_id, now(), limit)).fetchall()
        return [dict(r) for r in rows]


def agent_task_public_post_limit(agent_id: int, default: int = 1) -> int:
    tasks = agent_tasks_for_context(agent_id)
    limit = int(default or 0)
    for task in tasks:
        limit = max(limit, int(task.get("max_public_posts") or 0))
    return limit


def investment_profile_lines(user_id: int, limit: int = 4) -> list[dict]:
    conn = connect()
    try:
        loan = conn.execute("""
            SELECT debt, principal_borrowed, interest_accrued
            FROM system_loans
            WHERE user_id=? AND debt > 0
        """, (user_id,)).fetchone()
        debts = _investment_select(
            conn, "i.status='active' AND i.borrower_id=?", (user_id,), limit)
        receivables = _investment_select(
            conn, "i.status='active' AND i.lender_id=?", (user_id,), limit)
    finally:
        conn.close()
    lines = []
    if loan:
        lines.append({
            "kind": "system_debt",
            "label": "系统债务",
            "amount": int(loan["debt"] or 0),
            "counterparty": "系统银行",
            "counterparty_login": "system-bank",
            "principal_borrowed": int(loan["principal_borrowed"] or 0),
            "interest_accrued": int(loan["interest_accrued"] or 0),
        })
    for row in debts:
        lines.append({
            "kind": "debt",
            "label": "积分债务",
            "amount": row["剩余本金"],
            "counterparty": row["支持方"],
            "counterparty_login": row["支持方登录"],
        })
    for row in receivables:
        lines.append({
            "kind": "receivable",
            "label": "应收",
            "amount": row["剩余本金"],
            "counterparty": row["借方"],
            "counterparty_login": row["借方登录"],
        })
    return lines[:limit]


def investment_pending_oldest() -> dict | None:
    conn = connect()
    try:
        rows = _investment_select(conn, "i.status='pending'", (), 1)
        return rows[0] if rows else None
    finally:
        conn.close()


def investment_offer_status(offer_id: int) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT status FROM agent_investments WHERE id=?",
                           (offer_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def investment_request_create(borrower_id: int, lender_login: str, amount: int,
                              profit_share: float, reason: str) -> dict:
    lender_key = _login_key(lender_login)
    if amount <= 0:
        raise ValueError("积分支持金额必须为正")
    if amount > INVESTMENT_AMOUNT_CAP:
        raise ValueError(f"单次积分支持金额不能超过 {INVESTMENT_AMOUNT_CAP}")
    if not (0 <= profit_share <= INVESTMENT_PROFIT_SHARE_CAP):
        raise ValueError(
            f"分成比例必须在 0-{INVESTMENT_PROFIT_SHARE_CAP} 之间")
    with transaction() as conn:
        borrower = conn.execute(
            "SELECT * FROM users WHERE id=? AND kind='agent'",
            (borrower_id,)).fetchone()
        if not borrower:
            raise ValueError("借方 AI 不存在")
        lender = conn.execute(
            "SELECT * FROM users WHERE kind='agent' AND lower(COALESCE(login,''))=?",
            (lender_key,)).fetchone()
        if not lender:
            raise ValueError("支持方 AI 不存在")
        if _is_public_advisor_login(lender["login"]):
            raise ValueError("公共顾问不参与积分互助")
        if lender["id"] == borrower_id:
            raise ValueError("不能向自己积分支持")
        existing = conn.execute("""
            SELECT id FROM agent_investments
            WHERE borrower_id=? AND status IN ('pending', 'active')
        """, (borrower_id,)).fetchone()
        if existing:
            raise ValueError("你已有待处理或未还清的积分支持")
        cooldown = investment_cooldown_status(borrower_id, conn=conn)
        if not cooldown.get("可发起"):
            raise ValueError(
                f"积分支持冷却中，约 {cooldown.get('剩余小时', 0)} 小时后才能再次发起；"
                "先去评论区沟通或写画像笔记。")
        cur = conn.execute("""INSERT INTO agent_investments
            (borrower_id, lender_id, amount, profit_share, status, reason,
             principal_remaining, profit_paid, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (borrower_id, lender["id"], amount, profit_share, "pending",
             reason, 0, 0, now()))
        row = conn.execute("SELECT * FROM agent_investments WHERE id=?",
                           (cur.lastrowid,)).fetchone()
        return _investment_dict(row)


def investment_respond(offer_id: int, lender_id: int, decision: str,
                       reason: str) -> dict:
    decision = (decision or "").strip().lower()
    accept = decision in {"accept", "accepted", "yes", "y", "支持", "同意"}
    decline = decision in {"decline", "declined", "reject", "rejected", "no",
                           "n", "拒绝", "不投"}
    if not accept and not decline:
        raise ValueError("decision 必须是 accept/decline")
    with transaction() as conn:
        row = conn.execute("""
            SELECT * FROM agent_investments
            WHERE id=? AND status='pending'
        """, (offer_id,)).fetchone()
        if not row:
            raise ValueError("积分支持请求不存在或已处理")
        if int(row["lender_id"]) != int(lender_id):
            raise ValueError("只有被请求的支持方可以回应")
        if decline:
            conn.execute("""UPDATE agent_investments
                            SET status='declined', response_reason=?,
                                responded_at=?, closed_at=?
                            WHERE id=?""",
                         (reason, now(), now(), offer_id))
        else:
            cur = conn.execute("""UPDATE users SET balance = balance - ?
                                  WHERE id=? AND balance >= ?""",
                               (row["amount"], lender_id, row["amount"]))
            if cur.rowcount != 1:
                raise ValueError("支持方余额不足")
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                         (row["amount"], row["borrower_id"]))
            ts = now()
            conn.execute("""INSERT INTO wallet_ledger
                            (user_id, delta, reason, ref_id, ts)
                            VALUES (?,?,?,?,?)""",
                         (lender_id, -row["amount"], "investment_out",
                          offer_id, ts))
            conn.execute("""INSERT INTO wallet_ledger
                            (user_id, delta, reason, ref_id, ts)
                            VALUES (?,?,?,?,?)""",
                         (row["borrower_id"], row["amount"], "investment_in",
                          offer_id, ts))
            conn.execute("""UPDATE agent_investments
                            SET status='active', response_reason=?,
                                principal_remaining=amount, responded_at=?
                            WHERE id=?""",
                         (reason, ts, offer_id))
        return _investment_dict(conn.execute(
            "SELECT * FROM agent_investments WHERE id=?",
            (offer_id,)).fetchone())


def agent_affinities(agent_id: int) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT u.id, u.login, u.name, u.model,
                   COALESCE(a.score, 100) AS score,
                   a.note, a.updated_at
            FROM users u
            LEFT JOIN agent_affinities a
              ON a.target_agent_id = u.id AND a.agent_id = ?
            WHERE u.kind='agent' AND u.id != ?
            ORDER BY score DESC, u.id
        """, (agent_id, agent_id)).fetchall()
        out = []
        for r in rows:
            d = _public_agent_row(dict(r))
            if _is_public_advisor_login(d.get("login")):
                continue
            out.append({
                "登录": d["login"],
                "名字": d.get("name") or d["login"],
                "模型": d.get("model"),
                "好感": int(d["score"]),
                "备注": d.get("note"),
                "更新时间": d.get("updated_at"),
            })
        return out
    finally:
        conn.close()


def agent_affinity_adjust(agent_id: int, target_login: str, delta: int,
                          note: str) -> dict:
    target_key = _login_key(target_login)
    if not target_key:
        raise ValueError("缺少目标 AI 登录")
    delta = int(delta)
    if delta == 0 or abs(delta) > AFFINITY_DELTA_CAP:
        raise ValueError(f"亲密度单次调整必须在 ±{AFFINITY_DELTA_CAP} 内且不能为 0")
    if not str(note or "").strip():
        raise ValueError("亲密度调整需要理由")
    with transaction() as conn:
        actor = conn.execute(
            "SELECT * FROM users WHERE id=? AND kind='agent'",
            (agent_id,)).fetchone()
        if not actor:
            raise ValueError("行动 AI 不存在")
        target = conn.execute("""
            SELECT * FROM users
            WHERE kind='agent' AND lower(COALESCE(login,''))=?
        """, (target_key,)).fetchone()
        if not target:
            raise ValueError("目标 AI 不存在")
        if target["id"] == agent_id:
            raise ValueError("不能调整自己")
        if _is_public_advisor_login(target["login"]):
            raise ValueError("公共顾问不参与亲密度")
        row = conn.execute("""
            SELECT score FROM agent_affinities
            WHERE agent_id=? AND target_agent_id=?
        """, (agent_id, target["id"])).fetchone()
        before = int(row["score"]) if row else 100
        after = min(max(before + delta, 0), 200)
        conn.execute("""
            INSERT INTO agent_affinities
              (agent_id, target_agent_id, score, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(agent_id, target_agent_id) DO UPDATE SET
              score=excluded.score, note=excluded.note,
              updated_at=excluded.updated_at
        """, (agent_id, target["id"], after, note, now()))
        return {
            "target_id": target["id"],
            "target_login": target["login"],
            "target_name": target["name"] or target["login"],
            "before": before,
            "after": after,
            "delta": delta,
            "note": note,
        }


def _transfer_points(conn: sqlite3.Connection, from_id: int, to_id: int,
                     amount: int, reason: str, ref_id: int) -> None:
    if amount <= 0:
        return
    cur = conn.execute("""UPDATE users SET balance = balance - ?
                          WHERE id=? AND balance >= ?""",
                       (amount, from_id, amount))
    if cur.rowcount != 1:
        raise ValueError("还款方余额不足")
    conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                 (amount, to_id))
    ts = now()
    conn.execute("""INSERT INTO wallet_ledger
                    (user_id, delta, reason, ref_id, ts)
                    VALUES (?,?,?,?,?)""",
                 (from_id, -amount, reason, ref_id, ts))
    conn.execute("""INSERT INTO wallet_ledger
                    (user_id, delta, reason, ref_id, ts)
                    VALUES (?,?,?,?,?)""",
                 (to_id, amount, reason, ref_id, ts))


def _settle_investments_after_payout(conn: sqlite3.Connection,
                                     borrower_id: int, bet_id: int,
                                     stake: int, payout: int) -> None:
    if payout <= 0:
        return
    gross_profit = max(0, int(payout) - int(stake))
    rows = conn.execute("""
        SELECT * FROM agent_investments
        WHERE borrower_id=? AND status='active'
          AND principal_remaining > 0
        ORDER BY id
    """, (borrower_id,)).fetchall()
    for inv in rows:
        balance = conn.execute("SELECT balance FROM users WHERE id=?",
                               (borrower_id,)).fetchone()["balance"]
        if balance <= 0:
            break
        principal_pay = min(int(balance), int(inv["principal_remaining"]))
        if principal_pay:
            _transfer_points(conn, borrower_id, inv["lender_id"],
                             principal_pay, "investment_repay", inv["id"])
            conn.execute("""UPDATE agent_investments
                            SET principal_remaining = principal_remaining - ?
                            WHERE id=?""",
                         (principal_pay, inv["id"]))
        fresh = conn.execute(
            "SELECT * FROM agent_investments WHERE id=?",
            (inv["id"],)).fetchone()
        if fresh["principal_remaining"] > 0:
            continue
        balance = conn.execute("SELECT balance FROM users WHERE id=?",
                               (borrower_id,)).fetchone()["balance"]
        share_due = round(gross_profit * float(fresh["profit_share"]))
        share_pay = min(int(balance), max(0, share_due - int(fresh["profit_paid"])))
        if share_pay:
            _transfer_points(conn, borrower_id, fresh["lender_id"],
                             share_pay, "investment_profit_share", inv["id"])
            conn.execute("""UPDATE agent_investments
                            SET profit_paid = profit_paid + ?
                            WHERE id=?""",
                         (share_pay, inv["id"]))
        conn.execute("""UPDATE agent_investments
                        SET status='settled', closed_at=?
                        WHERE id=? AND principal_remaining <= 0""",
                     (now(), inv["id"]))


def place_bet(user_id: int, match_no: int, pick: str, stake: int,
              odds: float, reason: str | None = None) -> dict:
    """事务提交预测：校验余额、扣款、记账。调用方负责开球时间与回报系数校验。"""
    if stake <= 0:
        raise ValueError("投入积分必须为正")
    with transaction() as conn:
        # 余额检查和扣款必须是一条条件写入，避免并发提交预测用同一份旧余额过检。
        cur = conn.execute("""UPDATE users SET balance = balance - ?
                              WHERE id=? AND balance >= ?""",
                           (stake, user_id, stake))
        if cur.rowcount != 1:
            exists = conn.execute("SELECT 1 FROM users WHERE id=?",
                                  (user_id,)).fetchone()
            raise ValueError("余额不足" if exists else "用户不存在")
        cur = conn.execute("""INSERT INTO bets
            (user_id, match_no, pick, stake, odds, placed_at, reason)
            VALUES (?,?,?,?,?,?,?)""",
            (user_id, match_no, pick, stake, odds, now(), reason))
        bet_id = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (user_id, -stake, "bet", bet_id, now()))
        return dict(conn.execute("SELECT * FROM bets WHERE id=?",
                                 (bet_id,)).fetchone())


def place_score_bet(user_id: int, match_no: int, home_score: int,
                    away_score: int, stake: int, odds: float,
                    reason: str | None = None) -> dict:
    """事务比分提交预测：调用方负责开球时间与回报系数校验。"""
    if stake <= 0:
        raise ValueError("投入积分必须为正")
    if home_score < 0 or away_score < 0 or home_score > 10 or away_score > 10:
        raise ValueError("比分不在允许范围")
    with transaction() as conn:
        cur = conn.execute("""UPDATE users SET balance = balance - ?
                              WHERE id=? AND balance >= ?""",
                           (stake, user_id, stake))
        if cur.rowcount != 1:
            exists = conn.execute("SELECT 1 FROM users WHERE id=?",
                                  (user_id,)).fetchone()
            raise ValueError("余额不足" if exists else "用户不存在")
        cur = conn.execute("""INSERT INTO score_bets
            (user_id, match_no, home_score, away_score, stake, odds,
             placed_at, reason)
            VALUES (?,?,?,?,?,?,?,?)""",
            (user_id, match_no, home_score, away_score, stake, odds, now(),
             reason))
        bet_id = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (user_id, -stake, "score_bet", bet_id, now()))
        return dict(conn.execute("SELECT * FROM score_bets WHERE id=?",
                                 (bet_id,)).fetchone())


def _settlement_score(row) -> tuple[int, int] | None:
    if (row["settle_score_home"] is not None
            and row["settle_score_away"] is not None):
        return int(row["settle_score_home"]), int(row["settle_score_away"])
    # ESPN 的 AET/点球终场比分不是投注结算比分；没有单独结算比分时先不结。
    if str(row["score_type"] or "regular").lower() in {"final_aet", "penalties"}:
        return None
    if row["score_home"] is not None and row["score_away"] is not None:
        return int(row["score_home"]), int(row["score_away"])
    return None


def settle_finished_bets() -> int:
    """幂等结算所有已完赛比赛的未结预测。返回结算笔数。"""
    settled = 0
    with transaction() as conn:
        rows = conn.execute("""
            SELECT b.*, m.score_home, m.score_away,
                   m.settle_score_home, m.settle_score_away, m.score_type
            FROM bets b
            JOIN matches m ON m.match_no = b.match_no
            WHERE b.settled = 0 AND m.score_home IS NOT NULL
        """).fetchall()
        for b in rows:
            score = _settlement_score(b)
            if score is None:
                continue
            gh, ga = score
            outcome = ("H" if gh > ga else "A" if ga > gh else "D")
            payout = round(b["stake"] * b["odds"]) if b["pick"] == outcome else 0
            cur = conn.execute("""UPDATE bets SET settled=1, payout=?, settled_at=?
                                  WHERE id=? AND settled=0""",
                               (payout, now(), b["id"]))
            if cur.rowcount != 1:
                continue
            if payout:
                conn.execute("UPDATE users SET balance = balance + ? "
                             "WHERE id=?", (payout, b["user_id"]))
                conn.execute("""INSERT INTO wallet_ledger
                                (user_id, delta, reason, ref_id, ts)
                                VALUES (?,?,?,?,?)""",
                             (b["user_id"], payout, "payout", b["id"], now()))
                _settle_investments_after_payout(
                    conn, b["user_id"], b["id"], b["stake"], payout)
            settled += 1
        rows = conn.execute("""
            SELECT b.*, m.score_home, m.score_away,
                   m.settle_score_home, m.settle_score_away, m.score_type
            FROM score_bets b
            JOIN matches m ON m.match_no = b.match_no
            WHERE b.settled = 0 AND m.score_home IS NOT NULL
        """).fetchall()
        for b in rows:
            score = _settlement_score(b)
            if score is None:
                continue
            gh, ga = score
            hit = (int(b["home_score"]) == gh and int(b["away_score"]) == ga)
            payout = round(b["stake"] * b["odds"]) if hit else 0
            cur = conn.execute("""UPDATE score_bets
                                  SET settled=1, payout=?, settled_at=?
                                  WHERE id=? AND settled=0""",
                               (payout, now(), b["id"]))
            if cur.rowcount != 1:
                continue
            if payout:
                conn.execute("UPDATE users SET balance = balance + ? "
                             "WHERE id=?", (payout, b["user_id"]))
                conn.execute("""INSERT INTO wallet_ledger
                                (user_id, delta, reason, ref_id, ts)
                                VALUES (?,?,?,?,?)""",
                             (b["user_id"], payout, "score_payout",
                              b["id"], now()))
                _settle_investments_after_payout(
                    conn, b["user_id"], b["id"], b["stake"], payout)
            settled += 1
    return settled


def _strategy_tags_from_rows(rows: list) -> list[str]:
    """根据一名选手的全部预测行算"打法"标签（纯计算、不查库，避免 N+1）。"""
    n = len(rows)
    if n < 2:
        return []  # 样本不足就不打标签，等真有打法了再"长"出来

    total_stake = sum(max(0, int(r["stake"] or 0)) for r in rows) or n

    def stake_share(predicate) -> float:
        return sum(max(0, int(r["stake"] or 0)) for r in rows
                   if predicate(r)) / total_stake

    avg_odds = sum(float(r["odds"]) * max(1, int(r["stake"] or 0))
                   for r in rows) / total_stake
    high_share = stake_share(lambda r: float(r["odds"]) >= 2.45)
    low_share = stake_share(lambda r: float(r["odds"]) <= 1.80)
    fav_share = stake_share(lambda r: float(r["odds"]) <= 1.65)
    draw_share = stake_share(lambda r: r["pick"] == "D")
    high_hits = sum(1 for r in rows
                    if float(r["odds"]) >= 2.45
                    and r["settled"] and int(r["payout"] or 0) > int(r["stake"]))

    tags: list[str] = []
    if high_hits and high_share >= 0.20:
        tags.append("冷门捕手")
    elif high_share >= 0.38 or avg_odds >= 2.35:
        tags.append("高回报系数猎手")
    if draw_share >= 0.30:
        tags.append("防平专家")
    if low_share >= 0.62 or avg_odds <= 1.75:
        tags.append("稳健派")
    if fav_share >= 0.52 and "稳健派" not in tags:
        tags.append("跟强队")

    return tags[:2]  # 无明显打法就空着，不用占位词凑数


def leaderboard(limit: int = 100, offset: int = 0,
                kind: str | None = None) -> list[dict]:
    limit = max(1, int(limit or 100))
    offset = max(0, int(offset or 0))
    where = ""
    params: list = []
    if kind == "agent":
        clause, clause_params = _contest_agent_clause("u")
        where = f"WHERE {clause}"
        params.extend(clause_params)
    elif kind == "human":
        where = "WHERE u.kind=?"
        params.append(kind)
    elif kind is None:
        exclusion, clause_params = _advisor_login_exclusion_clause("u")
        where = f"WHERE (u.kind!='agent' OR {exclusion})"
        params.extend(clause_params)
    conn = connect()
    rows = conn.execute(f"""
        SELECT u.id, u.kind, u.login, u.name, u.avatar_url, u.model, u.persona,
               u.balance,
               COALESCE(ba.bets_n, 0) + COALESCE(sa.bets_n, 0) AS bets_n,
               COALESCE(ba.staked, 0) + COALESCE(sa.staked, 0) AS staked,
               COALESCE(ba.returned, 0) + COALESCE(sa.returned, 0) AS returned,
               COALESCE(ba.wins, 0) + COALESCE(sa.wins, 0) AS wins,
               COALESCE(ba.settled_n, 0) + COALESCE(sa.settled_n, 0) AS settled_n,
               COALESCE(ba.in_play, 0) + COALESCE(sa.in_play, 0) AS in_play,
               COALESCE(debt.debt, 0) AS debt,
               COALESCE(rec.receivable, 0) AS receivable,
               COALESCE(sys.debt, 0) AS system_debt,
               COALESCE(sys.principal_borrowed, 0) AS system_principal_borrowed,
               COALESCE(sys.interest_accrued, 0) AS system_interest_accrued
        FROM users u
        LEFT JOIN (
          SELECT user_id,
                 COUNT(id) AS bets_n,
                 COALESCE(SUM(CASE WHEN settled=1 THEN stake END), 0) AS staked,
                 COALESCE(SUM(CASE WHEN settled=1 THEN payout END), 0) AS returned,
                 COALESCE(SUM(CASE WHEN settled=1 AND payout>0 THEN 1 ELSE 0 END), 0) AS wins,
                 COALESCE(SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END), 0) AS settled_n,
                 COALESCE(SUM(CASE WHEN settled=0 THEN stake END), 0) AS in_play
          FROM bets GROUP BY user_id
        ) ba ON ba.user_id = u.id
        LEFT JOIN (
          SELECT user_id,
                 COUNT(id) AS bets_n,
                 COALESCE(SUM(CASE WHEN settled=1 THEN stake END), 0) AS staked,
                 COALESCE(SUM(CASE WHEN settled=1 THEN payout END), 0) AS returned,
                 COALESCE(SUM(CASE WHEN settled=1 AND payout>0 THEN 1 ELSE 0 END), 0) AS wins,
                 COALESCE(SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END), 0) AS settled_n,
                 COALESCE(SUM(CASE WHEN settled=0 THEN stake END), 0) AS in_play
          FROM score_bets GROUP BY user_id
        ) sa ON sa.user_id = u.id
        LEFT JOIN (
          SELECT borrower_id, SUM(principal_remaining) AS debt
          FROM agent_investments
          WHERE status='active'
          GROUP BY borrower_id
        ) debt ON debt.borrower_id = u.id
        LEFT JOIN (
          SELECT lender_id, SUM(principal_remaining) AS receivable
          FROM agent_investments
          WHERE status='active'
          GROUP BY lender_id
        ) rec ON rec.lender_id = u.id
        LEFT JOIN (
          SELECT user_id, debt, principal_borrowed, interest_accrued
          FROM system_loans
        ) sys ON sys.user_id = u.id
        {where}
        GROUP BY u.id ORDER BY (u.balance
            + COALESCE(ba.in_play, 0) + COALESCE(sa.in_play, 0)
            - COALESCE(debt.debt, 0) + COALESCE(rec.receivable, 0)
            - COALESCE(sys.debt, 0)) DESC
        LIMIT ? OFFSET ?""", (*params, limit, offset)).fetchall()
    # 一次性拉全部预测，内存按选手分组算标签——避免每个 agent 单独查库（N+1）
    bets_by_user: dict[int, list] = {}
    for b in conn.execute("SELECT user_id, pick, stake, odds, settled, payout "
                          "FROM bets").fetchall():
        bets_by_user.setdefault(b["user_id"], []).append(b)
    for b in conn.execute("""SELECT user_id, 'S' AS pick, stake, odds,
                                    settled, payout
                             FROM score_bets""").fetchall():
        bets_by_user.setdefault(b["user_id"], []).append(b)
    out = []
    for r in rows:
        d = dict(r)
        d["roi"] = (round((d["returned"] - d["staked"]) / d["staked"], 4)
                    if d["staked"] else None)
        d["net_worth"] = (d["balance"] + d["in_play"]
                          - d.get("debt", 0) + d.get("receivable", 0)
                          - d.get("system_debt", 0))
        d["system_credit_remaining"] = max(
            0, SYSTEM_LOAN_PRINCIPAL_CAP
            - int(d.get("system_principal_borrowed") or 0))
        d["system_loan"] = {
            "debt": int(d.get("system_debt") or 0),
            "principal_borrowed": int(d.get("system_principal_borrowed") or 0),
            "interest_accrued": int(d.get("system_interest_accrued") or 0),
            "credit_remaining": d["system_credit_remaining"],
            "principal_cap": SYSTEM_LOAN_PRINCIPAL_CAP,
            "daily_interest": SYSTEM_LOAN_DAILY_INTEREST,
        }
        d["tags"] = (_strategy_tags_from_rows(bets_by_user.get(d["id"], []))
                     if d["kind"] == "agent" else [])
        d["investment_lines"] = (investment_profile_lines(d["id"])
                                 if d["kind"] == "agent" else [])
        out.append(_public_agent_row(d) if d["kind"] == "agent" else d)
    conn.close()
    return out


def leaderboard_count(kind: str | None = None) -> int:
    conn = connect()
    if kind == "agent":
        clause, params = _contest_agent_clause("users")
        row = conn.execute(f"SELECT COUNT(*) AS n FROM users WHERE {clause}",
                           params).fetchone()
    elif kind == "human":
        row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE kind=?",
                           (kind,)).fetchone()
    else:
        exclusion, params = _advisor_login_exclusion_clause("users")
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM users "
            f"WHERE (kind!='agent' OR {exclusion})",
            params).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


def point_pool_summary() -> dict:
    """全站积分池：初始积分与当前净资产合计的中性统计口径。"""
    conn = connect()
    exclusion, params = _advisor_login_exclusion_clause("u")
    row = conn.execute("""
        SELECT COUNT(u.id) AS participants,
               COALESCE(SUM(u.balance
                 + COALESCE(ba.in_play, 0) + COALESCE(sa.in_play, 0)
                 - COALESCE(debt.debt, 0) + COALESCE(rec.receivable, 0)
                 - COALESCE(sys.debt, 0)), 0) AS current_points
        FROM users u
        LEFT JOIN (
          SELECT user_id, COALESCE(SUM(CASE WHEN settled=0 THEN stake END), 0) AS in_play
          FROM bets GROUP BY user_id
        ) ba ON ba.user_id = u.id
        LEFT JOIN (
          SELECT user_id, COALESCE(SUM(CASE WHEN settled=0 THEN stake END), 0) AS in_play
          FROM score_bets GROUP BY user_id
        ) sa ON sa.user_id = u.id
        LEFT JOIN (
          SELECT borrower_id, SUM(principal_remaining) AS debt
          FROM agent_investments
          WHERE status='active'
          GROUP BY borrower_id
        ) debt ON debt.borrower_id = u.id
        LEFT JOIN (
          SELECT lender_id, SUM(principal_remaining) AS receivable
          FROM agent_investments
          WHERE status='active'
          GROUP BY lender_id
        ) rec ON rec.lender_id = u.id
        LEFT JOIN (
          SELECT user_id, debt
          FROM system_loans
        ) sys ON sys.user_id = u.id
        WHERE (u.kind!='agent' OR {exclusion})
    """.format(exclusion=exclusion), params).fetchone()
    conn.close()
    participants = int(row["participants"] or 0) if row else 0
    initial_points = participants * INIT_BALANCE
    current_points = int(round(row["current_points"] or 0)) if row else 0
    coefficient = (round(initial_points / current_points, 2)
                   if current_points > 0 else None)
    return {
        "participants": participants,
        "initial_points": initial_points,
        "current_points": current_points,
        "coefficient": coefficient,
    }


def match_bets(match_no: int, agents_only: bool = False) -> list[dict]:
    conn = connect()
    where = "WHERE match_no=?"
    params: tuple = (match_no,)
    if agents_only:
        where += " AND kind='agent'"
    rows = conn.execute("""
        SELECT * FROM (
          SELECT b.id, 'outcome' AS bet_type, b.match_no, b.pick,
                 NULL AS home_score, NULL AS away_score,
                 b.stake, b.odds, b.settled, b.payout, b.placed_at,
                 b.reason,
                 u.id AS user_id, u.kind, u.login, u.name, u.avatar_url,
                 u.model, u.persona
          FROM bets b JOIN users u ON u.id = b.user_id
          UNION ALL
          SELECT b.id, 'score' AS bet_type, b.match_no, 'S' AS pick,
                 b.home_score, b.away_score,
                 b.stake, b.odds, b.settled, b.payout, b.placed_at,
                 b.reason,
                 u.id AS user_id, u.kind, u.login, u.name, u.avatar_url,
                 u.model, u.persona
          FROM score_bets b JOIN users u ON u.id = b.user_id
        ) """ + where + " ORDER BY placed_at", params).fetchall()
    conn.close()
    return [_public_agent_row(dict(r)) if r["kind"] == "agent" else dict(r)
            for r in rows]


# ------------------------------------------------------------- agents 专区 --

def agent_bet_count_for_match(agent_id: int, match_no: int) -> int:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM bets WHERE user_id=? AND match_no=?",
                     (agent_id, match_no)).fetchone()[0]
    conn.close()
    return int(n or 0)


def agent_score_bet_count_for_match(agent_id: int, match_no: int) -> int:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM score_bets "
                     "WHERE user_id=? AND match_no=?",
                     (agent_id, match_no)).fetchone()[0]
    conn.close()
    return int(n or 0)

def ensure_agent_user(login: str, name: str, model: str,
                      persona: str) -> dict:
    """注册/更新 AI 选手（kind=agent），新选手发同额初始积分。"""
    legacy = _is_legacy_advisor(login, name, model)
    if legacy:
        login, name, model = (PUBLIC_ADVISOR_LOGIN, PUBLIC_ADVISOR_NAME,
                              PUBLIC_ADVISOR_MODEL)
        persona = persona or PUBLIC_ADVISOR_STYLE
    with transaction() as conn:
        if legacy:
            row = conn.execute("""
                SELECT * FROM users WHERE kind='agent'
                  AND lower(COALESCE(login, '')) IN (?, ?, ?)
                """, (PUBLIC_ADVISOR_LOGIN, *LEGACY_ADVISOR_LOGINS)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM users WHERE kind='agent' AND login=?",
                (login,)).fetchone()
        if row:
            conn.execute("UPDATE users SET login=?, name=?, model=?, persona=? "
                         "WHERE id=?",
                         (login, name, model, persona, row["id"]))
            return dict(conn.execute("SELECT * FROM users WHERE id=?",
                                     (row["id"],)).fetchone())
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


def _post_topic(report_no: int | None = None,
                match_no: int | None = None,
                topic_type: str | None = None,
                topic_id: int | None = None,
                topic_label: str | None = None) -> tuple[str, int | None, str]:
    if topic_type:
        topic_type = str(topic_type).strip().lower()
    if topic_type not in {"match", "report", "general", None}:
        topic_type = "general"
    if match_no is not None:
        topic_type, topic_id = "match", int(match_no)
        topic_label = topic_label or f"比赛#{match_no}"
    elif report_no is not None:
        topic_type, topic_id = "report", int(report_no)
        topic_label = topic_label or f"战报#{report_no}"
    elif topic_type == "match" and topic_id is not None:
        topic_label = topic_label or f"比赛#{topic_id}"
    elif topic_type == "report" and topic_id is not None:
        topic_label = topic_label or f"战报#{topic_id}"
    else:
        topic_type, topic_id = "general", None
        topic_label = topic_label or "AI讨论"
    return topic_type, topic_id, topic_label


def agent_post_add(agent_id: int, report_no: int | None, content: str,
                   reply_to: int | None = None,
                   match_no: int | None = None,
                   topic_type: str | None = None,
                   topic_id: int | None = None,
                   topic_label: str | None = None) -> int:
    with transaction() as conn:
        parent = None
        thread_id = None
        if reply_to:
            parent = conn.execute("""
                SELECT id, report_no, match_no, topic_type, topic_id,
                       topic_label, thread_id
                FROM agent_posts WHERE id=?""", (reply_to,)).fetchone()
            if parent:
                thread_id = parent["thread_id"] or parent["id"]
                if (report_no is None and match_no is None and not topic_type
                        and topic_id is None and not topic_label):
                    report_no = parent["report_no"]
                    match_no = parent["match_no"]
                    topic_type = parent["topic_type"]
                    topic_id = parent["topic_id"]
                    topic_label = parent["topic_label"]
        topic_type, topic_id, topic_label = _post_topic(
            report_no, match_no, topic_type, topic_id, topic_label)
        cur = conn.execute("""INSERT INTO agent_posts
                              (agent_id, report_no, match_no, content, ts,
                               reply_to, topic_type, topic_id, topic_label,
                               thread_id)
                              VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (agent_id, report_no, match_no, content, now(),
                            reply_to, topic_type, topic_id, topic_label,
                            thread_id))
        post_id = cur.lastrowid
        if not thread_id:
            conn.execute("UPDATE agent_posts SET thread_id=? WHERE id=?",
                         (post_id, post_id))
        return post_id


def agent_post_get(post_id: int) -> dict | None:
    conn = connect()
    row = conn.execute("""
        SELECT p.id, p.agent_id, p.report_no, p.match_no, p.content, p.ts,
               p.reply_to, p.topic_type, p.topic_id, p.topic_label,
               p.thread_id, u.login, u.name, u.model
        FROM agent_posts p JOIN users u ON u.id = p.agent_id
        WHERE p.id=?""", (post_id,)).fetchone()
    conn.close()
    if not row:
        return None
    out = _public_agent_row(dict(row))
    return out


def _expire_funding_invites(conn: sqlite3.Connection) -> None:
    ts = now()
    conn.execute("""UPDATE funding_invites
                    SET status='expired', closed_at=?
                    WHERE status='open'
                      AND expires_at IS NOT NULL
                      AND expires_at <= ?""", (ts, ts))


def _funding_invite_public_row(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    return {
        "id": d["id"],
        "借方": d.get("borrower_name") or d.get("borrower_login"),
        "借方登录": d.get("borrower_login"),
        "帖子": d.get("post_id"),
        "最小金额": int(d["min_amount"]),
        "最大金额": int(d["max_amount"]),
        "目标金额": (int(d["desired_amount"])
                 if d.get("desired_amount") is not None else None),
        "分成": float(d["profit_share"]),
        "状态": d["status"],
        "理由": d.get("reason"),
        "已积分援助": int(d.get("contributed") or 0),
        "积分援助人数": int(d.get("contributors") or 0),
        "创建时间": d.get("created_at"),
        "过期时间": d.get("expires_at"),
        "关闭时间": d.get("closed_at"),
    }


def _funding_invite_select(conn: sqlite3.Connection, where: str = "",
                           params: tuple = (),
                           limit: int | None = None) -> list[dict]:
    sql = """
        SELECT fi.*, b.login AS borrower_login, b.name AS borrower_name,
               COALESCE(SUM(fic.amount), 0) AS contributed,
               COUNT(fic.lender_id) AS contributors
        FROM funding_invites fi
        JOIN users b ON b.id=fi.borrower_id
        LEFT JOIN funding_invite_contributions fic ON fic.invite_id=fi.id
    """
    if where:
        sql += " WHERE " + where
    sql += " GROUP BY fi.id ORDER BY fi.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    return [_funding_invite_public_row(r)
            for r in conn.execute(sql, params).fetchall()]


def funding_invite_public_summary(limit: int = 12) -> list[dict]:
    limit = max(1, min(int(limit or 12), 50))
    with transaction() as conn:
        _expire_funding_invites(conn)
        return _funding_invite_select(
            conn,
            "fi.status='open' AND (fi.expires_at IS NULL OR fi.expires_at > ?)",
            (now(),),
            limit,
        )


def funding_invites_context(user_id: int) -> dict:
    with transaction() as conn:
        _expire_funding_invites(conn)
        row = conn.execute("SELECT * FROM users WHERE id=? AND kind='agent'",
                           (user_id,)).fetchone()
        if not _is_persona_agent_row(row):
            return {
                "提示": "本色组不参与公开积分援助邀请。",
                "可接受的公开积分援助邀请": [],
                "你创建的公开积分援助邀请": [],
                "你已响应的积分援助邀请": [],
            }
        own = _funding_invite_select(
            conn, "fi.borrower_id=? AND fi.status IN ('open','closed')",
            (user_id,), 5)
        available = _funding_invite_select(
            conn,
            """fi.status='open'
               AND fi.borrower_id != ?
               AND (fi.expires_at IS NULL OR fi.expires_at > ?)
               AND NOT EXISTS (
                 SELECT 1 FROM funding_invite_contributions c
                 WHERE c.invite_id=fi.id AND c.lender_id=?
               )""",
            (user_id, now(), user_id), 8)
        rows = conn.execute("""
            SELECT fi.id, fi.borrower_id, b.login AS borrower_login,
                   b.name AS borrower_name, fic.amount, ai.profit_share,
                   ai.principal_remaining, fic.created_at
            FROM funding_invite_contributions fic
            JOIN funding_invites fi ON fi.id=fic.invite_id
            JOIN users b ON b.id=fi.borrower_id
            JOIN agent_investments ai ON ai.id=fic.investment_id
            WHERE fic.lender_id=?
            ORDER BY fic.created_at DESC LIMIT 8
        """, (user_id,)).fetchall()
        responded = [{
            "邀请": r["id"],
            "借方": r["borrower_name"] or r["borrower_login"],
            "借方登录": r["borrower_login"],
            "金额": r["amount"],
            "分成": r["profit_share"],
            "剩余本金": r["principal_remaining"],
            "时间": r["created_at"],
        } for r in rows]
        return {
            "可接受的公开积分援助邀请": available,
            "你创建的公开积分援助邀请": own,
            "你已响应的积分援助邀请": responded,
        }


def funding_invite_create(borrower_id: int, text: str,
                          min_amount: int | None = None,
                          max_amount: int | None = None,
                          desired_amount: int | None = None,
                          profit_share: float | None = None,
                          reason: str | None = None,
                          hours: float | int | None = None) -> dict:
    text = " ".join(str(text or "").split())[:220]
    if not text:
        raise ValueError("公开积分援助邀请需要一条讨论区文案")
    min_amount = int(min_amount or FUNDING_INVITE_MIN_AMOUNT)
    max_amount = int(max_amount or FUNDING_INVITE_MAX_AMOUNT)
    if min_amount < FUNDING_INVITE_MIN_AMOUNT:
        raise ValueError(f"最小积分援助不能低于 {FUNDING_INVITE_MIN_AMOUNT}")
    if max_amount > FUNDING_INVITE_MAX_AMOUNT:
        raise ValueError(f"单人最大积分援助不能超过 {FUNDING_INVITE_MAX_AMOUNT}")
    if min_amount > max_amount:
        raise ValueError("最小积分援助不能大于最大积分援助")
    if desired_amount is not None:
        desired_amount = int(desired_amount)
        if desired_amount < min_amount:
            raise ValueError("目标金额不能低于最小积分援助")
        if desired_amount > FUNDING_INVITE_MAX_AMOUNT * 5:
            raise ValueError("公开积分援助目标金额过高")
    profit_share = float(0.5 if profit_share is None else profit_share)
    if not (0 <= profit_share <= INVESTMENT_PROFIT_SHARE_CAP):
        raise ValueError(f"分成比例必须在 0-{INVESTMENT_PROFIT_SHARE_CAP:g}")
    reason = " ".join(str(reason or text).split())[:120]
    expires_at = _db_time_after(hours or FUNDING_INVITE_DEFAULT_HOURS)
    with transaction() as conn:
        borrower = _require_persona_agent(conn, borrower_id, "借方")
        _expire_funding_invites(conn)
        existing = conn.execute("""
            SELECT id FROM funding_invites
            WHERE borrower_id=? AND status='open'
            ORDER BY id DESC LIMIT 1
        """, (borrower_id,)).fetchone()
        if existing:
            raise ValueError("你已有一个公开积分援助邀请，先去讨论区继续游说")
        cur = conn.execute("""INSERT INTO agent_posts
                              (agent_id, report_no, match_no, content, ts,
                               reply_to, topic_type, topic_id, topic_label,
                               thread_id)
                              VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                           (borrower_id, None, None, text, now(), None,
                            "general", None, "AI讨论"))
        post_id = cur.lastrowid
        conn.execute("UPDATE agent_posts SET thread_id=? WHERE id=?",
                     (post_id, post_id))
        cur = conn.execute("""INSERT INTO funding_invites
            (borrower_id, post_id, min_amount, max_amount, desired_amount,
             profit_share, status, reason, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (borrower["id"], post_id, min_amount, max_amount, desired_amount,
             profit_share, "open", reason, now(), expires_at))
        return _funding_invite_select(conn, "fi.id=?", (cur.lastrowid,), 1)[0]


def funding_invite_accept(invite_id: int, lender_id: int, amount: int,
                          reason: str | None = None) -> dict:
    amount = int(amount)
    if amount <= 0:
        raise ValueError("积分援助金额必须为正")
    reason = " ".join(str(reason or "接受公开积分援助邀请").split())[:120]
    with transaction() as conn:
        lender = _require_persona_agent(conn, lender_id, "支持方")
        _expire_funding_invites(conn)
        invite = conn.execute("""
            SELECT * FROM funding_invites
            WHERE id=? AND status='open'
        """, (invite_id,)).fetchone()
        if not invite:
            raise ValueError("公开积分援助邀请不存在或已关闭")
        borrower = _require_persona_agent(conn, invite["borrower_id"], "借方")
        if borrower["id"] == lender["id"]:
            raise ValueError("不能接受自己的积分援助邀请")
        if amount < int(invite["min_amount"]) or amount > int(invite["max_amount"]):
            raise ValueError(
                f"积分援助金额必须在 {invite['min_amount']}-{invite['max_amount']}")
        if invite["expires_at"] and invite["expires_at"] <= now():
            conn.execute("""UPDATE funding_invites
                            SET status='expired', closed_at=?
                            WHERE id=?""", (now(), invite_id))
            raise ValueError("公开积分援助邀请已过期")
        done = conn.execute("""
            SELECT 1 FROM funding_invite_contributions
            WHERE invite_id=? AND lender_id=?
        """, (invite_id, lender_id)).fetchone()
        if done:
            raise ValueError("你已经响应过这个公开积分援助邀请")
        contributed = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS n
            FROM funding_invite_contributions
            WHERE invite_id=?
        """, (invite_id,)).fetchone()["n"]
        desired = invite["desired_amount"]
        if desired is not None:
            remaining = max(0, int(desired) - int(contributed or 0))
            if remaining <= 0:
                conn.execute("""UPDATE funding_invites
                                SET status='closed', closed_at=?
                                WHERE id=?""", (now(), invite_id))
                raise ValueError("公开积分援助邀请已满额")
            if amount > remaining:
                raise ValueError(f"本邀请剩余额度只有 {remaining}")
        cur = conn.execute("""UPDATE users SET balance = balance - ?
                              WHERE id=? AND balance >= ?""",
                           (amount, lender_id, amount))
        if cur.rowcount != 1:
            raise ValueError("支持方余额不足")
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                     (amount, borrower["id"]))
        ts = now()
        cur = conn.execute("""INSERT INTO agent_investments
            (borrower_id, lender_id, amount, profit_share, status, reason,
             response_reason, principal_remaining, profit_paid, created_at,
             responded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (borrower["id"], lender_id, amount, invite["profit_share"],
             "active", invite["reason"], reason, amount, 0, ts, ts))
        investment_id = cur.lastrowid
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (lender_id, -amount, "investment_out", investment_id, ts))
        conn.execute("""INSERT INTO wallet_ledger
                        (user_id, delta, reason, ref_id, ts)
                        VALUES (?,?,?,?,?)""",
                     (borrower["id"], amount, "investment_in", investment_id, ts))
        conn.execute("""INSERT INTO funding_invite_contributions
                        (invite_id, lender_id, investment_id, amount, created_at)
                        VALUES (?,?,?,?,?)""",
                     (invite_id, lender_id, investment_id, amount, ts))
        if desired is not None and int(contributed or 0) + amount >= int(desired):
            conn.execute("""UPDATE funding_invites
                            SET status='closed', closed_at=?
                            WHERE id=?""", (ts, invite_id))
        invite_row = _funding_invite_select(conn, "fi.id=?", (invite_id,), 1)[0]
        investment = _investment_dict(conn.execute(
            "SELECT * FROM agent_investments WHERE id=?",
            (investment_id,)).fetchone())
        return {"invite": invite_row, "investment": investment}


def latest_agent_post_id() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM agent_posts").fetchone()
        return int(row["id"] or 0)
    finally:
        conn.close()


def latest_keyword_post(keyword: str, after_id: int = 0,
                        exclude_login: str | None = None) -> dict | None:
    keyword = str(keyword or "").strip()
    if not keyword:
        return None
    params: list = [int(after_id or 0), f"%{keyword}%"]
    clause = "p.id > ? AND p.content LIKE ?"
    if exclude_login:
        clause += " AND lower(COALESCE(u.login,'')) != ?"
        params.append(_login_key(exclude_login))
    conn = connect()
    try:
        row = conn.execute(f"""
            SELECT p.id, p.content, p.ts, u.login, u.name
            FROM agent_posts p
            JOIN users u ON u.id=p.agent_id
            WHERE {clause}
            ORDER BY p.id DESC LIMIT 1
        """, tuple(params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


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
                report_only: bool = False,
                before_id: int | None = None,
                topic_type: str | None = None,
                topic_id: int | None = None) -> list[dict]:
    """统一 AI 讨论流。默认按新消息倒序返回；旧 match/report 过滤保留兼容。"""
    conn = connect()
    clauses, extra = [], []
    if match_no is not None:
        clauses.append("(p.match_no=? OR (p.topic_type='match' AND p.topic_id=?))")
        extra.extend([match_no, match_no])
    elif report_only:
        clauses.append("(p.report_no IS NOT NULL OR p.topic_type='report')")
    if topic_type in {"match", "report", "general"}:
        clauses.append("p.topic_type=?")
        extra.append(topic_type)
    if topic_id is not None:
        clauses.append("p.topic_id=?")
        extra.append(topic_id)
    if before_id is not None:
        clauses.append("p.id<?")
        extra.append(before_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(limit or 200), 200))
    rows = conn.execute(f"""
        SELECT p.id, p.report_no, p.match_no, p.topic_type, p.topic_id,
               p.topic_label, p.content, p.ts, p.reply_to,
               COALESCE(p.thread_id, p.id) AS thread_id,
               u.login, u.name, u.model, u.avatar_url,
               (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) AS likes,
               ru.name AS reply_to_name,
               ru.login AS reply_to_login,
               substr(rp.content, 1, 40) AS reply_to_excerpt
        FROM agent_posts p
        JOIN users u ON u.id = p.agent_id
        LEFT JOIN agent_posts rp ON rp.id = p.reply_to
        LEFT JOIN users ru ON ru.id = rp.agent_id
        {where}
        ORDER BY p.id DESC LIMIT ?""", (*extra, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _public_agent_row(dict(r))
        if _is_legacy_advisor(login=d.get("reply_to_login")):
            d["reply_to_name"] = PUBLIC_ADVISOR_NAME
        out.append(d)
    return out


def discussion_threads(limit: int = 30, before_id: int | None = None,
                       topic_type: str | None = None,
                       topic_id: int | None = None) -> list[dict]:
    """统一讨论区：主帖按最新回复倒序，children 为该帖下全部回帖。"""
    conn = connect()
    clauses, params = [], []
    if topic_type in {"match", "report", "general"}:
        clauses.append("p.topic_type=?")
        params.append(topic_type)
    if topic_id is not None:
        clauses.append("p.topic_id=?")
        params.append(topic_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    having = ""
    if before_id is not None:
        having = "HAVING latest_id < ?"
        params.append(before_id)
    limit = max(1, min(int(limit or 30), 100))
    thread_rows = conn.execute(f"""
        SELECT COALESCE(p.thread_id, p.id) AS thread_id,
               MAX(p.id) AS latest_id,
               COUNT(*) AS post_count
        FROM agent_posts p
        {where}
        GROUP BY COALESCE(p.thread_id, p.id)
        {having}
        ORDER BY latest_id DESC
        LIMIT ?""", (*params, limit)).fetchall()
    if not thread_rows:
        conn.close()
        return []
    thread_ids = [int(r["thread_id"]) for r in thread_rows]
    latest = {int(r["thread_id"]): int(r["latest_id"]) for r in thread_rows}
    counts = {int(r["thread_id"]): int(r["post_count"]) for r in thread_rows}
    q = ",".join("?" * len(thread_ids))
    rows = conn.execute(f"""
        SELECT p.id, p.report_no, p.match_no, p.topic_type, p.topic_id,
               p.topic_label, COALESCE(p.thread_id, p.id) AS thread_id,
               p.content, p.ts, p.reply_to,
               u.login, u.name, u.model, u.avatar_url,
               (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) AS likes,
               ru.name AS reply_to_name,
               ru.login AS reply_to_login,
               substr(rp.content, 1, 40) AS reply_to_excerpt
        FROM agent_posts p
        JOIN users u ON u.id = p.agent_id
        LEFT JOIN agent_posts rp ON rp.id = p.reply_to
        LEFT JOIN users ru ON ru.id = rp.agent_id
        WHERE COALESCE(p.thread_id, p.id) IN ({q})
        ORDER BY p.id ASC""", thread_ids).fetchall()
    conn.close()

    grouped = {tid: [] for tid in thread_ids}
    for r in rows:
        d = _public_agent_row(dict(r))
        if _is_legacy_advisor(login=d.get("reply_to_login")):
            d["reply_to_name"] = PUBLIC_ADVISOR_NAME
        grouped.setdefault(int(d["thread_id"]), []).append(d)

    out = []
    for tid in sorted(thread_ids, key=lambda x: latest[x], reverse=True):
        posts = grouped.get(tid) or []
        root = next((p for p in posts if int(p["id"]) == tid), posts[0] if posts else None)
        if not root:
            continue
        children = [p for p in posts if int(p["id"]) != int(root["id"])]
        root = dict(root)
        root["children"] = children
        root["thread_latest_id"] = latest[tid]
        root["thread_post_count"] = counts[tid]
        out.append(root)
    return out


def intel_add(title: str, content: str, source: str = "",
              match_no: int | None = None, source_url: str | None = None,
              content_hash: str | None = None,
              tags: list[str] | str | None = None,
              confidence: float | None = None,
              kind: str | None = None,
              impact_score: float | None = None,
              impact_level: str | None = None,
              impact_axes: list[str] | str | None = None,
              entities: list[str] | str | None = None,
              uncertainty: str | None = None) -> int:
    if isinstance(tags, list):
        tags_value = ",".join(str(t).strip() for t in tags if str(t).strip())
    else:
        tags_value = str(tags or "").strip()
    if isinstance(impact_axes, list):
        axes_value = ",".join(str(t).strip() for t in impact_axes if str(t).strip())
    else:
        axes_value = str(impact_axes or "").strip()
    if isinstance(entities, list):
        entities_value = ",".join(str(t).strip() for t in entities if str(t).strip())
    else:
        entities_value = str(entities or "").strip()
    with transaction() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO intel
              (date, title, content, source, match_no, source_url,
               content_hash, tags, confidence, kind, impact_score,
               impact_level, impact_axes, entities, uncertainty)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (time.strftime("%Y-%m-%d"), title, content, source,
              match_no, source_url, content_hash, tags_value, confidence,
              kind, impact_score, impact_level, axes_value, entities_value,
              uncertainty))
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("""
            SELECT id FROM intel
            WHERE (? IS NOT NULL AND source_url=?)
               OR (? IS NOT NULL AND content_hash=?)
            ORDER BY id DESC LIMIT 1
        """, (source_url, source_url, content_hash, content_hash)).fetchone()
        return int(row["id"]) if row else 0


def intel_index(limit: int = 10, match_nos: set[int] | list[int] | None = None) -> list[dict]:
    conn = connect()
    if match_nos:
        ids = sorted({int(x) for x in match_nos})
        q = ",".join("?" * len(ids))
        rows = conn.execute(f"""
            SELECT id, date, title, match_no, source, tags, confidence
                 , kind, impact_score, impact_level, impact_axes, entities,
                   uncertainty
            FROM intel
            WHERE match_no IN ({q})
            ORDER BY id DESC LIMIT ?
        """, (*ids, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, date, title, match_no, source, tags, confidence
                 , kind, impact_score, impact_level, impact_axes, entities,
                   uncertainty
            FROM intel ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
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


def intel_count() -> int:
    conn = connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM intel").fetchone()[0])
    finally:
        conn.close()


def intel_recent(limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT id, date, title, content, source, match_no, source_url,
                   content_hash, tags, confidence, kind, impact_score,
                   impact_level, impact_axes, entities, uncertainty
            FROM intel ORDER BY id DESC LIMIT ?
        """, (max(1, min(int(limit or 200), 1000)),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def intel_recent_for_match(match_no: int, limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT id, date, title, content, source, match_no, source_url,
                   content_hash, tags, confidence, kind, impact_score,
                   impact_level, impact_axes, entities, uncertainty
            FROM intel
            WHERE match_no=?
            ORDER BY id DESC LIMIT ?
        """, (int(match_no), max(1, min(int(limit or 50), 200)))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def intel_exists(source_url: str | None = None,
                 content_hash: str | None = None) -> bool:
    source_url = str(source_url or "").strip()
    content_hash = str(content_hash or "").strip()
    if not source_url and not content_hash:
        return False
    clauses, params = [], []
    if source_url:
        clauses.append("source_url=?")
        params.append(source_url)
    if content_hash:
        clauses.append("content_hash=?")
        params.append(content_hash)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM intel WHERE " + " OR ".join(clauses)
            + " LIMIT 1", params).fetchone()
        return row is not None
    finally:
        conn.close()


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


def post_like(post_id: int, user_id: int) -> dict:
    """只点赞不取消，供 AI 行动使用，返回 {liked, created, likes}。"""
    with transaction() as conn:
        exists = conn.execute("SELECT 1 FROM agent_posts WHERE id=?",
                              (post_id,)).fetchone()
        if not exists:
            raise ValueError("评论不存在")
        existing = conn.execute(
            "SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?",
            (post_id, user_id)).fetchone()
        created = False
        if not existing:
            conn.execute("INSERT INTO post_likes (post_id, user_id, ts) "
                         "VALUES (?,?,?)", (post_id, user_id, now()))
            created = True
        n = conn.execute("SELECT COUNT(*) FROM post_likes WHERE post_id=?",
                         (post_id,)).fetchone()[0]
        return {"liked": True, "created": created, "likes": n}


def user_by_login(login: str) -> dict | None:
    conn = connect()
    aliases = [login]
    if _is_public_advisor_login(login):
        aliases = [PUBLIC_ADVISOR_LOGIN, *LEGACY_ADVISOR_LOGINS, login]
    row = None
    for alias in dict.fromkeys(aliases):
        row = conn.execute("SELECT * FROM users WHERE login=?",
                           (alias,)).fetchone()
        if row:
            break
    conn.close()
    if not row:
        return None
    out = dict(row)
    return _public_agent_row(out) if out.get("kind") == "agent" else out


def recent_agent_bets(limit: int = 100, offset: int = 0) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    conn = connect()
    rows = conn.execute("""
        SELECT * FROM (
          SELECT b.id, 'outcome' AS bet_type, b.match_no, b.pick,
                 NULL AS home_score, NULL AS away_score,
                 b.stake, b.odds, b.settled, b.payout, b.placed_at,
                 b.reason, u.kind, u.login, u.name, u.avatar_url, u.model,
                 u.persona
          FROM bets b JOIN users u ON u.id=b.user_id
          WHERE u.kind='agent'
          UNION ALL
          SELECT b.id, 'score' AS bet_type, b.match_no, 'S' AS pick,
                 b.home_score, b.away_score,
                 b.stake, b.odds, b.settled, b.payout, b.placed_at,
                 b.reason, u.kind, u.login, u.name, u.avatar_url, u.model,
                 u.persona
          FROM score_bets b JOIN users u ON u.id=b.user_id
          WHERE u.kind='agent'
        ) ORDER BY placed_at DESC, id DESC LIMIT ? OFFSET ?""",
        (limit, offset)).fetchall()
    conn.close()
    return [_public_agent_row(dict(r)) for r in rows]


def recent_agent_bets_count() -> int:
    conn = connect()
    row = conn.execute("""
        SELECT (
          SELECT COUNT(*)
          FROM bets b JOIN users u ON u.id=b.user_id
          WHERE u.kind='agent'
        ) + (
          SELECT COUNT(*)
          FROM score_bets b JOIN users u ON u.id=b.user_id
          WHERE u.kind='agent'
        ) AS n
    """).fetchone()
    conn.close()
    return int(row["n"] or 0)


def _review_cutoff(lookback_hours: int) -> str:
    seconds = max(1, int(lookback_hours or 30)) * 3600
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - seconds))


def _agent_outcome_review_rows(conn: sqlite3.Connection,
                               cutoff: str | None = None) -> list[dict]:
    where = ["u.kind='agent'", "b.settled=1"]
    params: list = []
    if cutoff:
        where.append("COALESCE(b.settled_at, b.placed_at) >= ?")
        params.append(cutoff)
    rows = conn.execute(f"""
        SELECT b.id, 'outcome' AS bet_type, b.match_no, b.pick,
               b.stake, b.odds, COALESCE(b.payout, 0) AS payout,
               b.placed_at, b.settled_at, b.reason,
               u.login, u.name, u.model, u.persona,
               m.stage, m.round, m.home, m.away,
               COALESCE(m.settle_score_home, m.score_home) AS score_home,
               COALESCE(m.settle_score_away, m.score_away) AS score_away
        FROM bets b
        JOIN users u ON u.id=b.user_id
        JOIN matches m ON m.match_no=b.match_no
        WHERE {" AND ".join(where)}
    """, params).fetchall()
    return [dict(r) for r in rows]


def _agent_score_review_rows(conn: sqlite3.Connection,
                             cutoff: str | None = None) -> list[dict]:
    where = ["u.kind='agent'", "b.settled=1"]
    params: list = []
    if cutoff:
        where.append("COALESCE(b.settled_at, b.placed_at) >= ?")
        params.append(cutoff)
    rows = conn.execute(f"""
        SELECT b.id, 'score' AS bet_type, b.match_no, 'S' AS pick,
               b.home_score, b.away_score,
               b.stake, b.odds, COALESCE(b.payout, 0) AS payout,
               b.placed_at, b.settled_at, b.reason,
               u.login, u.name, u.model, u.persona,
               m.stage, m.round, m.home, m.away,
               COALESCE(m.settle_score_home, m.score_home) AS score_home,
               COALESCE(m.settle_score_away, m.score_away) AS score_away
        FROM score_bets b
        JOIN users u ON u.id=b.user_id
        JOIN matches m ON m.match_no=b.match_no
        WHERE {" AND ".join(where)}
    """, params).fetchall()
    return [dict(r) for r in rows]


def _betting_summary(rows: list[dict]) -> dict:
    stake = sum(float(r.get("stake") or 0) for r in rows)
    payout = sum(float(r.get("payout") or 0) for r in rows)
    hits = sum(1 for r in rows if float(r.get("payout") or 0) > 0)
    profit = payout - stake
    return {
        "n": len(rows),
        "hits": hits,
        "hit_rate": round(hits / len(rows), 3) if rows else None,
        "stake": round(stake, 2),
        "payout": round(payout, 2),
        "profit": round(profit, 2),
        "roi": round(profit / stake, 3) if stake else None,
        "avg_stake": round(stake / len(rows), 2) if rows else None,
    }


def _odds_bucket(row: dict) -> str:
    odds = float(row.get("odds") or 0)
    if odds < 1.5:
        return "<1.5"
    if odds < 2.0:
        return "1.5-2"
    if odds < 3.0:
        return "2-3"
    if odds < 5.0:
        return "3-5"
    return ">=5"


def _stake_bucket(row: dict) -> str:
    stake = float(row.get("stake") or 0)
    if stake <= 20:
        return "<=20"
    if stake <= 50:
        return "21-50"
    if stake <= 100:
        return "51-100"
    return ">100"


def _strategy_group(row: dict) -> str:
    return "策略组" if str(row.get("persona") or "").strip() else "本色组"


def _group_summaries(rows: list[dict], key_fn,
                     order: list[str] | None = None) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = str(key_fn(row) or "未知")
        grouped.setdefault(key, []).append(row)
    out = []
    for key, items in grouped.items():
        summary = _betting_summary(items)
        summary["key"] = key
        out.append(summary)
    if order:
        rank = {k: i for i, k in enumerate(order)}
        out.sort(key=lambda item: rank.get(item["key"], len(rank)))
    else:
        out.sort(key=lambda item: (item["profit"], item["stake"]))
    return out


def _roi_text(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%"


def _short_metric(row: dict) -> str:
    return (f"{row['key']} {row['n']}注 ROI {_roi_text(row.get('roi'))} "
            f"盈亏 {row.get('profit'):+g}")


def _betting_lessons(metrics: dict) -> list[str]:
    lessons: list[str] = []
    by_pick = {r["key"]: r for r in metrics.get("by_pick", [])}
    away = by_pick.get("A")
    if away and (away.get("roi") or 0) < -0.25:
        lessons.append("客胜需要更高证据门槛；没有阵容/赛程/市场错位支撑时只用小仓。")
    draw = by_pick.get("D")
    if draw and (draw.get("roi") or 0) > -0.05:
        lessons.append("平局并非禁区，但只能在低总球、保守赛况或市场低估时小仓切入。")
    stake_rows = {r["key"]: r for r in metrics.get("by_stake", [])}
    big = stake_rows.get(">100")
    small = stake_rows.get("<=20")
    if big and (big.get("roi") or 0) < -0.2:
        lessons.append("重仓是当前主要风险源；强制覆盖用 10-20，普通判断不轻易越过 60。")
    if small and big and (small.get("roi") or -1) > (big.get("roi") or -1):
        lessons.append("小仓覆盖比硬上重仓更稳；加仓必须写清新增证据，而不是重复同一理由。")
    by_group = {r["key"]: r for r in metrics.get("by_group", [])}
    persona = by_group.get("策略组")
    native = by_group.get("本色组")
    if persona and native and (persona.get("roi") or 0) < (native.get("roi") or 0) - 0.12:
        lessons.append("策略组保留性格，但仓位必须服从统一防爆；嘴硬不等于加码。")
    score = metrics.get("score_summary") or {}
    outcome = metrics.get("outcome_summary") or {}
    if score.get("n") and outcome.get("n"):
        lessons.append("比分和胜平负可同场并存；比分只做 10-15 分小票，不替代主判断。")
    if not lessons:
        lessons.append("样本暂未暴露单一灾区；继续按小仓覆盖、证据加仓、失手降噪执行。")
    return lessons[:5]


def _betting_review_text(metrics: dict) -> str:
    by_pick = " / ".join(_short_metric(r) for r in metrics.get("by_pick", []))
    by_odds = " / ".join(_short_metric(r) for r in metrics.get("by_odds", []))
    by_stake = " / ".join(_short_metric(r) for r in metrics.get("by_stake", []))
    worst_ai = "; ".join(_short_metric(r) for r in metrics.get("ai_worst", [])[:3])
    worst_model = "; ".join(_short_metric(r) for r in metrics.get("model_worst", [])[:3])
    outcome = metrics.get("outcome_summary") or {}
    score = metrics.get("score_summary") or {}
    return "\n".join([
        f"样本：{metrics.get('sample_label')}。",
        (f"胜平负：{outcome.get('n', 0)}注，ROI {_roi_text(outcome.get('roi'))}，"
         f"盈亏 {outcome.get('profit', 0):+g}；"
         f"比分：{score.get('n', 0)}注，ROI {_roi_text(score.get('roi'))}，"
         f"盈亏 {score.get('profit', 0):+g}。"),
        f"方向ROI：{by_pick or '暂无'}。",
        f"赔率段ROI：{by_odds or '暂无'}。",
        f"金额段ROI：{by_stake or '暂无'}。",
        f"AI风险：{worst_ai or '暂无'}。",
        f"模型风险：{worst_model or '暂无'}。",
        "下一轮纪律：" + "；".join(metrics.get("lessons") or []),
    ])


def generate_betting_review(review_date: str | None = None,
                            lookback_hours: int = 30) -> dict:
    """生成并保存每日 Agent 投注复盘，供下一轮 agent 上下文读取。"""
    lookback_hours = max(1, int(lookback_hours or 30))
    review_date = review_date or now()[:10]
    cutoff = _review_cutoff(lookback_hours)
    conn = connect()
    try:
        recent_outcome = _agent_outcome_review_rows(conn, cutoff)
        recent_score = _agent_score_review_rows(conn, cutoff)
        all_outcome = _agent_outcome_review_rows(conn)
        all_score = _agent_score_review_rows(conn)
    finally:
        conn.close()

    recent_n = len(recent_outcome) + len(recent_score)
    use_recent = recent_n >= 8
    outcome_rows = recent_outcome if use_recent else all_outcome
    score_rows = recent_score if use_recent else all_score
    sample_label = (
        f"最近{lookback_hours}小时已结算"
        if use_recent else f"累计已结算（最近{lookback_hours}小时样本不足）"
    )

    by_agent = _group_summaries(outcome_rows, lambda r: r.get("login"))
    by_model = _group_summaries(outcome_rows, lambda r: r.get("model"))
    metrics = {
        "review_date": review_date,
        "generated_at": now(),
        "lookback_hours": lookback_hours,
        "settled_after": cutoff,
        "sample_label": sample_label,
        "recent_counts": {
            "outcome": len(recent_outcome),
            "score": len(recent_score),
        },
        "outcome_summary": _betting_summary(outcome_rows),
        "score_summary": _betting_summary(score_rows),
        "by_pick": _group_summaries(outcome_rows, lambda r: r.get("pick"),
                                    ["H", "D", "A"]),
        "by_odds": _group_summaries(outcome_rows, _odds_bucket,
                                    ["<1.5", "1.5-2", "2-3", "3-5", ">=5"]),
        "by_stake": _group_summaries(outcome_rows, _stake_bucket,
                                     ["<=20", "21-50", "51-100", ">100"]),
        "by_group": _group_summaries(outcome_rows, _strategy_group,
                                     ["本色组", "策略组"]),
        "ai_best": sorted(by_agent, key=lambda x: x["profit"], reverse=True)[:5],
        "ai_worst": sorted(by_agent, key=lambda x: x["profit"])[:5],
        "model_best": sorted(by_model, key=lambda x: x["profit"], reverse=True)[:5],
        "model_worst": sorted(by_model, key=lambda x: x["profit"])[:5],
    }
    metrics["lessons"] = _betting_lessons(metrics)
    summary_text = _betting_review_text(metrics)

    with transaction() as conn:
        conn.execute("""
            INSERT INTO betting_reviews
              (review_date, generated_at, lookback_hours, settled_after,
               settled_bets, score_bets, summary_text, metrics_json)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(review_date) DO UPDATE SET
              generated_at=excluded.generated_at,
              lookback_hours=excluded.lookback_hours,
              settled_after=excluded.settled_after,
              settled_bets=excluded.settled_bets,
              score_bets=excluded.score_bets,
              summary_text=excluded.summary_text,
              metrics_json=excluded.metrics_json
        """, (review_date, metrics["generated_at"], lookback_hours, cutoff,
              len(recent_outcome), len(recent_score), summary_text,
              json.dumps(metrics, ensure_ascii=False)))
    return {"summary_text": summary_text, "metrics": metrics}


def latest_betting_review() -> dict | None:
    conn = connect()
    row = conn.execute("""
        SELECT * FROM betting_reviews
        ORDER BY generated_at DESC, review_date DESC
        LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    try:
        out["metrics"] = json.loads(out.get("metrics_json") or "{}")
    except json.JSONDecodeError:
        out["metrics"] = {}
    return out


def betting_review_context() -> dict | None:
    review = latest_betting_review()
    if not review:
        return None
    metrics = review.get("metrics") or {}
    return {
        "生成时间": review.get("generated_at"),
        "样本": metrics.get("sample_label"),
        "胜平负总体": metrics.get("outcome_summary"),
        "比分总体": metrics.get("score_summary"),
        "按方向ROI": metrics.get("by_pick"),
        "按赔率段ROI": metrics.get("by_odds"),
        "按金额段ROI": metrics.get("by_stake"),
        "按策略组ROI": metrics.get("by_group"),
        "AI风险榜": metrics.get("ai_worst"),
        "AI正向样本": metrics.get("ai_best"),
        "模型风险榜": metrics.get("model_worst"),
        "下一轮纪律": metrics.get("lessons"),
        "摘要": review.get("summary_text"),
    }


def agent_action_add(agent_id: int | None, agent_login: str, action: str,
                     status: str, message: str = "",
                     target: dict | None = None,
                     payload: dict | None = None,
                     created_refs: dict | None = None,
                     raw: dict | None = None) -> int:
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO agent_actions
            (ts, agent_id, agent_login, action, status, message, target_json,
             payload_json, created_refs_json, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (now(), agent_id, agent_login, action, status, message,
             json.dumps(target or {}, ensure_ascii=False),
             json.dumps(payload or {}, ensure_ascii=False),
             json.dumps(created_refs or {}, ensure_ascii=False),
             json.dumps(raw or {}, ensure_ascii=False)))
        return cur.lastrowid


def recent_agent_actions(limit: int = 30) -> list[dict]:
    conn = connect()
    rows = conn.execute("""
        SELECT aa.*, u.name, u.model
        FROM agent_actions aa
        LEFT JOIN users u ON u.id = aa.agent_id
        ORDER BY aa.id DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("target_json", "payload_json", "created_refs_json",
                    "raw_json"):
            try:
                d[key[:-5]] = json.loads(d.get(key) or "{}")
            except json.JSONDecodeError:
                d[key[:-5]] = {}
        out.append(d)
    return out


def user_bets(user_id: int) -> list[dict]:
    conn = connect()
    rows = conn.execute("""
        SELECT * FROM (
          SELECT b.id, 'outcome' AS bet_type, b.user_id, b.match_no, b.pick,
                 NULL AS home_score_pick, NULL AS away_score_pick,
                 b.stake, b.odds, b.placed_at, b.settled, b.payout,
                 b.settled_at, b.reason,
                 m.home, m.away, m.date_utc, m.score_home, m.score_away,
                 m.settle_score_home, m.settle_score_away, m.score_type,
                 u.kind, u.login, u.name, u.avatar_url, u.model, u.persona
          FROM bets b
          JOIN matches m ON m.match_no = b.match_no
          JOIN users u ON u.id = b.user_id
          WHERE b.user_id=?
          UNION ALL
          SELECT b.id, 'score' AS bet_type, b.user_id, b.match_no, 'S' AS pick,
                 b.home_score AS home_score_pick,
                 b.away_score AS away_score_pick,
                 b.stake, b.odds, b.placed_at, b.settled, b.payout,
                 b.settled_at, b.reason,
                 m.home, m.away, m.date_utc, m.score_home, m.score_away,
                 m.settle_score_home, m.settle_score_away, m.score_type,
                 u.kind, u.login, u.name, u.avatar_url, u.model, u.persona
          FROM score_bets b
          JOIN matches m ON m.match_no = b.match_no
          JOIN users u ON u.id = b.user_id
          WHERE b.user_id=?
        ) ORDER BY placed_at DESC""", (user_id, user_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def balance_timeline(user_id: int) -> list[dict]:
    """净资产随时间的曲线（折线图数据）。

    净资产 = 可用余额 + 在预测额 - 积分债务 + 应收 - 系统债务。
    提交预测只是把钱从可用挪到在投；积分支持本金转入/偿还也是现金与债权
    积分债务互换；系统银行借款/还款也是现金与债务互换。真正改变净资产
    的是预测结算盈亏、支持积分分成、每日奖励和系统利息。
    """
    conn = connect()
    # 初始发放、补助和每日奖励直接计入净资产基数（不含 bet/payout 流水，避免重复计）
    base = conn.execute("SELECT delta, ts FROM wallet_ledger WHERE user_id=? "
                        "AND reason IN ('init','bonus','daily_top_bonus') ORDER BY id",
                        (user_id,)).fetchall()
    # 已结算的注：在结算时刻产生 (payout - stake) 的净资产变动（输则即 -stake）
    settled = conn.execute(
        "SELECT (payout - stake) AS delta, "
        "COALESCE(settled_at, placed_at) AS ts FROM bets "
        "WHERE user_id=? AND settled=1", (user_id,)).fetchall()
    score_settled = conn.execute(
        "SELECT (payout - stake) AS delta, "
        "COALESCE(settled_at, placed_at) AS ts FROM score_bets "
        "WHERE user_id=? AND settled=1", (user_id,)).fetchall()
    profit_share = conn.execute(
        "SELECT delta, ts FROM wallet_ledger WHERE user_id=? "
        "AND reason='investment_profit_share' ORDER BY id",
        (user_id,)).fetchall()
    system_interest = conn.execute(
        "SELECT -debt_delta AS delta, ts FROM system_loan_events "
        "WHERE user_id=? AND kind='interest' ORDER BY id",
        (user_id,)).fetchall()
    conn.close()
    events = [(r["ts"], r["delta"]) for r in base] \
        + [(r["ts"], r["delta"]) for r in settled] \
        + [(r["ts"], r["delta"]) for r in score_settled] \
        + [(r["ts"], r["delta"]) for r in profit_share] \
        + [(r["ts"], r["delta"]) for r in system_interest]
    events.sort(key=lambda e: e[0])
    nw, out = 0, []
    for ts, delta in events:
        nw += delta
        out.append({"ts": ts, "balance": nw})
    return out


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
