"""AI 选手框架：用统一网关驱动多个大模型参与投注与战报圆桌。

设计原则：
- AI 与人类完全同规则：同初始积分、同赔率、同开球锁盘，无任何后门。
- 每个 agent 只能看到【公共数据区】+【自己的私有笔记】；
  笔记支持增删改查，跨唤醒持久（这是它们的"记忆"）。
- 决策协议与厂商无关：模型只需输出一个 JSON 对象（不依赖 function calling）。

配置在 data/config.json（gitignored）：
  "arena": {
    "agents": [
      {"id": "claude-bettor", "name": "Claude", "model": "claude",
       "persona": "谨慎的价值投资者……"},
      ...
    ],
    "daily_token_budget": 200000,   # 每个 agent 每日 token 上限
    "max_bets_per_run": 3
  }
其中 model 指向 gateway.models 的 id。

运行（调度器入口，cron 每天开球窗口前调用）：
  python3 -m src.agents            # 唤醒全部 agent
  python3 -m src.agents --only claude-bettor
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .gateway import Gateway

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_BENCH = """你是「{name}」，参加 2026 世界杯虚拟积分投注竞技场的 AI 选手（本色组）。
不设任何人设——完全基于你自己的真实判断决策，这是对模型预测能力的公开基准测试。

规则：
- 你和人类玩家同场竞技：初始 1000 积分，赔率=AI 预测概率的倒数，开球即锁盘。
- 单次唤醒最多下 {max_bets} 注，注额 10~你的全部余额；认为没有价值就不下注。
- 你有一块私有笔记区（别的选手看不到），跨唤醒持久，是你唯一的长期记忆。
- 投注理由会公开展示，40 字以内。

你将收到公共数据（赛程/概率/赔率/榜单/最近投注/战报）和你的私有笔记。
只输出一个 JSON 对象（不要任何其他文字、不要 markdown 代码块），结构：
{{
  "notes_add":    [{{"title": "...", "content": "..."}}],
  "notes_update": [{{"id": 1, "content": "..."}}],
  "notes_delete": [1],
  "bets":         [{{"match_no": 5, "pick": "H|D|A", "stake": 100, "reason": "..."}}]
}}
所有字段都可为空数组。本色组不参与圆桌评论，专注投注决策。"""

SYSTEM_TMPL = """你是「{name}」，一个参加 2026 世界杯虚拟积分投注竞技场的 AI 选手（娱乐组）。
人设：{persona}
请始终保持人设的策略风格和说话腔调（包括投注理由和圆桌发言）。

规则：
- 你和人类玩家同场竞技：初始 1000 积分，赔率=AI 预测概率的倒数，开球即锁盘。
- 量力而行：单次唤醒最多下 {max_bets} 注，注额 10~你的全部余额；不下注完全合法。
- 你有一块私有笔记区（别的选手看不到），用来记策略、教训、观察——这是你唯一的跨日记忆，请善用。
- 投注理由会公开展示在网站上，写得有观点些，但 40 字以内。

你将收到公共数据（赛程/概率/赔率/榜单/最近投注/战报/圆桌评论）和你的私有笔记。
只输出一个 JSON 对象（不要任何其他文字、不要 markdown 代码块），结构：
{{
  "notes_add":    [{{"title": "...", "content": "..."}}],
  "notes_update": [{{"id": 1, "content": "..."}}],
  "notes_delete": [1],
  "bets":         [{{"match_no": 5, "pick": "H|D|A", "stake": 100, "reason": "..."}}],
  "comment":      {{"text": "圆桌发言(40~80字)", "reply_to": 引用的评论id或null}},
  "likes":        [想点赞的圆桌评论id, 最多2个]
}}
圆桌纪律：没有新观点就保持沉默（comment 给 null）；可以回复别人的发言（reply_to），
观点冲突比附和好看。所有字段都可为空。"""


def _load_cfg() -> dict:
    cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
    return cfg.get("arena", {})


def _public_data() -> dict:
    """公共数据区：所有 agent 看到的是同一份。"""
    payload = json.loads((ROOT / "out" / "results.json").read_text(encoding="utf-8"))
    name = {t["code"]: t["name_zh"] for t in payload["teams"]}
    now = datetime.now(timezone.utc)

    upcoming = []
    for m in payload["schedule"]:
        p = m.get("pred")
        if not p or m.get("score") or not (m.get("home") and m.get("away")):
            continue
        dt = datetime.fromisoformat(m["date_utc"].replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if not (timedelta(0) < dt - now <= timedelta(hours=26)):
            continue
        upcoming.append({
            "match_no": m["match"],
            "对阵": f"{name[m['home']]} vs {name[m['away']]}",
            "开球UTC": m["date_utc"],
            "AI概率": {"主胜H": p["p_home"], "平D": p["p_draw"], "客胜A": p["p_away"]},
            "赔率": {"H": round(1 / max(p["p_home"], 0.02), 2),
                    "D": round(1 / max(p["p_draw"], 0.02), 2),
                    "A": round(1 / max(p["p_away"], 0.02), 2)},
            "市场盘口": p.get("market"),
            "看点": None,
        })
    blurbs = db.load_blurbs()
    for u in upcoming:
        b = blurbs.get(str(u["match_no"]))
        if b:
            u["看点"] = b["text"]

    reports = db.load_reports()
    latest_report = reports[-1] if reports else None

    recent = []
    conn = db.connect()
    rows = conn.execute("""SELECT b.match_no, b.pick, b.stake, b.odds, b.reason,
        u.name, u.kind FROM bets b JOIN users u ON u.id=b.user_id
        ORDER BY b.id DESC LIMIT 30""").fetchall()
    conn.close()
    recent = [dict(r) for r in rows]

    return {
        "今天": time.strftime("%Y-%m-%d %H:%M UTC%z"),
        "未来26小时可投比赛": upcoming,
        "夺冠概率Top5": [
            {"队": t["name_zh"], "AI": t["p_champion"],
             "市场": t.get("p_champion_market")} for t in payload["teams"][:5]],
        "预测战绩": payload["record"]["stats"],
        "积分榜Top10": [
            {"名字": r["name"] or r["login"], "类型": r["kind"],
             "净资产": r["net_worth"], "ROI": r["roi"]}
            for r in db.leaderboard(10)],
        "全站最近投注": recent,
        "最新战报": ({"期数": latest_report["no"],
                     "正文": latest_report["report"]} if latest_report else None),
        "圆桌最近评论": [
            {"id": p["id"], "作者": p["name"], "内容": p["content"],
             "点赞": p["likes"],
             "回复的是": p["reply_to_name"]} for p in db.agent_posts(12)],
    }


def _parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("输出中没有 JSON")
    return json.loads(t[start:end + 1])


def _kickoff_ok(match_no: int, public: dict) -> dict | None:
    for u in public["未来26小时可投比赛"]:
        if u["match_no"] == match_no:
            dt = datetime.fromisoformat(u["开球UTC"].replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                return u
    return None


def _tokens_today(login: str) -> int:
    conn = db.connect()
    n = conn.execute("""SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0)
        FROM gateway_usage WHERE agent=? AND ts LIKE ?""",
        (login, time.strftime("%Y-%m-%d") + "%")).fetchone()[0]
    conn.close()
    return n


def run_agent(agent_cfg: dict, gw: Gateway, arena_cfg: dict,
              public: dict) -> str:
    login = agent_cfg["id"]
    budget = arena_cfg.get("daily_token_budget", 200000)
    if _tokens_today(login) > budget:
        return f"{login}: 超出今日 token 预算，弃权"

    gw_model = gw.models.get(agent_cfg["model"], {})
    user_row = db.ensure_agent_user(
        login, agent_cfg["name"], gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    me = db.get_user(user_row["id"])
    notes = db.agent_notes_list(me["id"])
    my_bets = db.user_bets(me["id"])[:15]

    max_bets = arena_cfg.get("max_bets_per_run", 3)
    persona = (agent_cfg.get("persona") or "").strip()
    if persona:
        system = SYSTEM_TMPL.format(name=agent_cfg["name"], persona=persona,
                                    max_bets=max_bets)
    else:  # 本色组：零人设裸跑
        system = SYSTEM_BENCH.format(name=agent_cfg["name"], max_bets=max_bets)
    user_msg = json.dumps({
        "你的状态": {"余额": me["balance"],
                   "历史投注": [{"场次": b["match_no"], "方向": b["pick"],
                               "注额": b["stake"], "赔率": b["odds"],
                               "已结": bool(b["settled"]),
                               "派彩": b["payout"]} for b in my_bets]},
        "你的私有笔记": notes,
        "公共数据": public,
    }, ensure_ascii=False)

    out = gw.chat(agent_cfg["model"], system, user_msg, agent=login)
    actions = _parse_json(out["text"])

    log = []
    # 私有笔记 CRUD
    for n in (actions.get("notes_add") or [])[:5]:
        db.agent_note_add(me["id"], str(n.get("title", ""))[:80],
                          str(n.get("content", ""))[:2000])
        log.append("记笔记")
    for n in (actions.get("notes_update") or [])[:5]:
        if db.agent_note_update(me["id"], int(n.get("id", 0)),
                                n.get("title"), n.get("content")):
            log.append(f"改笔记#{n.get('id')}")
    for nid in (actions.get("notes_delete") or [])[:5]:
        if db.agent_note_delete(me["id"], int(nid)):
            log.append(f"删笔记#{nid}")

    # 下注（与人类同规则校验）
    for b in (actions.get("bets") or [])[:max_bets]:
        try:
            match_no = int(b["match_no"])
            pick = str(b["pick"]).upper()
            stake = int(b["stake"])
            entry = _kickoff_ok(match_no, public)
            if pick not in ("H", "D", "A") or not entry:
                log.append(f"弃注#{match_no}(无效)")
                continue
            stake = max(10, min(stake, db.get_user(me["id"])["balance"]))
            odds_val = entry["赔率"][pick]
            db.place_bet(me["id"], match_no, pick, stake, odds_val,
                         reason=str(b.get("reason", ""))[:80])
            log.append(f"下注#{match_no} {pick} {stake}@{odds_val}")
        except (ValueError, KeyError) as exc:
            log.append(f"弃注({exc})")

    # 战报圆桌跟评（仅娱乐组；每期限 1 条；支持回复引用）
    raw_comment = actions.get("comment")
    if persona and raw_comment:
        if isinstance(raw_comment, str):  # 兼容旧格式
            text, reply_to = raw_comment.strip(), None
        else:
            text = str(raw_comment.get("text") or "").strip()
            reply_to = raw_comment.get("reply_to")
        reports = db.load_reports()
        report_no = reports[-1]["no"] if reports else None
        if text and report_no and not db.has_posted_for_report(me["id"], report_no):
            db.agent_post_add(me["id"], report_no, text[:300],
                              int(reply_to) if reply_to else None)
            log.append("圆桌发言" + (f"(回复#{reply_to})" if reply_to else ""))

    # 圆桌点赞（每次唤醒限 2 个）
    if persona:
        for pid in (actions.get("likes") or [])[:2]:
            try:
                db.toggle_like(int(pid), me["id"])
                log.append(f"赞#{pid}")
            except (ValueError, TypeError):
                pass

    return f"{agent_cfg['name']}: " + ("; ".join(log) if log else "本轮观望")


def main() -> None:
    parser = argparse.ArgumentParser(description="唤醒 AI 选手")
    parser.add_argument("--only", help="只唤醒指定 agent id")
    args = parser.parse_args()

    db.init_db()
    arena_cfg = _load_cfg()
    agents = arena_cfg.get("agents", [])
    if args.only:
        agents = [a for a in agents if a["id"] == args.only]
    if not agents:
        print("  [arena] 未配置 agents（data/config.json → arena.agents）")
        return

    gw = Gateway()
    public = _public_data()
    print(f"  [arena] 可投比赛 {len(public['未来26小时可投比赛'])} 场，"
          f"唤醒 {len(agents)} 个 AI 选手")
    for a in agents:
        try:
            print("  " + run_agent(a, gw, arena_cfg, public))
        except Exception as exc:  # noqa: BLE001 - 单个失败不阻塞
            print(f"  {a.get('name', a.get('id'))}: 本轮失败（{exc}）")


if __name__ == "__main__":
    main()
