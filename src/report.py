"""AI 战报与单场看点：把每日数据摘要交给大模型生成内容。

- 每期战报 = Codex 主笔正文（180~280 字锐评）+ Claude Code 跟评（40~90 字）。
  生成时机：当天首次更新，或自上期以来有新完赛。存档 data/reports.json，
  并写 web/reports.js（window.WC_REPORTS）供网站展示。
- 单场看点：开球前 36 小时内的比赛各生成一句"AI 怎么看"，按场次锁档
  data/blurbs.json → web/blurbs.js（window.WC_BLURBS）。

LLM 配置放 data/config.json（OpenAI 兼容接口）：
    "llm": {"base_url": "https://.../v1", "api_key": "...",
            "model": "...", "commenter_model": "..."(可选，默认同 model)}
没有配置或调用失败时静默跳过，已有存档照常发布到网站。
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .utils import atomic_write_text

ROOT = Path(__file__).resolve().parent.parent

WRITER_SYSTEM = (
    "你是 Codex，世界杯预测网站的 AI 编辑。根据给定的数据摘要写一段 180~280 字的中文每日战报。"
    "只使用摘要中的事实和数字，绝不编造；语气像懂球的老编辑，有梗但克制；"
    "AI 预测命中要提，翻车更要诚实地提。提到预测一律称'AI'，不要用'模型'这个词。"
    "直接输出正文纯文本，不要标题、不要列表。")
COMMENTER_SYSTEM = (
    "你是 Claude Code，一个冷静、略毒舌但友善的 AI，在战报下面写一条 40~90 字的中文跟评。"
    "可以补充正文忽略的数据点、温和拆台、或提醒读者用概率思维看预测；"
    "不要复述战报内容，不要客套，不要用'主笔''模型'这类词。直接输出跟评纯文本。")
BLURB_SYSTEM = (
    "你是 Codex，世界杯预测网站的 AI 解说。为给定的每场比赛各写一句 40~70 字的中文'AI 怎么看'，"
    "依据各队 Elo、风格开放度、AI 与市场概率，说人话、有观点、不堆数字，"
    "提到预测一律称'AI'，不要用'模型'这个词。"
    '只输出 JSON 对象，键为场次编号字符串，值为该场的一句话，例如 {"5": "..."}')


# ------------------------------------------------------------------ LLM 调用 --

def _llm_config() -> dict | None:
    cfg_path = ROOT / "data" / "config.json"
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")).get("llm")
    if cfg and cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"):
        return cfg
    return None


def _chat(cfg: dict, system: str, user: str, model: str | None = None) -> str:
    body = json.dumps({
        "model": model or cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.8,
        "max_tokens": 1000,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out["choices"][0]["message"]["content"].strip()


# ------------------------------------------------------------------ 数据摘要 --

def _build_digest(payload: dict) -> dict:
    name = {t["code"]: t["name_zh"] for t in payload["teams"]}
    now = datetime.now(timezone.utc)

    recent = []
    for r in payload["record"]["list"][:8]:
        recent.append({
            "对阵": f"{name[r['home']]} {r['score'][0]}-{r['score'][1]} {name[r['away']]}",
            "赛前主胜概率": f"{r['p_home'] * 100:.0f}%",
            "胜负命中": r["outcome_hit"], "比分命中": r["score_hit"],
        })

    upcoming = []
    for m in payload["schedule"]:
        if m["score"] or not m.get("pred") or not (m["home"] and m["away"]):
            continue
        dt = datetime.fromisoformat(m["date_utc"].replace(" ", "T"))
        if timedelta(0) <= dt - now <= timedelta(hours=24):
            p = m["pred"]
            upcoming.append({
                "对阵": f"{name[m['home']]} vs {name[m['away']]}",
                "概率": f"主胜{p['p_home'] * 100:.0f}% 平{p['p_draw'] * 100:.0f}%"
                       f" 客胜{p['p_away'] * 100:.0f}%",
                "最可能比分": "-".join(map(str, p["pred_score"])),
            })

    history = payload.get("history", [])
    movers = []
    if len(history) >= 2:
        prev, cur = history[-2]["champion"], history[-1]["champion"]
        for code in cur:
            d = cur[code] - prev.get(code, 0)
            if abs(d) >= 0.005:
                movers.append(f"{name.get(code, code)} {d * +100:+.1f}pp")

    elo_moves = [f"{t['name_zh']} {t['elo'] - t['elo_base']:+.0f}"
                 for t in payload["teams"]
                 if abs(t["elo"] - t["elo_base"]) >= 3][:8]

    top5 = [{"队": t["name_zh"], "模型": f"{t['p_champion'] * 100:.1f}%",
             "市场": (f"{t['p_champion_market'] * 100:.1f}%"
                      if t.get("p_champion_market") else "无")}
            for t in payload["teams"][:5]]

    return {
        "日期": time.strftime("%Y-%m-%d"),
        "已赛场次": f"{payload['meta']['played']}/104",
        "模型战绩": payload["record"]["stats"],
        "最近赛果与命中": recent,
        "未来24小时比赛": upcoming[:6],
        "夺冠概率异动": movers[:6],
        "Elo涨跌": elo_moves,
        "夺冠榜Top5_模型vs市场": top5,
    }


# ------------------------------------------------------------------ 每日战报 --

def _publish() -> None:
    """把存档同步到网站（无论本次是否新生成）。"""
    reports = db.load_reports()
    atomic_write_text(
        ROOT / "web" / "reports.js",
        "window.WC_REPORTS = " + json.dumps(reports[-60:], ensure_ascii=False)
        + ";\n")
    blurbs = db.load_blurbs()
    atomic_write_text(
        ROOT / "web" / "blurbs.js",
        "window.WC_BLURBS = " + json.dumps(
            {k: v["text"] for k, v in blurbs.items()}, ensure_ascii=False)
        + ";\n")


def maybe_generate_report(payload: dict) -> None:
    reports = db.load_reports()
    today = time.strftime("%Y-%m-%d")
    played = payload["meta"]["played"]
    last = reports[-1] if reports else None
    if last and last["date"] == today and last["played"] >= played:
        return  # 当天已有且无新完赛
    cfg = _llm_config()
    if not cfg:
        return
    digest = json.dumps(_build_digest(payload), ensure_ascii=False)
    try:
        body = _chat(cfg, WRITER_SYSTEM, digest)
        comment = _chat(cfg, COMMENTER_SYSTEM,
                        f"数据摘要：{digest}\n\n主笔战报：{body}",
                        model=cfg.get("commenter_model"))
        db.save_report({
            "date": today, "time": time.strftime("%H:%M"),
            "played": played, "no": len(reports) + 1,
            "report": body, "comment": comment,
        })
        print(f"  [report] 已生成第 {len(reports) + 1} 期战报")
    except Exception as exc:  # noqa: BLE001
        print(f"  [report] 战报生成失败（{exc}），跳过")


# ------------------------------------------------------------------ 单场看点 --

def maybe_generate_blurbs(payload: dict) -> None:
    cfg = _llm_config()
    if not cfg:
        return
    blurbs = db.load_blurbs()
    name = {t["code"]: t["name_zh"] for t in payload["teams"]}
    by_code = {t["code"]: t for t in payload["teams"]}
    now = datetime.now(timezone.utc)

    todo = {}
    for m in payload["schedule"]:
        if (m["score"] or not m.get("pred") or not (m["home"] and m["away"])
                or str(m["match"]) in blurbs):
            continue
        dt = datetime.fromisoformat(m["date_utc"].replace(" ", "T"))
        if timedelta(0) <= dt - now <= timedelta(hours=36):
            p, h, a = m["pred"], by_code[m["home"]], by_code[m["away"]]
            todo[str(m["match"])] = {
                "对阵": f"{name[m['home']]} vs {name[m['away']]}",
                "Elo": f"{h['elo']:.0f} vs {a['elo']:.0f}",
                "模型概率": f"主胜{p['p_home'] * 100:.0f}% 平{p['p_draw'] * 100:.0f}%"
                          f" 客胜{p['p_away'] * 100:.0f}%，最可能 "
                          + "-".join(map(str, p["pred_score"])),
                "市场": (f"主胜{p['market']['p_home'] * 100:.0f}%"
                        f" 客胜{p['market']['p_away'] * 100:.0f}%"
                        if p.get("market") else "无盘口"),
            }
    if not todo:
        return
    try:
        raw = _chat(cfg, BLURB_SYSTEM, json.dumps(todo, ensure_ascii=False))
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        out = json.loads(raw)
        for k, text in out.items():
            if k in todo and isinstance(text, str) and text.strip():
                db.save_blurb(int(k), text.strip())
        print(f"  [report] 已生成 {len(out)} 条单场看点")
    except Exception as exc:  # noqa: BLE001
        print(f"  [report] 看点生成失败（{exc}），跳过")


def update_all(payload: dict) -> None:
    maybe_generate_report(payload)
    maybe_generate_blurbs(payload)
    _publish()
