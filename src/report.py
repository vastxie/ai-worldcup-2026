"""AI 战报与单场看点：把每日数据摘要交给大模型生成内容。

- 每期战报 = Codex 主笔正文（180~280 字锐评）+ Claude Code 跟评（40~90 字）。
  生成时机：当天首次更新，或自上期以来有新完赛。存档 data/reports.json，
  并写 web/reports.js（window.WC_REPORTS）供网站展示。
- 单场看点：开球前 36 小时内的比赛各生成一句"AI 怎么看"，按场次锁档
  data/blurbs.json → web/blurbs.js（window.WC_BLURBS）。

LLM 配置统一走 data/config.json 的 gateway（OpenAI-compatible / pi-serve）：
    "gateway": {"base_url": "http://127.0.0.1:8787/v1", "api_key": "...",
                "models": [{"id": "glm", "model": "zai-coding-cn/glm-5.2"}]}
没有配置或调用失败时静默跳过，已有存档照常发布到网站。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .gateway import Gateway
from .utils import atomic_write_text

ROOT = Path(__file__).resolve().parent.parent

WRITER_SYSTEM = (
    "你是 Codex，世界杯预测网站的 AI 编辑。根据给定的数据摘要写一段 180~280 字的中文每日战报。"
    "只使用摘要中的事实和数字，绝不编造；语气像懂球的老编辑，有梗但克制；"
    "AI 预测命中要提，翻车更要诚实地提。提到预测一律称'AI'，不要用'模型'这个词。"
    "公开文案统一写成预测、提交预测、回报系数、市场参考、结算得分、虚拟积分；"
    "保持表达自然，不写解释性免责声明。"
    "直接输出正文纯文本，不要标题、不要列表。")
COMMENTER_SYSTEM = (
    "你是 Claude Code，一个冷静、略毒舌但友善的 AI，在战报下面写一条 40~90 字的中文跟评。"
    "可以补充正文忽略的数据点、温和拆台、或提醒读者用概率思维看预测；"
    "不要复述战报内容，不要客套，不要用'主笔''模型'这类词；"
    "公开文案统一使用预测、提交预测、回报系数、虚拟积分等说法。直接输出跟评纯文本。")
BLURB_SYSTEM = (
    "你是 Codex，世界杯预测网站的 AI 解说。为给定的每场比赛各写一句 40~70 字的中文'AI 怎么看'，"
    "依据各队 Elo、风格开放度、AI 与市场参考概率、已确认情报，说人话、有观点、不堆数字，"
    "提到预测一律称'AI'，不要用'模型'这个词。"
    "如果有新情报，优先把它折成一句克制判断，不要复述新闻。"
    "保持表达自然，不写解释性免责声明。"
    '只输出 JSON 对象，键为场次编号字符串，值为该场的一句话，例如 {"5": "..."}')


def _term(*parts: str) -> str:
    return "".join(parts)


def _unsafe(*codes: int) -> str:
    return "".join(chr(c) for c in codes)


PUBLIC_TEXT_REPLACEMENTS = (
    (_unsafe(0x535a, 0x5f69, 0x76d8, 0x53e3), "公开数据参考"),
    (_unsafe(0x535a, 0x5f69, 0x5e02, 0x573a), "公开市场参考"),
    (_unsafe(0x76d8, 0x53e3), "市场参考"),
    (_unsafe(0x8d54, 0x7387), "回报系数"),
    (_unsafe(0x6295, 0x6ce8), "预测"),
    (_unsafe(0x4e0b, 0x6ce8), "提交预测"),
    (_unsafe(0x6ce8, 0x989d), "投入积分"),
    (_unsafe(0x6d3e, 0x5f69), "结算得分"),
    (_unsafe(0x6ce8, 0x8d44), "积分援助"),
    (_unsafe(0x5229, 0x6da6, 0x5206, 0x6210), "积分分成"),
    (_unsafe(0x5bf9, 0x8d4c), "同场预测"),
    (_unsafe(0x8d4c, 0x7403), "预测球赛"),
    (_unsafe(0x8d4c, 0x5f92), "冲动玩家"),
    (_unsafe(0x62bc, 0x5e73), "选平局"),
    (_unsafe(0x62bc, 0x5ba2, 0x961f), "选客队"),
    (_unsafe(0x62bc, 0x6fb3, 0x6d32), "选澳洲"),
    (_unsafe(0x62bc, 0x7f8e, 0x56fd), "选美国"),
    (_unsafe(0x62bc, 0x4e86, 0x6ce2, 0x9ed1), "选了波黑"),
    (_unsafe(0x62bc, 0x5df4, 0x62c9, 0x572d), "选巴拉圭"),
    (_unsafe(0x6ca1, 0x62bc, 0x4e2d), "没猜中"),
    (_unsafe(0x62bc, 0x4e2d), "猜中"),
    (_unsafe(0x53ea, 0x62bc), "只用"),
    (_unsafe(0x8c6a, 0x6ce8), "重仓预测"),
    (_unsafe(0x5c0f, 0x6ce8), "小分参与"),
    (_unsafe(0x7b2c, 0x4e00, 0x6ce8), "第一次预测"),
    (_unsafe(0x0031, 0x0033, 0x0020, 0x6ce8), "13 次预测"),
    (_unsafe(0x0031, 0x0035, 0x0020, 0x6ce8), "15 次预测"),
    (_unsafe(0x5f00, 0x7403, 0x9501, 0x76d8), "开球锁定"),
    (_unsafe(0x51c0, 0x8d5a), "净增"),
    (_unsafe(0x51c0, 0x8d44, 0x4ea7), "总积分"),
    (_unsafe(0x8fdb, 0x8d26), "增加"),
    (_unsafe(0x5f69, 0x86cb), "细节"),
    (_unsafe(0x62bc, 0x97f5), "呼应"),
    (_unsafe(0x4e24, 0x6ce8), "两次选择"),
    (_unsafe(0x0039, 0x0020, 0x6ce8), "9 次选择"),
    ("ROI", "回报率"),
)


def _public_text(value):
    if isinstance(value, str):
        for old, new in PUBLIC_TEXT_REPLACEMENTS:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_public_text(v) for v in value]
    if isinstance(value, dict):
        return {k: _public_text(v) for k, v in value.items()}
    return value


# ------------------------------------------------------------------ LLM 调用 --


def _load_config() -> dict:
    cfg_path = Path(os.getenv("WORLDCUP_CONFIG", ROOT / "data" / "config.json"))
    if not cfg_path.exists():
        return {}
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _gateway_choice(preferred: str | None = None) -> tuple[Gateway, str] | None:
    try:
        gw = Gateway()
    except Exception:  # noqa: BLE001
        return None
    cfg = _load_config()
    ops = cfg.get("ops") or {}
    candidates = [
        preferred,
        os.environ.get("AIWC_OPS_EDITOR_MODEL"),
        os.environ.get("OPS_EDITOR_MODEL"),
        (ops.get("content") or {}).get("editor_model"),
        ops.get("editor_model"),
        (ops.get("intel_update") or {}).get("editor_model"),
        (ops.get("intel") or {}).get("editor_model"),
        "glm",
        "gpt",
        "claude",
    ]
    for model in candidates:
        if model and model in gw.models:
            return gw, model
    if gw.models:
        first = next(iter(gw.models.keys()))
        return gw, first
    return None


def _content_model(key: str) -> str | None:
    cfg = _load_config()
    ops = cfg.get("ops") or {}
    content = ops.get("content") or {}
    env_key = f"AIWC_REPORT_{key.upper()}_MODEL"
    return (os.environ.get(env_key)
            or content.get(f"{key}_model")
            or content.get("editor_model")
            or ops.get("editor_model"))


def _chat_gateway(system: str, user: str, preferred_model: str | None = None,
                  max_tokens: int | None = None, temperature: float = 0.7,
                  agent: str = "ops-editor") -> str | None:
    choice = _gateway_choice(preferred_model)
    if not choice:
        return None
    gw, model = choice
    out = gw.chat(model, system, user, max_tokens=max_tokens,
                  temperature=temperature, agent=agent)
    return out["text"].strip()


def _chat_blurb(system: str, user: str,
                preferred_model: str | None = None) -> str | None:
    return _chat_gateway(system, user, preferred_model, max_tokens=1400,
                         temperature=0.55, agent="ops-blurb-editor")


def _json_object(raw: str) -> dict:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return json.loads(raw)


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
    reports = _public_text(db.load_reports())
    atomic_write_text(
        ROOT / "web" / "reports.js",
        "window.WC_REPORTS = " + json.dumps(reports[-60:], ensure_ascii=False)
        + ";\n")
    blurbs = _public_text(db.load_blurbs())
    atomic_write_text(
        ROOT / "web" / "blurbs.js",
        "window.WC_BLURBS = " + json.dumps(
            {k: v["text"] for k, v in blurbs.items()}, ensure_ascii=False)
        + ";\n")
    intel = []
    for item in db.intel_recent(400):
        intel.append(_public_text({
            "id": item.get("id"),
            "date": item.get("date"),
            "title": item.get("title"),
            "content": item.get("content"),
            "source": item.get("source"),
            "match_no": item.get("match_no"),
            "source_url": item.get("source_url"),
            "tags": item.get("tags"),
            "confidence": item.get("confidence"),
            "kind": item.get("kind"),
            "impact_score": item.get("impact_score"),
            "impact_level": item.get("impact_level"),
            "impact_axes": item.get("impact_axes"),
            "entities": item.get("entities"),
            "uncertainty": item.get("uncertainty"),
        }))
    atomic_write_text(
        ROOT / "web" / "intel.js",
        "window.WC_INTEL = " + json.dumps(intel, ensure_ascii=False)
        + ";\n")


def maybe_generate_report(payload: dict) -> bool:
    reports = db.load_reports()
    today = time.strftime("%Y-%m-%d")
    played = payload["meta"]["played"]
    last = reports[-1] if reports else None
    if last and last["date"] == today and last["played"] >= played:
        return False  # 当天已有且无新完赛
    digest = json.dumps(_build_digest(payload), ensure_ascii=False)
    try:
        body = _chat_gateway(
            WRITER_SYSTEM, digest, _content_model("writer"),
            max_tokens=1000, temperature=0.8, agent="ops-report-writer")
        if not body:
            return False
        comment = _chat_gateway(
            COMMENTER_SYSTEM, f"数据摘要：{digest}\n\n主笔战报：{body}",
            _content_model("commenter"), max_tokens=500, temperature=0.8,
            agent="ops-report-commenter")
        if not comment:
            return False
        db.save_report({
            "date": today, "time": time.strftime("%H:%M"),
            "played": played, "no": len(reports) + 1,
            "report": body, "comment": comment,
        })
        print(f"  [report] 已生成第 {len(reports) + 1} 期战报")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [report] 战报生成失败（{exc}），跳过")
        return False


# ------------------------------------------------------------------ 单场看点 --

def _intel_by_match(intel_samples: list[dict] | None) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for item in intel_samples or []:
        try:
            match_no = int(item.get("match_no"))
        except (TypeError, ValueError):
            continue
        summary = str(item.get("summary") or item.get("content")
                      or item.get("title") or "").split("\n\n来源")[0]
        out.setdefault(match_no, []).append({
            "标题": item.get("title"),
            "摘要": summary[:220],
            "标签": item.get("tags") or [],
            "来源": item.get("source") or item.get("source_url") or "",
        })
    return out


def maybe_generate_blurbs(payload: dict,
                          intel_samples: list[dict] | None = None,
                          force_match_nos: set[int] | None = None,
                          editor_model: str | None = None) -> int:
    blurbs = db.load_blurbs()
    name = {t["code"]: t["name_zh"] for t in payload["teams"]}
    by_code = {t["code"]: t for t in payload["teams"]}
    now = datetime.now(timezone.utc)
    intel_map = _intel_by_match(intel_samples)
    force_match_nos = force_match_nos or set()

    todo = {}
    for m in payload["schedule"]:
        match_no = int(m["match"])
        if m["score"] or not m.get("pred") or not (m["home"] and m["away"]):
            continue
        if str(match_no) in blurbs and match_no not in force_match_nos:
            continue
        dt = datetime.fromisoformat(m["date_utc"].replace(" ", "T"))
        if timedelta(0) <= dt - now <= timedelta(hours=36):
            p, h, a = m["pred"], by_code[m["home"]], by_code[m["away"]]
            todo[str(match_no)] = {
                "对阵": f"{name[m['home']]} vs {name[m['away']]}",
                "Elo": f"{h['elo']:.0f} vs {a['elo']:.0f}",
                "AI概率": f"主胜{p['p_home'] * 100:.0f}% 平{p['p_draw'] * 100:.0f}%"
                        f" 客胜{p['p_away'] * 100:.0f}%",
                "预测比分": p.get("top_scores") or [
                    "-".join(map(str, p["pred_score"]))],
                "市场参考": (f"主胜{p['market']['p_home'] * 100:.0f}%"
                        f" 客胜{p['market']['p_away'] * 100:.0f}%"
                        if p.get("market") else "无公开参考"),
                "新增情报": intel_map.get(match_no, [])[:3],
            }
    if not todo:
        return 0
    try:
        raw = _chat_blurb(BLURB_SYSTEM, json.dumps(todo, ensure_ascii=False),
                          preferred_model=editor_model)
        if not raw:
            return 0
        out = _json_object(raw)
        saved = 0
        for k, text in out.items():
            if k in todo and isinstance(text, str) and text.strip():
                db.save_blurb(int(k), text.strip())
                saved += 1
        print(f"  [report] 已生成 {saved} 条单场看点")
        return saved
    except Exception as exc:  # noqa: BLE001
        print(f"  [report] 看点生成失败（{exc}），跳过")
        return 0


def update_all(payload: dict) -> dict:
    report_created = maybe_generate_report(payload)
    blurbs_created = maybe_generate_blurbs(payload)
    _publish()
    return {"report_created": report_created,
            "blurbs_created": blurbs_created}
