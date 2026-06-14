"""统一 JSON Action Agent 调度器。

模型只提交一个结构化行动申请；下注、评论、笔记等真实写入都由后端
按数据库当前事实重新校验后执行。

用法：
  python3 -m src.agent_session
  python3 -m src.agent_session --rounds 20 --seed 42
  python3 -m src.agent_session --only claude-fun --dry-run

注意：--dry-run 只隔离数据库写入，仍会真实调用模型并消耗 token。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db
from .agents import _load_cfg, _parse_json, _tokens_today
from .gateway import Gateway
from .model import exact_score_prob

ROOT = Path(__file__).resolve().parent.parent

VALID_ACTIONS = {
    "read_data",
    "read_intel",
    "place_bet",
    "place_score_bet",
    "write_discussion_post",
    "reply_comment",
    "manage_notes",
    "review_own_performance",
    "request_investment",
    "respond_investment",
    "create_funding_invite",
    "accept_funding_invite",
    "adjust_affinity",
    "pass",
}
PUBLIC_SPEECH_ACTIONS = {
    "write_discussion_post",
    "reply_comment",
    "create_funding_invite",
}
ALL_ACTIONS_HINT = (
    "read_data|read_intel|place_bet|write_discussion_post|"
    "place_score_bet|reply_comment|manage_notes|review_own_performance|request_investment|"
    "respond_investment|create_funding_invite|accept_funding_invite|"
    "adjust_affinity|pass"
)
BET_ONLY_ACTIONS_HINT = (
    "read_data|read_intel|place_bet|place_score_bet|manage_notes|review_own_performance|"
    "request_investment|respond_investment|adjust_affinity|pass"
)
ALL_ACTION_DESCRIPTIONS = """- read_data：公共数据已在上下文里；选择它只表示继续观察。
- read_intel：target.intel_ids=[情报id]，最多 3 条；系统会给全文后继续本次活动。
- place_bet：target.match_no，payload.pick=H/D/A，payload.stake，payload.reason<=40字。
- place_score_bet：target.match_no，payload.home_score、payload.away_score、payload.stake<=50，payload.reason<=40字。
- write_discussion_post：payload.text；可选 target.match_no 或 target.report_no 作为话题标签。
- reply_comment：target.reply_to，payload.text；回复讨论区真实已有帖子。
- manage_notes：payload.add/update/delete 管理私有笔记。
- review_own_performance：payload.text 写你的复盘结论，系统会存成私有笔记。
- request_investment：target.agent_login 指定投资方；payload.amount、payload.profit_share、payload.reason。
- respond_investment：target.offer_id；payload.decision=accept/decline，payload.reason。
- create_funding_invite：payload.text 公开求小额注资；payload.min_amount/max_amount/desired_amount/profit_share/reason。
- accept_funding_invite：target.invite_id；payload.amount、payload.reason；接受别人公开注资邀请。
- adjust_affinity：target.agent_login 指定另一个 AI；payload.delta=-15..15，payload.reason。
- pass：payload.reason 简短说明为什么观望，并结束本次活动。

评论建议 30~90 字；没新观点就 pass。"""
BET_ONLY_ACTION_DESCRIPTIONS = """- read_data：公共数据已在上下文里；选择它只表示继续观察。
- read_intel：target.intel_ids=[情报id]，最多 3 条；系统会给全文后继续本次活动。
- place_bet：target.match_no，payload.pick=H/D/A，payload.stake，payload.reason<=40字。
- place_score_bet：target.match_no，payload.home_score、payload.away_score、payload.stake<=50，payload.reason<=40字。
- manage_notes：payload.add/update/delete 管理私有笔记，只用于下注假设和复盘。
- review_own_performance：payload.text 写你的复盘结论，系统会存成私有笔记。
- request_investment：target.agent_login 指定投资方；payload.amount、payload.profit_share、payload.reason。
- respond_investment：target.offer_id；payload.decision=accept/decline，payload.reason。
- adjust_affinity：target.agent_login 指定另一个 AI；payload.delta=-15..15，payload.reason。
- pass：payload.reason 简短说明为什么观望，并结束本次活动。

没有下注价值就 pass；不要输出公开评论或回复。"""
MIN_STAKE = 10
MAX_STAKE = 100000
MAX_SCORE_STAKE = 50
SCORE_MAX_GOALS = 6
SCORE_ODDS_CAP_P = 1 / 80
COMMENT_MAX = 220
REASON_MAX = 80
MAX_INTEL = 3
MAX_NOTE_OPS = 5
MAX_INVESTMENT = db.INVESTMENT_AMOUNT_CAP
MAX_PROFIT_SHARE = db.INVESTMENT_PROFIT_SHARE_CAP
FUNDING_INVITE_MIN = db.FUNDING_INVITE_MIN_AMOUNT
FUNDING_INVITE_MAX = db.FUNDING_INVITE_MAX_AMOUNT
MAX_AFFINITY_DELTA = db.AFFINITY_DELTA_CAP
DEFAULT_MAX_AFFINITY_ADJUSTS_PER_TURN = 2
DEFAULT_MAX_STEPS = 3
DEFAULT_MAX_PUBLIC_POSTS_PER_TURN = 1
DEFAULT_MAX_BETS_PER_TURN = 1
DEFAULT_MAX_INTEL_READS_PER_TURN = 1

SYSTEM_ACTION = """你是「{name}」，2026 世界杯 AI 竞技场里的自主行动 AI。
{style}
{action_policy}

你会收到公共数据、讨论区帖子、情报索引、自己的余额/投注/私有笔记/融资债务、最近行动流。
一次活动会由多个步骤组成；每一步只能选择一个行动。你不是直接操作数据库，而是提交一个 JSON 行动申请，
系统会按真实余额、开球时间、赔率、评论目标重新校验，通过才执行。
你可以在统一 AI 讨论区发帖，也可以回复公共数据里的最近帖子或本次运行最近行动里带 post_id 的新帖子。
如果选择 read_intel，系统会把情报全文加入本轮上下文，再让你继续后续步骤。
如果“你的融资状态”里有待你处理的融资请求，优先用 respond_investment 明确接受或拒绝。
融资接受后会扣你的余额、给对方到账；对方后续赢钱会先还本金，再按承诺 profit_share 给你分利润。
融资方亏光不会平账，未还本金会继续留作债务。
正式融资请求有 24 小时冷却；冷却期不要反复申请。你可以先在讨论区说服潜在投资方，
或用 manage_notes 写/更新“画像: 某AI”的小本本，把对方风格、信用、嘴硬程度、投资价值记下来。
公开注资邀请是轻量小额融资：create_funding_invite 会在讨论区发帖并允许娱乐组 AI 用
accept_funding_invite 小额注资；本色组不参与。它仍会生成真实债务，后续盈利同样先还本金再分成。
如果你有“主动任务”，优先围绕任务行动；可以嘴硬、装可怜、许诺分成，但不要人身攻击或无意义刷屏。
你也可以用 adjust_affinity 调整自己对其他 AI 的好感/信任，初始都是 100；它会影响你后续判断。
选择 pass 表示结束本次活动；没有明确价值就 pass。

只输出一个 JSON 对象，不要 markdown、不要解释、不要代码块。
统一格式：
{{
  "action": "{actions_hint}",
  "target": {{}},
  "payload": {{}}
}}

动作说明：
{action_descriptions}"""


@contextmanager
def isolated_db(enabled: bool):
    """dry-run 使用临时 DB 副本；仍会真实调用模型。"""
    if not enabled:
        yield
        return
    old_path = db.DB_PATH
    with tempfile.TemporaryDirectory(prefix="wc-agent-dry-run-") as tmp:
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


def _load_results() -> dict:
    path = ROOT / "out" / "results.json"
    if not path.exists():
        raise FileNotFoundError("缺少 out/results.json，请先运行 update.sh")
    return json.loads(path.read_text(encoding="utf-8"))


def _utc(date_utc: str) -> datetime:
    dt = datetime.fromisoformat(date_utc.replace(" ", "T").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _team_names(data: dict) -> dict[str, str]:
    return {t["code"]: t.get("name_zh") or t.get("name_en") or t["code"]
            for t in data.get("teams", [])}


def _match_row(data: dict, match_no: int) -> dict | None:
    return next((m for m in data.get("schedule", [])
                 if int(m.get("match", 0)) == match_no), None)


def _match_label(m: dict, names: dict[str, str]) -> str:
    home = names.get(m.get("home"), m.get("home") or m.get("slot_home") or "?")
    away = names.get(m.get("away"), m.get("away") or m.get("slot_away") or "?")
    return f"{home} vs {away}"


def _odds_for_pred(pred: dict) -> dict[str, float]:
    return {
        "H": round(1 / max(float(pred["p_home"]), 0.02), 2),
        "D": round(1 / max(float(pred["p_draw"]), 0.02), 2),
        "A": round(1 / max(float(pred["p_away"]), 0.02), 2),
    }


def _score_odds_for_match(m: dict, teams: dict[str, dict]) -> dict[str, float]:
    pred = m.get("pred") or {}
    home = teams.get(m.get("home"))
    away = teams.get(m.get("away"))
    if not pred or not home or not away:
        return {}
    out = {}
    for gh in range(SCORE_MAX_GOALS + 1):
        for ga in range(SCORE_MAX_GOALS + 1):
            p = exact_score_prob(home, away, gh, ga, we_override=pred)
            out[f"{gh}-{ga}"] = round(1 / max(p, SCORE_ODDS_CAP_P), 2)
    return out


def _advisor_note(fable: dict) -> str:
    bits = []
    if fable.get("delta"):
        bits.append(f"主客{fable['delta']:+g}pp")
    if fable.get("draw"):
        bits.append(f"平局{fable['draw']:+g}pp")
    if fable.get("total"):
        bits.append(f"总球{fable['total']:+g}")
    return f"{' / '.join(bits) or '微调'} · {fable.get('note', '')}"


def _compact_match(m: dict, names: dict[str, str], teams: dict[str, dict],
                   blurbs: dict[str, dict]) -> dict:
    pred = m.get("pred") or {}
    item: dict[str, Any] = {
        "match_no": m["match"],
        "对阵": _match_label(m, names),
        "阶段": m.get("stage"),
        "开球UTC": m.get("date_utc"),
        "已赛": bool(m.get("score")),
        "比分": m.get("score"),
    }
    if pred:
        item["AI概率"] = {
            "H": pred.get("p_home"),
            "D": pred.get("p_draw"),
            "A": pred.get("p_away"),
        }
        item["赔率"] = _odds_for_pred(pred)
        score_odds = {} if m.get("score") else _score_odds_for_match(m, teams)
        if score_odds:
            item["比分赔率"] = score_odds
        item["市场盘口"] = pred.get("market")
        if pred.get("fable"):
            item["Codex微调"] = _advisor_note(pred["fable"])
    blurb = blurbs.get(str(m["match"]))
    if blurb:
        item["看点"] = blurb["text"]
    return item


def _betting_table(data: dict) -> dict[int, dict]:
    names = _team_names(data)
    teams = {t["code"]: t for t in data.get("teams", [])}
    now = datetime.now(timezone.utc)
    out = {}
    for m in data.get("schedule", []):
        pred = m.get("pred")
        if not pred or m.get("score") or not (m.get("home") and m.get("away")):
            continue
        ko = _utc(m["date_utc"])
        if ko <= now:
            continue
        out[int(m["match"])] = {
            "match_no": int(m["match"]),
            "对阵": _match_label(m, names),
            "date_utc": m["date_utc"],
            "odds": _odds_for_pred(pred),
            "score_odds": _score_odds_for_match(m, teams),
        }
    return out


def _public_context(data: dict) -> dict:
    names = _team_names(data)
    teams = {t["code"]: t for t in data.get("teams", [])}
    blurbs = db.load_blurbs()
    now = datetime.now(timezone.utc)
    future, focus, finished = [], [], []
    for m in data.get("schedule", []):
        if not (m.get("home") and m.get("away")):
            continue
        ko = _utc(m["date_utc"])
        delta_h = (ko - now).total_seconds() / 3600
        if m.get("score"):
            finished.append(_compact_match(m, names, teams, blurbs))
        elif m.get("pred") and 0 < delta_h <= 72:
            future.append(_compact_match(m, names, teams, blurbs))
        if -12 <= delta_h <= 36:
            focus.append(_compact_match(m, names, teams, blurbs))

    finished.sort(key=lambda x: x.get("开球UTC") or "", reverse=True)
    future.sort(key=lambda x: x.get("开球UTC") or "")
    focus.sort(key=lambda x: x.get("开球UTC") or "")
    reports = db.load_reports()
    posts = db.agent_posts(24)
    advisor_logins = {
        getattr(db, "PUBLIC_ADVISOR_LOGIN", "codex"),
        *getattr(db, "LEGACY_ADVISOR_LOGINS", set()),
    }
    board = [
        r for r in db.leaderboard(100)
        if r.get("kind") == "agent"
        and str(r.get("login") or "").lower() not in advisor_logins
    ]
    ai_board = [
        {"名字": r["name"] or r["login"], "登录": r["login"],
         "净资产": r["net_worth"], "余额": r["balance"],
         "在投": r["in_play"], "债务": r.get("debt", 0),
         "应收": r.get("receivable", 0), "ROI": r["roi"],
         "投注数": r["bets_n"], "已结": r["settled_n"],
         "胜场": r["wins"], "标签": r.get("tags") or []}
        for r in board
    ]
    eliminated = [r for r in ai_board if r["净资产"] <= 0]
    at_risk = [r for r in ai_board
               if r["净资产"] > 0 and (r["余额"] <= 50 or r["净资产"] <= 250)]
    return {
        "当前时间UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "未来72小时可投比赛": future[:12],
        "近期焦点比赛": focus[:12],
        "最近赛果": finished[:8],
        "夺冠概率Top5": [
            {"队": t.get("name_zh"), "AI": t.get("p_champion"),
             "市场": t.get("p_champion_market")}
            for t in data.get("teams", [])[:5]
        ],
        "预测战绩": data.get("record", {}).get("stats", {}),
        "AI积分榜": ai_board,
        "出局AI": eliminated,
        "濒危AI": at_risk,
        "全站最近AI投注": db.recent_agent_bets(30),
        "最近3期战报": [
            {"期数": r["no"], "日期": r["date"], "正文": r["report"]}
            for r in reports[-3:]
        ],
        "讨论区最新帖子": [
            {"id": p["id"],
             "话题": p.get("topic_label")
                    or (f"比赛#{p['match_no']}" if p.get("match_no")
                        else f"战报#{p['report_no']}" if p.get("report_no")
                        else "AI讨论"),
             "作者": p["name"], "内容": p["content"],
             "回复给": p.get("reply_to"), "点赞": p["likes"]}
            for p in posts
        ],
        "投融资状态": db.investment_public_summary(12),
        "公开注资邀请": db.funding_invite_public_summary(12),
        "情报区索引": db.intel_index(12),
        "最近AI行动": [
            {"AI": a.get("name") or a.get("agent_login"),
             "动作": a["action"], "状态": a["status"],
             "结果": a["message"], "时间": a["ts"]}
            for a in db.recent_agent_actions(12)
        ],
    }


def _performance_summary(user_id: int, limit: int = 30) -> dict:
    rows = db.user_bets(user_id)[:limit]
    settled = [b for b in rows if b["settled"]]
    staked = sum(int(b["stake"] or 0) for b in settled)
    returned = sum(int(b["payout"] or 0) for b in settled)
    by_pick = {p: 0 for p in ("H", "D", "A")}
    by_bucket = {"低赔率<=1.8": 0, "中赔率": 0, "高赔率>=2.5": 0}
    for b in rows:
        by_pick[b["pick"]] = by_pick.get(b["pick"], 0) + 1
        odds = float(b["odds"])
        if odds <= 1.8:
            by_bucket["低赔率<=1.8"] += 1
        elif odds >= 2.5:
            by_bucket["高赔率>=2.5"] += 1
        else:
            by_bucket["中赔率"] += 1
    losing_streak = 0
    for b in settled:
        if int(b["payout"] or 0) > 0:
            break
        losing_streak += 1
    return {
        "最近投注数": len(rows),
        "已结算数": len(settled),
        "胜率": (round(sum(1 for b in settled if b["payout"] > 0)
                     / len(settled), 3) if settled else None),
        "净盈亏": returned - staked,
        "ROI": (round((returned - staked) / staked, 4) if staked else None),
        "方向偏好": by_pick,
        "赔率区间": by_bucket,
        "当前连亏": losing_streak,
        "未结投注": [
            {"场次": b["match_no"],
             "方向": (f"比分 {b.get('home_score_pick')}-{b.get('away_score_pick')}"
                    if b.get("bet_type") == "score" else b["pick"]),
             "注额": b["stake"],
             "赔率": b["odds"], "理由": b.get("reason")}
            for b in rows if not b["settled"]
        ][:8],
    }


def _ensure_agent(agent_cfg: dict, gw: Gateway) -> dict:
    gw_model = gw.models.get(agent_cfg["model"], {})
    row = db.ensure_agent_user(
        agent_cfg["id"], agent_cfg["name"],
        gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    fresh = db.get_user(row["id"])
    if not fresh:
        raise RuntimeError("agent user create failed")
    return fresh


def _system_prompt(agent_cfg: dict) -> str:
    persona = (agent_cfg.get("persona") or "").strip()
    if persona:
        style = f"你有人设：{persona}\n请保持这个策略风格和说话腔调。"
        action_policy = "你可以下注、评论、回复、写私有笔记或复盘；公开发言要短。"
        actions_hint = ALL_ACTIONS_HINT
        action_descriptions = ALL_ACTION_DESCRIPTIONS
    else:
        style = ("你是本色组：不设人设，只做冷静、简短、基于事实的模型判断；"
                 "不要写角色梗。")
        action_policy = (
            "本色组只考虑投注、投融资和私有复盘，不参与公开发言；不要选择 "
            "write_discussion_post、reply_comment。"
        )
        actions_hint = BET_ONLY_ACTIONS_HINT
        action_descriptions = BET_ONLY_ACTION_DESCRIPTIONS
    return SYSTEM_ACTION.format(name=agent_cfg["name"], style=style,
                                action_policy=action_policy,
                                actions_hint=actions_hint,
                                action_descriptions=action_descriptions)


def _is_bench_agent(agent_cfg: dict) -> bool:
    return not (agent_cfg.get("persona") or "").strip()


def _agent_context(me: dict, public: dict, session_events: list[dict],
                   intel_docs: list[dict] | None = None,
                   turn_state: dict | None = None) -> dict:
    bets = db.user_bets(me["id"])[:15]
    ctx = {
        "你的状态": {"余额": me["balance"], "登录": me["login"]},
        "你的近期投注": [
            {"场次": b["match_no"],
             "方向": (f"比分 {b.get('home_score_pick')}-{b.get('away_score_pick')}"
                    if b.get("bet_type") == "score" else b["pick"]),
             "注额": b["stake"],
             "赔率": b["odds"], "已结": bool(b["settled"]),
             "派彩": b["payout"], "理由": b.get("reason")}
            for b in bets
        ],
        "你的近期表现": _performance_summary(me["id"]),
        "你的私有笔记": db.agent_notes_list(me["id"]),
        "你的融资状态": db.investment_context(me["id"]),
        "你的主动任务": [
            {k: task.get(k) for k in ("id", "title", "instruction",
                                      "priority", "created_at", "expires_at")}
            for task in db.agent_tasks_for_context(me["id"])
        ],
        "你的公开注资邀请": db.funding_invites_context(me["id"]),
        "你对其他AI的印象": db.agent_affinities(me["id"]),
        "公共数据": public,
        "本次运行最近行动": session_events[-12:],
    }
    if turn_state:
        ctx["本轮状态"] = turn_state
    if intel_docs:
        ctx["本轮已读情报全文"] = [
            {"id": d["id"], "标题": d["title"], "正文": d["content"],
             "来源": d.get("source")}
            for d in intel_docs
        ]
    return ctx


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize_action(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {"action": "pass", "target": {}, "payload": {"reason": "输出不是对象"},
                "raw": {}}
    action = str(raw.get("action") or "").strip().lower()
    target = dict(_as_dict(raw.get("target")))
    payload = dict(_as_dict(raw.get("payload")))

    for key in ("match_no", "report_no", "reply_to", "offer_id", "invite_id",
                "home_score", "away_score", "score",
                "agent_login", "target_agent_login", "target_login",
                "lender_login", "lender", "investor",
                "topic_type", "topic_id"):
        if key in raw and key not in target:
            target[key] = raw[key]
    for key in ("pick", "stake", "reason", "text", "intel_ids",
                "home_score", "away_score", "score",
                "add", "update", "delete", "notes_add", "notes_update",
                "notes_delete", "amount", "profit_share", "decision",
                "min_amount", "max_amount", "desired_amount", "invite_id",
                "agent_login", "target_agent_login", "target_login",
                "lender_login", "lender", "investor", "delta",
                "topic_type", "topic_id"):
        if key in raw and key not in payload:
            payload[key] = raw[key]

    if not action:
        if raw.get("read_intel"):
            action = "read_intel"
            target["intel_ids"] = raw.get("read_intel")
        elif raw.get("bets"):
            action = "place_bet"
            bet = (raw.get("bets") or [{}])[0]
            target["match_no"] = bet.get("match_no")
            payload.update({k: bet.get(k) for k in ("pick", "stake", "reason")})
        elif raw.get("notes_add") or raw.get("notes_update") or raw.get("notes_delete"):
            action = "manage_notes"
        elif raw.get("comment"):
            action = "write_discussion_post"
            comment = raw.get("comment")
            payload["text"] = (comment if isinstance(comment, str)
                               else _as_dict(comment).get("text"))
            target["reply_to"] = _as_dict(comment).get("reply_to")
        else:
            action = "pass"
    if action == "comment":
        action = "write_discussion_post"
    elif action == "reply":
        action = "reply_comment"
    elif action in {"post", "new_post", "discussion", "write_post",
                    "write_discussion", "forum_post"}:
        action = "write_discussion_post"
    elif action in {"borrow", "request_funding", "request_financing",
                    "ask_investment", "ask_funding"}:
        action = "request_investment"
    elif action in {"score_bet", "place_exact_score", "exact_score_bet",
                    "bet_score", "place_score"}:
        action = "place_score_bet"
    elif action in {"funding_response", "investment_response",
                    "respond_funding", "respond_financing"}:
        action = "respond_investment"
    elif action in {"create_funding_invite", "funding_invite",
                    "open_funding_invite", "funding_pitch",
                    "public_funding_invite", "ask_public_funding",
                    "beg_funding", "funding_post"}:
        action = "create_funding_invite"
    elif action in {"accept_funding_invite", "accept_public_funding",
                    "fund_invite", "invest_invite",
                    "respond_funding_invite"}:
        action = "accept_funding_invite"
    elif action in {"affinity", "adjust_relation", "adjust_relationship",
                    "relationship", "like_ai", "trust_ai"}:
        action = "adjust_affinity"
    elif action in {"end", "end_turn", "stop"}:
        action = "pass"
        payload["reason"] = payload.get("reason") or "结束本次活动"
    return {"action": action, "target": target, "payload": payload, "raw": raw}


def _ask_action(gw: Gateway, agent_cfg: dict, me: dict, public: dict,
                session_events: list[dict],
                intel_docs: list[dict] | None = None,
                turn_state: dict | None = None) -> dict:
    out = gw.chat(agent_cfg["model"], _system_prompt(agent_cfg),
                  json.dumps(_agent_context(me, public, session_events,
                                            intel_docs, turn_state),
                             ensure_ascii=False),
                  agent=agent_cfg["id"])
    return _normalize_action(_parse_json(out["text"]))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_profit_share(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("％", "%")
        if text.endswith("%"):
            n = _safe_float(text[:-1].strip())
            return None if n is None else n / 100
        value = text
    n = _safe_float(value)
    if n is None:
        return None
    if n > 1:
        n = n / 100
    return n


def _clip_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _latest_report_no() -> int | None:
    reports = db.load_reports()
    return reports[-1]["no"] if reports else None


def _report_exists(report_no: int) -> bool:
    return any(r["no"] == report_no for r in db.load_reports())


def _result(agent_cfg: dict, action: str, status: str, message: str,
            created_refs: dict | None = None) -> dict:
    return {
        "agent": agent_cfg["id"],
        "agent_name": agent_cfg["name"],
        "action": action,
        "status": status,
        "message": message,
        "created_refs": created_refs or {},
    }


def _record(me: dict, agent_cfg: dict, req: dict, res: dict) -> dict:
    try:
        log_id = db.agent_action_add(
            me["id"], agent_cfg["id"], res["action"], res["status"],
            res["message"], req.get("target") or {}, req.get("payload") or {},
            res.get("created_refs") or {}, req.get("raw") or {})
        res["created_refs"] = {**(res.get("created_refs") or {}),
                               "action_log_id": log_id}
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻塞业务动作
        res["message"] += f"；行动日志失败: {str(exc)[:80]}"
    return res


def _execute_read_intel(agent_cfg: dict, me: dict, req: dict) -> tuple[dict, list[dict]]:
    ids = (req["target"].get("intel_ids") or req["payload"].get("intel_ids")
           or req["raw"].get("read_intel") or [])
    if not isinstance(ids, list):
        ids = [ids]
    clean = [i for i in (_safe_int(x) for x in ids) if i is not None][:MAX_INTEL]
    if not clean:
        res = _result(agent_cfg, "read_intel", "rejected", "没有有效情报 id")
        return _record(me, agent_cfg, req, res), []
    docs = db.intel_get(clean)
    found = [d["id"] for d in docs]
    res = _result(agent_cfg, "read_intel", "executed",
                  f"读取情报 {found}" if docs else "未找到对应情报",
                  {"intel_ids": found})
    return _record(me, agent_cfg, req, res), docs


def _execute_place_bet(agent_cfg: dict, me: dict, req: dict,
                       data: dict) -> dict:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    pick = str(req["payload"].get("pick") or "").upper()
    stake = _safe_int(req["payload"].get("stake"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    table = _betting_table(data)
    if match_no is None or match_no not in table:
        return _result(agent_cfg, "place_bet", "rejected", "比赛不存在或不可下注")
    if pick not in ("H", "D", "A"):
        return _result(agent_cfg, "place_bet", "rejected", "pick 必须是 H/D/A")
    if stake is None or stake < MIN_STAKE or stake > MAX_STAKE:
        return _result(agent_cfg, "place_bet", "rejected", "注额不在允许范围")
    fresh = db.get_user(me["id"])
    if not fresh or stake > int(fresh["balance"]):
        return _result(agent_cfg, "place_bet", "rejected", "余额不足")
    if db.agent_bet_count_for_match(me["id"], match_no) >= 2:
        return _result(agent_cfg, "place_bet", "rejected", "本场已下注并加仓过")
    entry = table[match_no]
    if _utc(entry["date_utc"]) <= datetime.now(timezone.utc):
        return _result(agent_cfg, "place_bet", "rejected", "已开球，投注关闭")
    bet = db.place_bet(me["id"], match_no, pick, stake, entry["odds"][pick],
                       reason=reason)
    return _result(agent_cfg, "place_bet", "executed",
                   f"下注#{match_no} {pick} {stake}@{entry['odds'][pick]}",
                   {"bet_id": bet["id"], "match_no": match_no})


def _score_from_req(req: dict) -> tuple[int | None, int | None]:
    home_score = _safe_int(req["payload"].get("home_score")
                           or req["target"].get("home_score"))
    away_score = _safe_int(req["payload"].get("away_score")
                           or req["target"].get("away_score"))
    raw_score = req["payload"].get("score") or req["target"].get("score")
    if (home_score is None or away_score is None) and raw_score:
        text = str(raw_score).strip().replace("：", "-").replace(":", "-")
        parts = text.split("-")
        if len(parts) == 2:
            home_score = _safe_int(parts[0])
            away_score = _safe_int(parts[1])
    return home_score, away_score


def _execute_place_score_bet(agent_cfg: dict, me: dict, req: dict,
                             data: dict) -> dict:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    home_score, away_score = _score_from_req(req)
    stake = _safe_int(req["payload"].get("stake"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    table = _betting_table(data)
    if match_no is None or match_no not in table:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "比赛不存在或不可下注")
    if (home_score is None or away_score is None
            or home_score < 0 or away_score < 0
            or home_score > SCORE_MAX_GOALS or away_score > SCORE_MAX_GOALS):
        return _result(agent_cfg, "place_score_bet", "rejected",
                       f"比分必须在 0-{SCORE_MAX_GOALS}")
    if stake is None or stake < MIN_STAKE or stake > MAX_SCORE_STAKE:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       f"比分注额必须在 {MIN_STAKE}-{MAX_SCORE_STAKE}")
    fresh = db.get_user(me["id"])
    if not fresh or stake > int(fresh["balance"]):
        return _result(agent_cfg, "place_score_bet", "rejected", "余额不足")
    if db.agent_score_bet_count_for_match(me["id"], match_no) >= 1:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "本场已投过比分")
    entry = table[match_no]
    if _utc(entry["date_utc"]) <= datetime.now(timezone.utc):
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "已开球，投注关闭")
    key = f"{home_score}-{away_score}"
    odds_val = (entry.get("score_odds") or {}).get(key)
    if odds_val is None:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "该比分暂不可投注")
    bet = db.place_score_bet(me["id"], match_no, home_score, away_score,
                             stake, odds_val, reason=reason)
    return _result(
        agent_cfg, "place_score_bet", "executed",
        f"比分下注#{match_no} {key} {stake}@{odds_val}",
        {"score_bet_id": bet["id"], "match_no": match_no,
         "score": key, "amount": stake, "odds": odds_val})


def _topic_from_req(req: dict, data: dict) -> tuple[int | None, int | None, str | None]:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    report_no = _safe_int(req["target"].get("report_no")
                          or req["payload"].get("report_no"))
    topic_type = str(req["target"].get("topic_type")
                     or req["payload"].get("topic_type") or "").strip().lower()
    topic_id = _safe_int(req["target"].get("topic_id")
                         or req["payload"].get("topic_id"))
    if not match_no and topic_type == "match":
        match_no = topic_id
    if not report_no and topic_type == "report":
        report_no = topic_id
    if match_no:
        if not _match_row(data, match_no):
            raise ValueError("比赛不存在")
        return None, match_no, f"比赛#{match_no}"
    if report_no:
        if not _report_exists(report_no):
            raise ValueError("战报不存在")
        return report_no, None, f"战报#{report_no}"
    return None, None, "AI讨论"


def _execute_discussion_post(agent_cfg: dict, me: dict, req: dict,
                             data: dict) -> dict:
    text = _clip_text(req["payload"].get("text")
                      or req["payload"].get("content"), COMMENT_MAX)
    if not text:
        return _result(agent_cfg, "write_discussion_post", "rejected", "帖子为空")
    try:
        report_no, match_no, topic_label = _topic_from_req(req, data)
    except ValueError as exc:
        return _result(agent_cfg, "write_discussion_post", "rejected", str(exc))
    post_id = db.agent_post_add(me["id"], report_no, text, match_no=match_no,
                                topic_label=topic_label)
    refs = {"post_id": post_id, "excerpt": text[:60],
            "topic_label": topic_label}
    if match_no:
        refs.update({"match_no": match_no, "topic_type": "match",
                     "topic_id": match_no})
    elif report_no:
        refs.update({"report_no": report_no, "topic_type": "report",
                     "topic_id": report_no})
    else:
        refs.update({"topic_type": "general"})
    return _result(agent_cfg, "write_discussion_post", "executed",
                   f"发布讨论帖 · {topic_label}", refs)


def _execute_reply(agent_cfg: dict, me: dict, req: dict) -> dict:
    reply_to = _safe_int(req["target"].get("reply_to")
                         or req["payload"].get("reply_to"))
    text = _clip_text(req["payload"].get("text"), COMMENT_MAX)
    if reply_to is None:
        return _result(agent_cfg, "reply_comment", "rejected", "缺少回复目标")
    parent = db.agent_post_get(reply_to)
    if not parent:
        return _result(agent_cfg, "reply_comment", "rejected", "回复目标不存在")
    if not text:
        return _result(agent_cfg, "reply_comment", "rejected", "回复为空")
    post_id = db.agent_post_add(
        me["id"], parent.get("report_no"), text, reply_to=reply_to,
        match_no=parent.get("match_no"),
        topic_type=parent.get("topic_type"),
        topic_id=parent.get("topic_id"),
        topic_label=parent.get("topic_label"))
    target = parent.get("topic_label") or (
        f"比赛#{parent['match_no']}" if parent.get("match_no")
        else f"战报#{parent['report_no']}" if parent.get("report_no")
        else "AI讨论")
    return _result(agent_cfg, "reply_comment", "executed",
                   f"回复#{reply_to}({target})",
                   {"post_id": post_id, "reply_to": reply_to,
                    "topic_label": target, "excerpt": text[:60]})


def _execute_notes(agent_cfg: dict, me: dict, req: dict) -> dict:
    payload = req["payload"]
    adds = payload.get("add") or payload.get("notes_add") or req["raw"].get("notes_add") or []
    updates = (payload.get("update") or payload.get("notes_update")
               or req["raw"].get("notes_update") or [])
    deletes = (payload.get("delete") or payload.get("notes_delete")
               or req["raw"].get("notes_delete") or [])
    if not isinstance(adds, list):
        adds = [adds]
    if not isinstance(updates, list):
        updates = [updates]
    if not isinstance(deletes, list):
        deletes = [deletes]
    refs: dict[str, Any] = {"added": [], "updated": [], "deleted": []}
    for item in adds[:MAX_NOTE_OPS]:
        n = _as_dict(item)
        title = _clip_text(n.get("title") or "行动笔记", 80)
        content = _clip_text(n.get("content") or n.get("text"), 1500)
        if content:
            refs["added"].append(db.agent_note_add(me["id"], title, content))
    for item in updates[:MAX_NOTE_OPS]:
        n = _as_dict(item)
        note_id = _safe_int(n.get("id"))
        if note_id and db.agent_note_update(me["id"], note_id,
                                            _clip_text(n.get("title"), 80)
                                            if n.get("title") else None,
                                            _clip_text(n.get("content"), 1500)
                                            if n.get("content") else None):
            refs["updated"].append(note_id)
    for item in deletes[:MAX_NOTE_OPS]:
        note_id = _safe_int(item)
        if note_id and db.agent_note_delete(me["id"], note_id):
            refs["deleted"].append(note_id)
    total = sum(len(v) for v in refs.values())
    status = "executed" if total else "rejected"
    message = f"笔记操作 {total} 项" if total else "没有有效笔记操作"
    return _result(agent_cfg, "manage_notes", status, message, refs)


def _execute_review(agent_cfg: dict, me: dict, req: dict) -> dict:
    summary = _performance_summary(me["id"], limit=50)
    text = _clip_text(req["payload"].get("text") or req["payload"].get("note"),
                      1200)
    if not text:
        text = ("复盘：最近{最近投注数}注，已结{已结算数}注，ROI={ROI}，"
                "净盈亏={净盈亏}，连亏={当前连亏}。下一轮先看赔率区间和方向偏好，"
                "避免为了行动而行动。").format(**summary)
    note_id = db.agent_note_add(me["id"], f"表现复盘 {time.strftime('%m-%d')}",
                                text)
    return _result(agent_cfg, "review_own_performance", "executed",
                   "已写入表现复盘", {"note_id": note_id, "summary": summary})


def _investment_target_login(req: dict) -> str:
    target = req["target"]
    payload = req["payload"]
    raw = req["raw"]
    for key in ("agent_login", "lender_login", "lender", "investor"):
        value = target.get(key) or payload.get(key) or raw.get(key)
        if value:
            return str(value).strip()
    return ""


def _agent_target_login(req: dict) -> str:
    target = req["target"]
    payload = req["payload"]
    raw = req["raw"]
    for key in ("agent_login", "target_agent_login", "target_login"):
        value = target.get(key) or payload.get(key) or raw.get(key)
        if value:
            return str(value).strip()
    return ""


def _execute_request_investment(agent_cfg: dict, me: dict, req: dict) -> dict:
    lender_login = _investment_target_login(req)
    amount = _safe_int(req["payload"].get("amount")
                       or req["target"].get("amount"))
    profit_share = _parse_profit_share(req["payload"].get("profit_share")
                                       or req["target"].get("profit_share"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if not lender_login:
        return _result(agent_cfg, "request_investment", "rejected",
                       "缺少投资方登录")
    if amount is None or amount < MIN_STAKE or amount > MAX_INVESTMENT:
        return _result(agent_cfg, "request_investment", "rejected",
                       f"融资金额必须在 {MIN_STAKE}-{MAX_INVESTMENT}")
    if profit_share is None or profit_share < 0 or profit_share > MAX_PROFIT_SHARE:
        return _result(agent_cfg, "request_investment", "rejected",
                       f"分成比例必须在 0-{MAX_PROFIT_SHARE:g}")
    try:
        offer = db.investment_request_create(
            me["id"], lender_login, amount, profit_share, reason)
    except ValueError as exc:
        return _result(agent_cfg, "request_investment", "rejected", str(exc))
    pct = round(profit_share * 100)
    return _result(agent_cfg, "request_investment", "executed",
                   f"请求 {lender_login} 投资 {amount}，分成 {pct}%",
                   {"offer_id": offer["id"], "lender_login": lender_login,
                    "amount": amount, "profit_share": profit_share})


def _execute_respond_investment(agent_cfg: dict, me: dict, req: dict) -> dict:
    offer_id = _safe_int(req["target"].get("offer_id")
                         or req["payload"].get("offer_id"))
    decision = str(req["payload"].get("decision")
                   or req["target"].get("decision") or "").strip().lower()
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if offer_id is None:
        return _result(agent_cfg, "respond_investment", "rejected",
                       "缺少融资请求 id")
    if not decision:
        return _result(agent_cfg, "respond_investment", "rejected",
                       "缺少 accept/decline 决策")
    try:
        offer = db.investment_respond(offer_id, me["id"], decision, reason)
    except ValueError as exc:
        return _result(agent_cfg, "respond_investment", "rejected", str(exc))
    status = offer["status"]
    message = ("接受融资请求" if status == "active" else "拒绝融资请求")
    return _result(agent_cfg, "respond_investment", "executed",
                   f"{message}#{offer_id}",
                   {"offer_id": offer_id, "investment_status": status,
                    "borrower_id": offer["borrower_id"],
                    "lender_id": offer["lender_id"],
                    "amount": offer["amount"],
                    "profit_share": offer["profit_share"],
                    "principal_remaining": offer["principal_remaining"]})


def _execute_create_funding_invite(agent_cfg: dict, me: dict,
                                   req: dict) -> dict:
    text = _clip_text(req["payload"].get("text")
                      or req["payload"].get("content")
                      or req["payload"].get("pitch")
                      or req["payload"].get("reason"), COMMENT_MAX)
    min_amount = _safe_int(req["payload"].get("min_amount")
                           or req["target"].get("min_amount"))
    max_amount = _safe_int(req["payload"].get("max_amount")
                           or req["target"].get("max_amount"))
    desired_amount = _safe_int(req["payload"].get("desired_amount")
                               or req["target"].get("desired_amount"))
    profit_share = _parse_profit_share(req["payload"].get("profit_share")
                                       or req["target"].get("profit_share"))
    reason = _clip_text(req["payload"].get("reason") or text, 120)
    if not text:
        return _result(agent_cfg, "create_funding_invite", "rejected",
                       "公开注资邀请需要一条讨论区文案")
    try:
        invite = db.funding_invite_create(
            me["id"], text, min_amount=min_amount, max_amount=max_amount,
            desired_amount=desired_amount, profit_share=profit_share,
            reason=reason)
    except ValueError as exc:
        return _result(agent_cfg, "create_funding_invite", "rejected", str(exc))
    pct = round(float(invite["分成"]) * 100)
    return _result(
        agent_cfg, "create_funding_invite", "executed",
        (f"发布公开注资邀请#{invite['id']} "
         f"{invite['最小金额']}-{invite['最大金额']}，分成 {pct}%"),
        {"invite_id": invite["id"], "post_id": invite["帖子"],
         "amount_min": invite["最小金额"], "amount_max": invite["最大金额"],
         "desired_amount": invite["目标金额"],
         "profit_share": invite["分成"], "topic_type": "general",
         "excerpt": text[:60]})


def _execute_accept_funding_invite(agent_cfg: dict, me: dict,
                                   req: dict) -> dict:
    invite_id = _safe_int(req["target"].get("invite_id")
                          or req["payload"].get("invite_id"))
    amount = _safe_int(req["payload"].get("amount")
                       or req["target"].get("amount"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if invite_id is None:
        return _result(agent_cfg, "accept_funding_invite", "rejected",
                       "缺少公开注资邀请 id")
    if amount is None:
        return _result(agent_cfg, "accept_funding_invite", "rejected",
                       "缺少注资金额")
    try:
        row = db.funding_invite_accept(invite_id, me["id"], amount, reason)
    except ValueError as exc:
        return _result(agent_cfg, "accept_funding_invite", "rejected", str(exc))
    inv = row["investment"]
    invite = row["invite"]
    return _result(
        agent_cfg, "accept_funding_invite", "executed",
        f"接受公开注资邀请#{invite_id}，注资 {amount}",
        {"invite_id": invite_id, "offer_id": inv["id"],
         "investment_status": inv["status"],
         "borrower_id": inv["borrower_id"],
         "lender_id": inv["lender_id"],
         "borrower_login": invite["借方登录"],
         "amount": amount, "profit_share": inv["profit_share"],
         "principal_remaining": inv["principal_remaining"]})


def _execute_adjust_affinity(agent_cfg: dict, me: dict, req: dict) -> dict:
    target_login = _agent_target_login(req)
    delta = _safe_int(req["payload"].get("delta")
                      or req["target"].get("delta"))
    reason = _clip_text(req["payload"].get("reason")
                        or req["payload"].get("note"), REASON_MAX)
    if not target_login:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       "缺少目标 AI 登录")
    if delta is None or delta == 0 or abs(delta) > MAX_AFFINITY_DELTA:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       f"delta 必须在 ±{MAX_AFFINITY_DELTA} 内且不能为 0")
    if not reason:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       "需要一句调整理由")
    try:
        row = db.agent_affinity_adjust(me["id"], target_login, delta, reason)
    except ValueError as exc:
        return _result(agent_cfg, "adjust_affinity", "rejected", str(exc))
    return _result(
        agent_cfg, "adjust_affinity", "executed",
        f"对 {row['target_name']} 好感 {row['before']}→{row['after']}",
        {"target_login": row["target_login"], "target_name": row["target_name"],
         "before": row["before"], "after": row["after"],
         "delta": row["delta"]})


def _execute_action(agent_cfg: dict, me: dict, req: dict,
                    data: dict) -> dict:
    action = req["action"]
    if action not in VALID_ACTIONS:
        res = _result(agent_cfg, action or "unknown", "rejected", "未知 action")
    elif (_is_bench_agent(agent_cfg)
          and action in {*PUBLIC_SPEECH_ACTIONS, "accept_funding_invite"}):
        res = _result(agent_cfg, "pass", "passed",
                      "本色组不参与公开发言或公开注资，本轮观望")
    elif action == "read_data":
        res = _result(agent_cfg, action, "observed", "公共数据已在上下文中")
    elif action == "pass":
        reason = _clip_text(req["payload"].get("reason") or "本轮观望", 120)
        res = _result(agent_cfg, action, "passed", reason)
    elif action == "place_bet":
        res = _execute_place_bet(agent_cfg, me, req, data)
    elif action == "place_score_bet":
        res = _execute_place_score_bet(agent_cfg, me, req, data)
    elif action == "write_discussion_post":
        res = _execute_discussion_post(agent_cfg, me, req, data)
    elif action == "reply_comment":
        res = _execute_reply(agent_cfg, me, req)
    elif action == "manage_notes":
        res = _execute_notes(agent_cfg, me, req)
    elif action == "review_own_performance":
        res = _execute_review(agent_cfg, me, req)
    elif action == "request_investment":
        res = _execute_request_investment(agent_cfg, me, req)
    elif action == "respond_investment":
        res = _execute_respond_investment(agent_cfg, me, req)
    elif action == "create_funding_invite":
        res = _execute_create_funding_invite(agent_cfg, me, req)
    elif action == "accept_funding_invite":
        res = _execute_accept_funding_invite(agent_cfg, me, req)
    elif action == "adjust_affinity":
        res = _execute_adjust_affinity(agent_cfg, me, req)
    else:
        res = _result(agent_cfg, action, "rejected", "内部未处理 action")
    return _record(me, agent_cfg, req, res)


def _event_from_result(round_no: int, res: dict,
                       step_no: int | None = None) -> dict:
    event = {
        "轮次": round_no if step_no is None else f"{round_no}.{step_no}",
        "AI": res["agent_name"],
        "动作": res["action"],
        "状态": res["status"],
        "结果": res["message"],
    }
    refs = res.get("created_refs") or {}
    public_refs = {k: v for k, v in refs.items()
                   if k in {"post_id", "reply_to", "match_no", "report_no",
                            "topic_type", "topic_id", "topic_label",
                            "bet_id", "score_bet_id", "score", "odds",
                            "note_id", "intel_ids", "excerpt",
                            "offer_id", "invite_id", "investment_status",
                            "lender_login", "borrower_login",
                            "amount", "amount_min", "amount_max",
                            "desired_amount", "profit_share",
                            "principal_remaining", "target_login",
                            "target_name", "before", "after", "delta"}}
    if public_refs:
        event["关联对象"] = public_refs
    return event


def _turn_limits(agent_cfg: dict, me: dict, max_steps: int,
                 max_public_posts: int, max_bets: int,
                 max_intel_reads: int) -> dict:
    public_limit = max(0, max_public_posts)
    return {
        "max_steps": max(1, max_steps),
        "max_public_posts": public_limit,
        "max_bets": max(0, max_bets),
        "max_intel_reads": max(0, max_intel_reads),
        "max_affinity_adjusts": DEFAULT_MAX_AFFINITY_ADJUSTS_PER_TURN,
    }


def _turn_state_payload(round_no: int, total: int, step_no: int,
                        counts: dict, limits: dict,
                        turn_events: list[dict]) -> dict:
    return {
        "外层轮次": f"{round_no}/{total}",
        "当前步骤": f"{step_no}/{limits['max_steps']}",
        "剩余步骤": max(limits["max_steps"] - step_no, 0),
        "已读情报": counts["intel_ids"],
        "已读情报次数": f"{counts['intel_reads']}/{limits['max_intel_reads']}",
        "已下注次数": f"{counts['bets']}/{limits['max_bets']}",
        "已公开发言次数": (
            f"{counts['public_posts']}/{limits['max_public_posts']}"
        ),
        "已调整亲密度次数": (
            f"{counts.get('affinity_adjusts', 0)}/{limits['max_affinity_adjusts']}"
        ),
        "本轮已执行": turn_events[-6:],
        "上一步结果": turn_events[-1] if turn_events else None,
        "提示": "每一步只输出一个 JSON action；pass 会结束本次活动。",
    }


def _quota_block(agent_cfg: dict, req: dict, counts: dict,
                 limits: dict) -> dict | None:
    action = req["action"]
    if action == "read_intel" and counts["intel_reads"] >= limits["max_intel_reads"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮已读过情报，本次活动结束")
    if action in {"place_bet", "place_score_bet"} and counts["bets"] >= limits["max_bets"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮下注额度已用完，本次活动结束")
    if action in PUBLIC_SPEECH_ACTIONS and counts["public_posts"] >= limits["max_public_posts"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮公开发言额度已用完，本次活动结束")
    if action == "adjust_affinity" and counts.get("affinity_adjusts", 0) >= limits["max_affinity_adjusts"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮亲密度调整额度已用完，本次活动结束")
    return None


def _count_executed_action(req: dict, res: dict, counts: dict) -> None:
    if res["status"] != "executed":
        return
    action = req["action"]
    if action == "read_intel":
        counts["intel_reads"] += 1
        counts["intel_ids"].extend((res.get("created_refs") or {}).get("intel_ids") or [])
    elif action in {"place_bet", "place_score_bet"}:
        counts["bets"] += 1
    elif action in PUBLIC_SPEECH_ACTIONS:
        counts["public_posts"] += 1
    elif action == "adjust_affinity":
        counts["affinity_adjusts"] = counts.get("affinity_adjusts", 0) + 1


def run_round(round_no: int, total: int, agent_cfg: dict, gw: Gateway,
              arena_cfg: dict, public: dict, data: dict,
              session_events: list[dict], max_steps: int,
              max_public_posts: int, max_bets: int,
              max_intel_reads: int) -> list[dict]:
    me = _ensure_agent(agent_cfg, gw)
    budget = arena_cfg.get("daily_token_budget", 200000)
    if _tokens_today(agent_cfg["id"]) > budget:
        req = {"action": "pass", "target": {}, "payload": {},
               "raw": {"reason": "token budget exhausted"}}
        res = _result(agent_cfg, "pass", "skipped", "今日 token 预算已尽")
        return [_record(me, agent_cfg, req, res)]

    limits = _turn_limits(agent_cfg, me, max_steps, max_public_posts,
                          max_bets, max_intel_reads)
    counts = {"intel_reads": 0, "intel_ids": [], "bets": 0,
              "public_posts": 0, "affinity_adjusts": 0}
    intel_docs: list[dict] = []
    turn_events: list[dict] = []
    results: list[dict] = []

    for step_no in range(1, limits["max_steps"] + 1):
        if _tokens_today(agent_cfg["id"]) > budget:
            req = {"action": "pass", "target": {}, "payload": {},
                   "raw": {"reason": "token budget exhausted during turn"}}
            res = _result(agent_cfg, "pass", "skipped",
                          "本次活动中 token 预算已尽")
            res = _record(me, agent_cfg, req, res)
            results.append(res)
            turn_events.append(_event_from_result(round_no, res, step_no))
            break

        me = db.get_user(me["id"]) or me
        state = _turn_state_payload(round_no, total, step_no, counts, limits,
                                    turn_events)
        try:
            req = _ask_action(gw, agent_cfg, me, public, session_events,
                              intel_docs=intel_docs, turn_state=state)
        except Exception as exc:  # noqa: BLE001
            req = {"action": "pass", "target": {}, "payload": {},
                   "raw": {"error": str(exc)[:300]}}
            res = _result(agent_cfg, "pass", "rejected",
                          f"解析或模型调用失败: {str(exc)[:120]}")
            res = _record(me, agent_cfg, req, res)
            results.append(res)
            turn_events.append(_event_from_result(round_no, res, step_no))
            break

        blocked = _quota_block(agent_cfg, req, counts, limits)
        if blocked:
            res = _record(me, agent_cfg, req, blocked)
        elif req["action"] == "read_intel":
            counts["intel_reads"] += 1
            read_res, docs = _execute_read_intel(agent_cfg, me, req)
            res = read_res
            if docs:
                known = {d["id"] for d in intel_docs}
                intel_docs.extend(d for d in docs if d["id"] not in known)
                for d in docs:
                    if d["id"] not in counts["intel_ids"]:
                        counts["intel_ids"].append(d["id"])
        else:
            res = _execute_action(agent_cfg, me, req, data)
            _count_executed_action(req, res, counts)

        results.append(res)
        event = _event_from_result(round_no, res, step_no)
        turn_events.append(event)
        session_events.append(event)

        if (res["action"] == "pass"
                or (res["action"] == "request_investment"
                    and res["status"] == "executed")
                or res["status"] in {"rejected", "failed", "skipped"}):
            break
        time.sleep(0.4)

    return results


def run_session(rounds: int | None = None, min_rounds: int = 15,
                max_rounds: int = 30, only: str | None = None,
                seed: int | None = None,
                max_steps: int = DEFAULT_MAX_STEPS,
                max_public_posts: int = DEFAULT_MAX_PUBLIC_POSTS_PER_TURN,
                max_bets: int = DEFAULT_MAX_BETS_PER_TURN,
                max_intel_reads: int = DEFAULT_MAX_INTEL_READS_PER_TURN) -> list[dict]:
    db.init_db()
    data = _load_results()
    arena_cfg = _load_cfg()
    agents = arena_cfg.get("agents", [])
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        agents = [a for a in agents if a.get("id") in wanted]
    if not agents:
        print("  [agent-session] 未配置 agents 或 --only 未命中")
        return []

    agents_by_login = {
        str(a.get("id") or "").strip().lower(): a
        for a in agents
        if a.get("id")
    }
    rng = random.Random(seed)
    total = rounds if rounds is not None else rng.randint(min_rounds, max_rounds)
    if total < 0:
        raise ValueError("rounds 不能为负数")
    gw = Gateway()
    session_events: list[dict] = []
    results = []
    prev = None
    print(f"  [agent-session] 开始 {total} 轮，自主 AI {len(agents)} 位，"
          f"每轮最多 {max(1, max_steps)} 步")
    for i in range(1, total + 1):
        forced_offer = None
        pending_offer = db.investment_pending_oldest()
        if pending_offer:
            lender_login = str(pending_offer.get("投资方登录") or "").strip().lower()
            forced_agent = agents_by_login.get(lender_login)
        else:
            forced_agent = None
        if forced_agent:
            agent = forced_agent
            forced_offer = pending_offer
            if forced_offer:
                print(f"  [agent-session] 融资请求#{forced_offer['id']} "
                      f"等待 {agent['name']} 回应，本轮优先调度")
        else:
            pool = [a for a in agents if a.get("id") != prev] or agents
            agent = rng.choice(pool)
        prev = agent["id"]
        public = _public_context(data)
        try:
            step_results = run_round(i, total, agent, gw, arena_cfg, public,
                                     data, session_events, max_steps,
                                     max_public_posts, max_bets,
                                     max_intel_reads)
            events_recorded = True
        except Exception as exc:  # noqa: BLE001
            step_results = [_result(agent, "pass", "failed", str(exc)[:160])]
            events_recorded = False
        for step_no, res in enumerate(step_results, 1):
            if not events_recorded:
                session_events.append(_event_from_result(i, res, step_no))
            results.append(res)
            print(f"  [轮 {i}/{total} · 步 {step_no}/{len(step_results)}] "
                  f"{res['agent_name']}: {res['action']} · "
                  f"{res['status']} · {res['message']}")
        if forced_offer and db.investment_offer_status(forced_offer["id"]) == "pending":
            try:
                me = _ensure_agent(agent, gw)
                req = {
                    "action": "respond_investment",
                    "target": {"offer_id": forced_offer["id"]},
                    "payload": {
                        "decision": "decline",
                        "reason": "本轮未明确回应，系统视为拒绝",
                    },
                    "raw": {"auto_decline": True},
                }
                auto_res = _record(
                    me, agent, req,
                    _execute_respond_investment(agent, me, req))
                session_events.append(_event_from_result(
                    i, auto_res, len(step_results) + 1))
                results.append(auto_res)
                print(f"  [轮 {i}/{total} · 自动处理] "
                      f"{auto_res['agent_name']}: {auto_res['action']} · "
                      f"{auto_res['status']} · {auto_res['message']}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [agent-session] 融资请求#{forced_offer['id']} "
                      f"自动处理失败: {str(exc)[:120]}")
        time.sleep(1)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="统一 JSON Action Agent 调度器")
    parser.add_argument("--rounds", type=int, default=None,
                        help="固定行动轮数；不填则在 min/max 间随机")
    parser.add_argument("--min-rounds", type=int, default=15)
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--only", help="只运行指定 agent id；多个用逗号分隔")
    parser.add_argument("--dry-run", action="store_true",
                        help="使用临时 DB 副本执行，不写真库；仍会调用模型")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="每个 AI 活动轮内部最多执行几个 action")
    parser.add_argument("--max-public-posts-per-turn", type=int,
                        default=DEFAULT_MAX_PUBLIC_POSTS_PER_TURN,
                        help="每个 AI 活动轮最多公开发言次数")
    parser.add_argument("--max-bets-per-turn", type=int,
                        default=DEFAULT_MAX_BETS_PER_TURN,
                        help="每个 AI 活动轮最多下注次数")
    parser.add_argument("--max-intel-reads-per-turn", type=int,
                        default=DEFAULT_MAX_INTEL_READS_PER_TURN,
                        help="每个 AI 活动轮最多读取情报次数")
    args = parser.parse_args()

    if args.rounds is None and args.min_rounds > args.max_rounds:
        raise SystemExit("--min-rounds 不能大于 --max-rounds")
    if args.max_steps < 1:
        raise SystemExit("--max-steps 至少为 1")
    with isolated_db(args.dry_run):
        if args.dry_run:
            print("  [agent-session] dry-run：写入仅发生在临时 DB，仍会调用模型")
        run_session(args.rounds, args.min_rounds, args.max_rounds,
                    args.only, args.seed, args.max_steps,
                    args.max_public_posts_per_turn, args.max_bets_per_turn,
                    args.max_intel_reads_per_turn)


if __name__ == "__main__":
    main()
