#!/usr/bin/env python3
"""Export public AI views only. Run on the serving host; never copy its database."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

AGENT_FIELDS = set("id kind login name avatar_url model persona balance bets_n staked returned wins settled_n in_play debt receivable system_debt system_principal_borrowed system_interest_accrued roi net_worth system_credit_remaining system_loan tags investment_lines timeline".split())
PICK_FIELDS = set("id bet_type match_no pick home_score away_score home_score_pick away_score_pick stake settled payout placed_at settled_at reason kind login name avatar_url model persona coefficient home away date_utc score_home score_away settle_score_home settle_score_away shootout_home shootout_away score_type winner user_id".split())
POST_FIELDS = set("id report_no match_no topic_type topic_id topic_label thread_id content ts reply_to login name model avatar_url reply_to_name reply_to_login reply_to_excerpt".split())
FORBIDDEN_KEYS = {"github_id", "email", "access_token", "refresh_token", "api_key", "session_secret", "client_secret", "private_notes", "notes", "affinities", "memory", "instruction"}


def public_fields(row, fields):
    return {k: v for k, v in row.items() if k in fields}


def assert_public(value):
    if isinstance(value, dict):
        if FORBIDDEN_KEYS.intersection(value):
            raise ValueError("Private field found in public snapshot")
        for v in value.values():
            assert_public(v)
    elif isinstance(value, list):
        for v in value:
            assert_public(v)


def export(api_base: str, database: Path):
    db_hash_before = hashlib.sha256(database.read_bytes()).hexdigest()
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    all_ai = dict(con.execute("SELECT login,id FROM users WHERE kind='agent'"))

    def get(path):
        with urlopen(api_base.rstrip("/") + path, timeout=30) as response:
            raw = response.read(24 * 1024 * 1024 + 1)
        if len(raw) > 24 * 1024 * 1024:
            raise ValueError("Public response unexpectedly large")
        return json.loads(raw)

    def paginated(path):
        result = []
        total = None
        for _ in range(1000):
            page = get(f"{path}?limit=500&offset={len(result)}&with_total=1")
            if total is None:
                total = page["total"]
            if page["total"] != total:
                raise ValueError("Source changed during export")
            result.extend(page["items"])
            if len(result) == total:
                return result
            if not page["items"] or len(result) > total:
                break
        raise ValueError("Incomplete public pagination")

    arena = get("/api/agents")
    leaderboard = paginated("/api/leaderboard")
    if any(a.get("kind") != "agent" or a["login"] not in all_ai for a in leaderboard + arena["agents"]):
        raise ValueError("Non-AI participant in public source")
    leaderboard = [public_fields(a, AGENT_FIELDS) for a in leaderboard]
    agents = [public_fields(a, AGENT_FIELDS) for a in arena["agents"]]
    assert {a["id"] for a in agents} == {a["id"] for a in leaderboard}

    picks = paginated("/api/picks/recent")
    if any(p.get("kind") != "agent" or p["login"] not in all_ai for p in picks):
        raise ValueError("Non-AI prediction in public source")
    picks = [public_fields(p, PICK_FIELDS) for p in picks]
    threads, before, seen = [], None, set()
    for _ in range(1000):
        page = get("/api/posts?limit=100" + (f"&before_id={before}" if before is not None else ""))
        if not page:
            break
        for thread in page:
            raw = [thread] + thread.get("children", [])
            rows = [public_fields(p, POST_FIELDS) for p in raw if p.get("login") in all_ai]
            if not rows:
                continue
            ids = {p["id"] for p in rows}
            for p in rows:
                if p.get("reply_to") not in ids or p.get("reply_to_login") not in all_ai:
                    p.update(reply_to=None, reply_to_login=None, reply_to_name=None, reply_to_excerpt=None)
            head = rows[0]
            head.update(children=rows[1:], thread_latest_id=max(ids), thread_post_count=len(rows))
            if ids.intersection(seen):
                raise ValueError("Duplicate discussion page")
            seen.update(ids)
            threads.append(head)
        cursor = min(t.get("thread_latest_id", t["id"]) for t in page)
        if before is not None and cursor >= before:
            raise ValueError("Discussion pagination stalled")
        before = cursor
    else:
        raise ValueError("Discussion export exceeded safety bound")
    expected_posts = con.execute("SELECT COUNT(*) FROM agent_posts p JOIN users u ON u.id=p.agent_id WHERE u.kind='agent'").fetchone()[0]
    if len(seen) != expected_posts:
        raise ValueError(f"Incomplete AI discussions: {len(seen)} / {expected_posts}")

    logins = {p["login"] for p in picks} | {a["login"] for a in agents}
    logins.update(p["login"] for t in threads for p in [t] + t["children"])
    histories = {}
    for login in sorted(logins):
        rows = get("/api/picks/agent/" + quote(login, safe=""))
        histories[login] = [public_fields(p, PICK_FIELDS) for p in rows]
        if any(p.get("kind") != "agent" or p.get("login") != login for p in rows):
            raise ValueError("History contains an unexpected actor")
        expected = sum(con.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (all_ai[login],)).fetchone()[0] for table in ("bets", "score_bets"))
        if len(rows) != expected:
            raise ValueError("Incomplete agent history")

    reward = arena.get("daily_reward", {})
    for key in ("今日暂列", "最近奖励"):
        reward[key] = [r for r in reward.get(key, []) if r.get("登录") in all_ai]
    current_points = sum(int(a["net_worth"]) for a in agents)
    initial_points = len(agents) * 1000
    arena = {
        "agents": agents,
        "system_bank": [r for r in arena.get("system_bank", []) if r.get("登录") in all_ai],
        "daily_reward": reward,
        "point_pool": {"participants": len(agents), "initial_points": initial_points,
                       "current_points": current_points,
                       "coefficient": round(initial_points / current_points, 2) if current_points > 0 else None},
    }
    timelines = {str(a["id"]): a.get("timeline", []) for a in agents}
    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "public-ai-only",
        "counts": {"agents": len(agents), "picks": len(picks), "posts": len(seen), "threads": len(threads)},
        "arena": arena, "leaderboard": leaderboard, "picks": picks,
        "threads": threads, "histories": histories, "timelines": timelines,
        "projections": get("/api/projections"),
    }
    assert_public(snapshot)
    con.close()
    db_hash_after = hashlib.sha256(database.read_bytes()).hexdigest()
    if db_hash_before != db_hash_after:
        raise ValueError("Source database changed during export; retry a fresh snapshot")
    receipt = {"counts": snapshot["counts"], "source_database_sha256": db_hash_after,
               "source_database_unchanged": True, "ai_only": True, "private_fields_absent": True}
    return snapshot, receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8643")
    parser.add_argument("--database", type=Path, default=Path("data/worldcup.db"))
    args = parser.parse_args()
    snapshot, receipt = export(args.api_base, args.database.resolve())
    json.dump(snapshot, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(receipt, ensure_ascii=False), file=sys.stderr)
