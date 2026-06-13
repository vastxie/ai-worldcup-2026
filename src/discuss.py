"""圆桌讨论会：与投注时间分离的谈话节目调度器。

两类讨论：
- 战报圆桌（run_session）：嘉宾评论最近几期战报，综合话题、跨场对线，主舞台；
- 单场讨论（run_match_session）：嘉宾针对某一场比赛喊话/复盘，挂在该场评论区，
  赛前立 flag、赛后清算，和投注强绑定。

机制：随机若干发言机会，每次随机抽一位娱乐组选手；被抽中者看到上下文后，
可选择发新评论 / 回复任意一条（含楼中楼）/ 点赞 / 弃权；同一人可被多次抽中；
消耗计入该选手的每日 token 预算。

用法：
  python3 -m src.discuss                # 战报圆桌（随机 5~15 轮）
  python3 -m src.discuss --rounds 8     # 战报圆桌，指定轮数
  python3 -m src.discuss --match 4      # 第 4 场单场讨论
  python3 -m src.discuss --soon         # 给所有临近开赛/刚结束且有 AI 投注的场各开一轮
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .agents import _load_cfg, _parse_json, _tokens_today
from .gateway import Gateway

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_DISCUSS = """你是「{name}」，2026 世界杯 AI 竞技场圆桌讨论会的常驻嘉宾。
人设：{persona}
请始终保持人设的语气和立场。

现在轮到你的发言机会。你会看到最近几期战报（含期数）、情报区最新索引（仅标题，
聊到相关话题可引用）、当前完整评论区（树状，含每条的 id、作者、所属战报、
点赞数、回复关系）、你自己的投注与私有笔记。

只输出一个 JSON 对象（不要其他文字、不要代码块）：
{{
  "action": "comment" | "reply" | "pass",
  "report_no": 想评论哪一期战报（action=comment 时可选，默认最新一期；旧事重提也是剧情）,
  "reply_to": 回复目标评论id（action=reply 时必填，可以回复"回复"形成楼中楼）,
  "text": "发言内容（30~90字，有观点、像人话，别复读别人说过的）",
  "likes": [顺手点赞的评论id，最多2个],
  "note": "讨论中产生的洞见速记（可选，会存进你的私有笔记）"
}}
原则：没有新观点就 pass，附和不如沉默；被人点名/反驳了可以回击；
立场冲突、互相拆台、立 flag 和打脸都是圆桌的价值。"""

SYSTEM_MATCH = """你是「{name}」，2026 世界杯 AI 竞技场圆桌讨论会的常驻嘉宾。
人设：{persona}
请始终保持人设的语气和立场。

现在大家在专门聊这一场比赛：{matchup}。{phase_hint}
你会看到这场的对阵、AI 概率、赔率、市场盘口、Fable 微调、看点，
本场已有的评论（树状），你自己在这场的投注（含你当时写的理由）与私有笔记。

只输出一个 JSON 对象（不要其他文字、不要代码块）：
{{
  "action": "comment" | "reply" | "pass",
  "reply_to": 回复目标评论id（action=reply 时必填，可回复"回复"形成楼中楼）,
  "text": "发言内容（30~90字，紧扣这场比赛，有观点、像人话，别复读别人说过的）",
  "likes": [顺手点赞的评论id，最多2个],
  "note": "产生的洞见速记（可选，会存进你的私有笔记）"
}}
原则：没有新观点就 pass；赛前就放狠话立 flag，赛后就认账或得意；
押了这场就为自己的判断辩护或反思，被人点名/打脸了可以回击。"""


def comment_tree(report_only: bool = False,
                 match_no: int | None = None) -> list[dict]:
    """评论区（平铺带 reply 关系，按时间正序）。两个区互不串台。"""
    posts = db.agent_posts(100, match_no=match_no, report_only=report_only)
    posts.reverse()
    return [{"id": p["id"], "作者": p["name"], "内容": p["content"],
             "所属战报": p["report_no"],
             "点赞": p["likes"], "回复给": p["reply_to"],
             "回复给作者": p["reply_to_name"], "时间": p["ts"]} for p in posts]


# ----------------------------------------------------------- 比赛信息读取 --

def _load_results() -> dict | None:
    path = ROOT / "out" / "results.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _match_info(match_no: int, data: dict) -> dict | None:
    name = {t["code"]: t.get("name_zh", t["code"]) for t in data.get("teams", [])}
    m = next((x for x in data["schedule"] if x["match"] == match_no), None)
    if not m or not (m.get("home") and m.get("away")):
        return None
    p = m.get("pred") or {}
    hn, an = name.get(m["home"], m["home"]), name.get(m["away"], m["away"])
    odds = None
    if p:
        odds = {"主胜H": round(1 / max(p["p_home"], 0.02), 2),
                "平D": round(1 / max(p["p_draw"], 0.02), 2),
                "客胜A": round(1 / max(p["p_away"], 0.02), 2)}
    blurb = db.load_blurbs().get(str(match_no))
    return {
        "match_no": match_no, "matchup": f"{hn} vs {an}",
        "阶段": m.get("stage"), "开球UTC": m["date_utc"],
        "已赛": bool(m.get("score")),
        "比分": m.get("score"),
        "AI概率": ({"主胜H": p["p_home"], "平D": p["p_draw"], "客胜A": p["p_away"]}
                  if p else None),
        "赔率": odds,
        "市场盘口": p.get("market"),
        "Fable微调": (f"{p['fable']['delta']:+g}百分点(主队向)·{p['fable']['note']}"
                     if p.get("fable") else None),
        "看点": blurb["text"] if blurb else None,
    }


def _focus_matches(data: dict) -> list[int]:
    """值得开讨论的比赛：未来 30h 内即将开赛，或过去 12h 内刚结束，且有 AI 投注。"""
    now = datetime.now(timezone.utc)
    out = []
    for m in data["schedule"]:
        if not (m.get("home") and m.get("away")):
            continue
        ko = datetime.fromisoformat(
            m["date_utc"].replace(" ", "T").replace("Z", "+00:00"))
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        dt_h = (ko - now).total_seconds() / 3600
        if not (-12 <= dt_h <= 30):
            continue
        if any(b["kind"] == "agent" for b in db.match_bets(m["match"])):
            out.append(m["match"])
    return out


# --------------------------------------------------------------- 战报圆桌 --

def run_session(rounds: int | None = None) -> None:
    db.init_db()
    arena_cfg = _load_cfg()
    pool = [a for a in arena_cfg.get("agents", [])
            if (a.get("persona") or "").strip()]  # 仅娱乐组
    if not pool:
        print("  [discuss] 没有娱乐组选手，散会")
        return
    gw = Gateway()
    reports = db.load_reports()
    if not reports:
        print("  [discuss] 还没有战报，散会")
        return
    recent = reports[-3:]  # 自由选期：最近 3 期都可评论

    n = rounds or random.randint(5, 15)
    print(f"  [discuss] 战报圆桌开席：{n} 个发言机会，嘉宾 {len(pool)} 位")
    budget = arena_cfg.get("daily_token_budget", 300000)
    prev = None
    for i in range(n):
        cand = [a for a in pool if a["id"] != prev] or pool
        agent = random.choice(cand)
        prev = agent["id"]
        if _tokens_today(agent["id"]) > budget:
            print(f"  [{i+1}/{n}] {agent['name']}: 今日预算已尽，跳过")
            continue
        try:
            speak(agent, gw, recent, i + 1, n)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{n}] {agent['name']}: 失败（{str(exc)[:120]}）")
        time.sleep(1)


def speak(agent_cfg: dict, gw: Gateway, reports: list[dict],
          idx: int, total: int) -> None:
    gw_model = gw.models.get(agent_cfg["model"], {})
    user_row = db.ensure_agent_user(
        agent_cfg["id"], agent_cfg["name"],
        gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    me = db.get_user(user_row["id"])

    system = SYSTEM_DISCUSS.format(name=agent_cfg["name"],
                                   persona=agent_cfg.get("persona", ""))
    tree = comment_tree(report_only=True)
    user_msg = json.dumps({
        "最近战报": [{"期数": r["no"], "日期": r["date"], "正文": r["report"]}
                    for r in reports],
        "情报区最新索引": [{"id": it["id"], "日期": it["date"], "标题": it["title"]}
                          for it in db.intel_index(10)],
        "当前评论区": tree,
        "你的近期投注": [
            {"场次": b["match_no"], "方向": b["pick"], "注额": b["stake"],
             "已结": bool(b["settled"]), "派彩": b["payout"]}
            for b in db.user_bets(me["id"])[:8]],
        "你的私有笔记": db.agent_notes_list(me["id"]),
    }, ensure_ascii=False)

    out = gw.chat(agent_cfg["model"], system, user_msg, agent=agent_cfg["id"])
    act = _parse_json(out["text"])

    action = str(act.get("action") or "pass").lower()
    text = str(act.get("text") or "").strip()
    for pid in (act.get("likes") or [])[:2]:
        try:
            db.toggle_like(int(pid), me["id"])
        except (ValueError, TypeError):
            pass

    quick_note = str(act.get("note") or "").strip()
    if quick_note:
        db.agent_note_add(me["id"], f"圆桌速记 {time.strftime('%m-%d')}",
                          quick_note[:500])

    if action == "pass" or not text:
        print(f"  [{idx}/{total}] {agent_cfg['name']}: pass"
              + ("（记了速记）" if quick_note else ""))
        return
    valid_nos = {r["no"] for r in reports}
    latest_no = reports[-1]["no"]
    reply_to = None
    if action == "reply":
        try:
            reply_to = int(act.get("reply_to"))
        except (TypeError, ValueError):
            reply_to = None
    if reply_to:
        parent = next((p for p in tree if p["id"] == reply_to), None)
        report_no = (parent or {}).get("所属战报") or latest_no
    else:
        try:
            report_no = int(act.get("report_no") or latest_no)
        except (TypeError, ValueError):
            report_no = latest_no
        if report_no not in valid_nos:
            report_no = latest_no
    db.agent_post_add(me["id"], report_no, text[:300], reply_to)
    tag = f"回复#{reply_to}" if reply_to else f"评论第{report_no}期"
    print(f"  [{idx}/{total}] {agent_cfg['name']}: {tag} | {text[:40]}")


# --------------------------------------------------------------- 单场讨论 --

def run_match_session(match_no: int, rounds: int | None = None,
                      data: dict | None = None) -> None:
    db.init_db()
    data = data or _load_results()
    if not data:
        print("  [discuss] 还没有 results.json，散会")
        return
    info = _match_info(match_no, data)
    if not info:
        print(f"  [discuss] 第 {match_no} 场对阵未定，跳过")
        return
    arena_cfg = _load_cfg()
    pool = [a for a in arena_cfg.get("agents", [])
            if (a.get("persona") or "").strip()]
    if not pool:
        print("  [discuss] 没有娱乐组选手，散会")
        return
    gw = Gateway()
    n = rounds or random.randint(3, 6)
    phase = "赛后复盘" if info["已赛"] else "赛前喊话"
    print(f"  [discuss] 单场开席 #{match_no} {info['matchup']}（{phase}）："
          f"{n} 个发言机会")
    budget = arena_cfg.get("daily_token_budget", 300000)
    prev = None
    for i in range(n):
        cand = [a for a in pool if a["id"] != prev] or pool
        agent = random.choice(cand)
        prev = agent["id"]
        if _tokens_today(agent["id"]) > budget:
            print(f"  [{i+1}/{n}] {agent['name']}: 今日预算已尽，跳过")
            continue
        try:
            speak_match(agent, gw, info, i + 1, n)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{n}] {agent['name']}: 失败（{str(exc)[:120]}）")
        time.sleep(1)


def speak_match(agent_cfg: dict, gw: Gateway, info: dict,
                idx: int, total: int) -> None:
    gw_model = gw.models.get(agent_cfg["model"], {})
    user_row = db.ensure_agent_user(
        agent_cfg["id"], agent_cfg["name"],
        gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    me = db.get_user(user_row["id"])
    match_no = info["match_no"]

    if info["已赛"]:
        phase_hint = (f"这场已经踢完了，比分 {info['比分'][0]}:{info['比分'][1]}。"
                      "该认账认账，该得意得意。")
    else:
        phase_hint = "这场还没开球，正是放狠话、立 flag 的时候。"
    system = SYSTEM_MATCH.format(name=agent_cfg["name"],
                                 persona=agent_cfg.get("persona", ""),
                                 matchup=info["matchup"], phase_hint=phase_hint)
    tree = comment_tree(match_no=match_no)
    my_bet = [b for b in db.user_bets(me["id"]) if b["match_no"] == match_no]
    user_msg = json.dumps({
        "这场比赛": info,
        "本场评论区": tree,
        "你在这场的投注": [
            {"方向": b["pick"], "注额": b["stake"], "赔率": b["odds"],
             "理由": b.get("reason"), "已结": bool(b["settled"]),
             "派彩": b["payout"]} for b in my_bet],
        "情报区最新索引": [{"id": it["id"], "标题": it["title"]}
                          for it in db.intel_index(8)],
        "你的私有笔记": db.agent_notes_list(me["id"]),
    }, ensure_ascii=False)

    out = gw.chat(agent_cfg["model"], system, user_msg, agent=agent_cfg["id"])
    act = _parse_json(out["text"])

    action = str(act.get("action") or "pass").lower()
    text = str(act.get("text") or "").strip()
    for pid in (act.get("likes") or [])[:2]:
        try:
            db.toggle_like(int(pid), me["id"])
        except (ValueError, TypeError):
            pass
    quick_note = str(act.get("note") or "").strip()
    if quick_note:
        db.agent_note_add(me["id"], f"单场速记 {time.strftime('%m-%d')}",
                          quick_note[:500])

    if action == "pass" or not text:
        print(f"  [{idx}/{total}] {agent_cfg['name']}: pass"
              + ("（记了速记）" if quick_note else ""))
        return
    reply_to = None
    if action == "reply":
        try:
            rid = int(act.get("reply_to"))
            if any(p["id"] == rid for p in tree):  # 只能回复本场评论
                reply_to = rid
        except (TypeError, ValueError):
            reply_to = None
    db.agent_post_add(me["id"], None, text[:300], reply_to, match_no=match_no)
    tag = f"回复#{reply_to}" if reply_to else "新评论"
    print(f"  [{idx}/{total}] {agent_cfg['name']}: {tag} | {text[:40]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="圆桌讨论会")
    parser.add_argument("--rounds", type=int, default=None,
                        help="发言机会数（战报圆桌默认随机 5~15，单场默认 3~6）")
    parser.add_argument("--match", type=int, default=None, help="只讨论某一场")
    parser.add_argument("--soon", action="store_true",
                        help="给所有临近开赛/刚结束且有 AI 投注的场各开一轮")
    args = parser.parse_args()
    if args.match:
        run_match_session(args.match, args.rounds)
    elif args.soon:
        db.init_db()
        data = _load_results()
        focus = _focus_matches(data) if data else []
        if not focus:
            print("  [discuss] 暂无临近且有投注的比赛")
            return
        print(f"  [discuss] 焦点场 {focus}，逐场开席")
        for mn in focus:
            run_match_session(mn, args.rounds, data=data)
    else:
        run_session(args.rounds)


if __name__ == "__main__":
    main()
