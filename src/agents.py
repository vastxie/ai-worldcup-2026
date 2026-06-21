"""AI 选手框架：用统一网关驱动多个大模型参与预测与战报圆桌。

设计原则：
- AI 与人类完全同规则：同初始积分、同回报系数、同开球锁盘，无任何后门。
- 每个 agent 只能看到【公共数据区】+【自己的私有笔记】；
  笔记支持增删改查，跨唤醒持久（这是它们的"记忆"）。
- 决策协议与厂商无关：模型只需输出一个 JSON 对象（不依赖 function calling）。

配置在 data/config.json（gitignored）：
  "arena": {
    "agents": [
      {"id": "claude-bettor", "name": "Claude", "model": "claude",
       "persona": "谨慎的价值支持者……"},
      ...
    ],
    "daily_token_budget": 200000,   # 每个 agent 每日 token 上限
    "max_bets_per_run": 3
  }
其中 model 指向 gateway.models 的 id。

运行（兼容入口；统一自主行动优先用 python3 -m src.agent_session）：
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

SYSTEM_BENCH = """你是「{name}」，参加 2026 世界杯虚拟积分预测竞技场的 AI 选手（本色组）。
不设任何人设——完全基于你自己的真实判断决策，这是对模型预测能力的公开基准测试。

规则：
- 你和人类玩家同场竞技：初始 1000 积分，回报系数=AI 预测概率的倒数，开球即锁盘。
- 单次唤醒最多下 {max_bets} 注，投入积分 10~你的全部余额；认为没有价值就不提交预测。
- 你有一块私有笔记区（别的选手看不到），跨唤醒持久，是你唯一的长期记忆。
- 预测理由会公开展示，40 字以内。

你将收到公共数据（赛程/概率/回报系数/赛果/榜单/最近预测/战报/情报区索引）、
你的结算反馈和私有笔记。
只输出一个 JSON 对象（不要任何其他文字、不要 markdown 代码块），结构：
{{
  "read_intel":   [想读全文的情报id, 最多3个]（可选：申请后系统会把全文发给你再让你做最终决策）,
  "notes_add":    [{{"title": "...", "content": "..."}}],
  "notes_update": [{{"id": 1, "content": "..."}}],
  "notes_delete": [1],
  "bets":         [{{"match_no": 5, "pick": "H|D|A", "stake": 100, "reason": "..."}}]
}}
所有字段都可为空数组。本色组不参与圆桌评论，专注预测决策。
笔记请保持精炼（建议 ≤20 条核心结论，过时的及时删改）。"""

SYSTEM_TMPL = """你是「{name}」，一个参加 2026 世界杯虚拟积分预测竞技场的 AI 选手（娱乐组）。
人设：{persona}
请始终保持人设的策略风格和说话腔调（包括预测理由和圆桌发言）。

规则：
- 你和人类玩家同场竞技：初始 1000 积分，回报系数=AI 预测概率的倒数，开球即锁盘。
- 量力而行：单次唤醒最多下 {max_bets} 注，投入积分 10~你的全部余额；不提交预测完全合法。
- 你有一块私有笔记区（别的选手看不到），用来记策略、教训、观察——这是你唯一的跨日记忆，请善用。
- 预测理由会公开展示在网站上，写得有观点些，但 40 字以内。

现在是预测时间（圆桌讨论另有专场）。你将收到公共数据（赛程/概率/回报系数/赛果/
榜单/最近预测/最近3期战报/圆桌评论/情报区索引）、你的结算反馈和私有笔记。
只输出一个 JSON 对象（不要任何其他文字、不要 markdown 代码块），结构：
{{
  "read_intel":   [想读全文的情报id, 最多3个]（可选：申请后系统会把全文发给你再让你做最终决策）,
  "notes_add":    [{{"title": "...", "content": "..."}}],
  "notes_update": [{{"id": 1, "content": "..."}}],
  "notes_delete": [1],
  "bets":         [{{"match_no": 5, "pick": "H|D|A", "stake": 100, "reason": "..."}}]
}}
所有字段都可为空数组。预测理由请保持你的人设腔调。
笔记请保持精炼（建议 ≤20 条核心结论，过时的及时删改）。"""


def _load_cfg() -> dict:
    cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
    return cfg.get("arena", {})


def _public_data() -> dict:
    """公共数据区：所有 agent 看到的是同一份。"""
    payload = json.loads((ROOT / "out" / "results.json").read_text(encoding="utf-8"))
    name = {t["code"]: t["name_zh"] for t in payload["teams"]}
    now = datetime.now(timezone.utc)

    def advisor_note(fable: dict) -> str:
        bits = []
        if fable.get("delta"):
            bits.append(f"主客{fable['delta']:+g}pp")
        if fable.get("draw"):
            bits.append(f"平局{fable['draw']:+g}pp")
        if fable.get("total"):
            bits.append(f"总球{fable['total']:+g}")
        return f"{' / '.join(bits) or '微调'} · {fable.get('note', '')}"

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
            "回报系数": {"H": round(1 / max(p["p_home"], 0.02), 2),
                    "D": round(1 / max(p["p_draw"], 0.02), 2),
                    "A": round(1 / max(p["p_away"], 0.02), 2)},
            "市场市场参考": p.get("market"),
            # Codex 微调透明公示：选手可选择跟随或反向
            "Codex微调": (advisor_note(p["fable"]) if p.get("fable") else None),
            "看点": None,
        })
    blurbs = db.load_blurbs()
    for u in upcoming:
        b = blurbs.get(str(u["match_no"]))
        if b:
            u["看点"] = b["text"]

    reports = db.load_reports()

    recent = []
    conn = db.connect()
    rows = conn.execute("""SELECT b.match_no, b.pick, b.stake, b.odds, b.reason,
        u.name, u.kind FROM bets b JOIN users u ON u.id=b.user_id
        WHERE u.kind='agent'
        ORDER BY b.id DESC LIMIT 30""").fetchall()
    conn.close()
    recent = [dict(r) for r in rows]

    # 最近赛果（结构化：比分 + AI 赛前判断 + 命中情况）——归因分析的原料
    finished = []
    for m in payload["schedule"]:
        if not m.get("score") or not m.get("pred"):
            continue
        finished.append({
            "对阵": f"{name[m['home']]} {m['score'][0]}-{m['score'][1]} {name[m['away']]}",
            "AI赛前": {"看好": m["pred"].get("pick"),
                      "首选比分": m["pred"].get("pred_score"),
                      "主胜率": m["pred"]["p_home"]},
            "胜负命中": m.get("outcome_hit"), "比分命中": m.get("score_hit"),
            "时间": m["date_utc"],
        })
    finished = sorted(finished, key=lambda x: x["时间"], reverse=True)[:8]

    return {
        "今天": time.strftime("%Y-%m-%d %H:%M UTC%z"),
        "未来26小时可投比赛": upcoming,
        "最近赛果": finished,
        "夺冠概率Top5": [
            {"队": t["name_zh"], "AI": t["p_champion"],
             "市场": t.get("p_champion_market")} for t in payload["teams"][:5]],
        "预测战绩": payload["record"]["stats"],
        "积分榜Top10": [
            {"名字": r["name"] or r["login"], "类型": r["kind"],
             "净资产": r["net_worth"], "ROI": r["roi"]}
            for r in db.leaderboard(10)],
        "全站最近预测": recent,
        "最近3期战报": [{"期数": r["no"], "日期": r["date"], "正文": r["report"]}
                      for r in reports[-3:]],
        "圆桌最近评论": [
            {"id": p["id"], "作者": p["name"], "内容": p["content"],
             "点赞": p["likes"],
             "回复的是": p["reply_to_name"]} for p in db.agent_posts(12)],
        "情报区索引": db.intel_index(10),
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

    # 结算反馈：把上次唤醒以来的输赢摆到它脸上（reward 信号）
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%d %H:%M")
    recent_settled = [b for b in my_bets if b["settled"]
                      and (b["settled_at"] or "") >= cutoff]
    pnl = sum(b["payout"] - b["stake"] for b in recent_settled)
    settlement = {
        "明细": [{"场次": b["match_no"], "方向": b["pick"], "投入积分": b["stake"],
                "回报系数": b["odds"], "结果": "赢" if b["payout"] else "输",
                "净盈亏": b["payout"] - b["stake"]} for b in recent_settled],
        "本期净盈亏": pnl,
    } if recent_settled else "（无新结算）"

    max_bets = arena_cfg.get("max_bets_per_run", 3)
    persona = (agent_cfg.get("persona") or "").strip()
    if persona:
        system = SYSTEM_TMPL.format(name=agent_cfg["name"], persona=persona,
                                    max_bets=max_bets)
    else:  # 本色组：零人设裸跑
        system = SYSTEM_BENCH.format(name=agent_cfg["name"], max_bets=max_bets)

    ctx = {
        "你的状态": {"余额": me["balance"],
                   "历史预测": [{"场次": b["match_no"], "方向": b["pick"],
                               "投入积分": b["stake"], "回报系数": b["odds"],
                               "已结": bool(b["settled"]),
                               "结算得分": b["payout"]} for b in my_bets]},
        "结算反馈": settlement,
        "你的私有笔记": notes,
        "公共数据": public,
    }
    if len(notes) > 30:
        ctx["系统提示"] = (f"你的笔记已有 {len(notes)} 条，严重过载。"
                         "本次请先精简：合并同类、删除过时，目标 20 条以内。")
    user_msg = json.dumps(ctx, ensure_ascii=False)

    out = gw.chat(agent_cfg["model"], system, user_msg, agent=login)
    actions = _parse_json(out["text"])

    log = []
    # 情报两阶段：申请全文 → 喂回 → 最终决策（最多一轮追问）
    intel_ids = []
    for i in (actions.get("read_intel") or [])[:3]:
        try:
            intel_ids.append(int(i))
        except (TypeError, ValueError):
            pass
    if intel_ids:
        docs = db.intel_get(intel_ids)
        if docs:
            followup = (user_msg + "\n\n【你申请的情报全文】\n"
                        + json.dumps([{"id": d["id"], "标题": d["title"],
                                       "正文": d["content"], "来源": d["source"]}
                                      for d in docs], ensure_ascii=False)
                        + "\n\n请基于全部信息输出最终决策 JSON（不要再申请情报）。")
            out = gw.chat(agent_cfg["model"], system, followup, agent=login)
            actions = _parse_json(out["text"])
            log.append(f"读情报{intel_ids}")
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

    # 提交预测（与人类同规则校验）
    for b in (actions.get("bets") or [])[:max_bets]:
        try:
            match_no = int(b["match_no"])
            pick = str(b["pick"]).upper()
            stake = int(b["stake"])
            entry = _kickoff_ok(match_no, public)
            if pick not in ("H", "D", "A") or not entry:
                log.append(f"放弃预测#{match_no}(无效)")
                continue
            stake = max(10, min(stake, db.get_user(me["id"])["balance"]))
            odds_val = entry["回报系数"][pick]
            db.place_bet(me["id"], match_no, pick, stake, odds_val,
                         reason=str(b.get("reason", ""))[:80])
            log.append(f"提交预测#{match_no} {pick} {stake}@{odds_val}")
        except (ValueError, KeyError) as exc:
            log.append(f"放弃预测({exc})")

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
