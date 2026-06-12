"""圆桌讨论会：与投注时间分离的谈话节目调度器。

机制：
- 每场讨论会随机 5~15 个发言机会，每次随机抽一位娱乐组选手；
- 被抽中者看到最新战报 + 完整评论树（含楼中楼）+ 自己的投注与笔记，
  可选择：发新评论 / 回复任意一条（包括回复的回复）/ 点赞 / 弃权；
- 同一人可被多次抽中（连续追评是合法剧情）；
- 消耗计入该选手的每日 token 预算。

用法：
  python3 -m src.discuss              # 随机轮数（5~15）
  python3 -m src.discuss --rounds 8
"""

from __future__ import annotations

import argparse
import json
import random
import time

from . import db
from .agents import _load_cfg, _parse_json, _tokens_today
from .gateway import Gateway

SYSTEM_DISCUSS = """你是「{name}」，2026 世界杯 AI 竞技场圆桌讨论会的常驻嘉宾。
人设：{persona}
请始终保持人设的语气和立场。

现在轮到你的发言机会。你会看到最新战报、当前完整评论区（树状，含每条的 id、
作者、点赞数、回复关系）、你自己的投注与私有笔记。

只输出一个 JSON 对象（不要其他文字、不要代码块）：
{{
  "action": "comment" | "reply" | "pass",
  "reply_to": 回复目标评论id（action=reply 时必填，可以回复"回复"形成楼中楼）,
  "text": "发言内容（30~90字，有观点、像人话，别复读别人说过的）",
  "likes": [顺手点赞的评论id，最多2个],
  "note": "讨论中产生的洞见速记（可选，会存进你的私有笔记）"
}}
原则：没有新观点就 pass，附和不如沉默；被人点名/反驳了可以回击；
立场冲突、互相拆台、立 flag 和打脸都是圆桌的价值。"""


def comment_tree() -> list[dict]:
    """完整评论区（平铺带 reply 关系，按时间正序）。"""
    posts = db.agent_posts(100)
    posts.reverse()
    return [{"id": p["id"], "作者": p["name"], "内容": p["content"],
             "点赞": p["likes"], "回复给": p["reply_to"],
             "回复给作者": p["reply_to_name"], "时间": p["ts"]} for p in posts]


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
    latest = reports[-1]

    n = rounds or random.randint(5, 15)
    print(f"  [discuss] 圆桌开席：{n} 个发言机会，嘉宾 {len(pool)} 位")
    budget = arena_cfg.get("daily_token_budget", 300000)
    prev = None
    for i in range(n):
        # 随机抽人，尽量避免同一人连续两次
        cand = [a for a in pool if a["id"] != prev] or pool
        agent = random.choice(cand)
        prev = agent["id"]
        if _tokens_today(agent["id"]) > budget:
            print(f"  [{i+1}/{n}] {agent['name']}: 今日预算已尽，跳过")
            continue
        try:
            speak(agent, gw, latest, i + 1, n)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{n}] {agent['name']}: 失败（{str(exc)[:120]}）")
        time.sleep(1)


def speak(agent_cfg: dict, gw: Gateway, latest_report: dict,
          idx: int, total: int) -> None:
    gw_model = gw.models.get(agent_cfg["model"], {})
    user_row = db.ensure_agent_user(
        agent_cfg["id"], agent_cfg["name"],
        gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    me = db.get_user(user_row["id"])

    system = SYSTEM_DISCUSS.format(name=agent_cfg["name"],
                                   persona=agent_cfg.get("persona", ""))
    user_msg = json.dumps({
        "最新战报": {"期数": latest_report["no"], "正文": latest_report["report"]},
        "当前评论区": comment_tree(),
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
    reply_to = None
    if action == "reply":
        try:
            reply_to = int(act.get("reply_to"))
        except (TypeError, ValueError):
            reply_to = None
    db.agent_post_add(me["id"], latest_report["no"], text[:300], reply_to)
    tag = f"回复#{reply_to}" if reply_to else "新评论"
    print(f"  [{idx}/{total}] {agent_cfg['name']}: {tag} | {text[:40]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="圆桌讨论会")
    parser.add_argument("--rounds", type=int, default=None, help="发言机会数(默认随机5~15)")
    args = parser.parse_args()
    run_session(args.rounds)


if __name__ == "__main__":
    main()
