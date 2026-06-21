"""高频情报雷达：RSS/公开源 -> 逐篇 GLM 整理 -> intel 入库。

这个入口只处理情报广场，不跑比分更新、战报生成或 AI 讨论。
默认每篇候选单独交给编辑模型判断，输出四类之一：
事实 / 预测 / 市场参考 / 观点；噪声直接跳过。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import db
from .agents import _parse_json
from .gateway import Gateway

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
RUNS_PATH = OUT / "intel_update_runs.jsonl"
SOURCES_PATH = OUT / "intel_sources.jsonl"
LOCK_PATH = OUT / "intel_update.lock"

USER_AGENT = "ai-worldcup-intel/0.1 (+https://wc.lightai.io)"
KINDS = {"事实", "预测", "市场参考", "观点"}
NOISE_KEYWORDS = (
    "how to watch", "tv channel", "live stream", "tickets", "ticket",
    "where to watch", "watch live", "wallchart", "wall chart",
)
SIGNAL_KEYWORDS = (
    "world cup", "fifa", "2026", "injury", "injuries", "injured",
    "squad", "lineup", "line-up", "team news", "press conference",
    "training", "weather", "stadium", "odds", "prediction", "picks",
    "世界杯", "伤病", "首发", "阵容", "发布会", "训练", "天气",
    "场地", "市场参考", "回报系数", "预测",
)
DEFAULT_FEEDS = [
    {
        "id": "bbc-football",
        "name": "BBC Sport Football",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "priority": 90,
    },
    {
        "id": "guardian-football",
        "name": "The Guardian Football",
        "url": "https://www.theguardian.com/football/rss",
        "priority": 86,
    },
    {
        "id": "sky-football",
        "name": "Sky Sports Football",
        "url": "https://www.skysports.com/rss/12040",
        "priority": 76,
    },
    {
        "id": "sportsmole",
        "name": "Sports Mole",
        "url": "https://www.sportsmole.co.uk/rss.xml",
        "priority": 78,
    },
    {
        "id": "rotowire-soccer",
        "name": "RotoWire Soccer",
        "url": "https://www.rotowire.com/rss/news.php?sport=soccer",
        "priority": 88,
    },
    {
        "id": "aljazeera",
        "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "priority": 60,
    },
]

INTEL_EDITOR_SYSTEM = """你是世界杯预测站的情报广场编辑 Agent。
你会收到一篇来自白名单 RSS/公开源的文章候选，以及近期比赛列表。
你的任务不是自由上网，也不是替 AI 提交预测；你只判断文章是否值得入库，并把它整理成公共证据。

只允许四类入库：
- 事实：伤病、停赛、首发、训练、发布会、天气、场地、赛程负荷、已确认事件。
- 预测：媒体、专家或作者对比赛走势/结果/战术的预测。
- 市场参考：回报系数、市场参考、市场倾向或价格变化。
- 观点：有明确来源的战术/心理/球队状态分析和舆论判断。

赛中和赛后内容也可以入库：赛中确认进球、红黄牌、伤退、换人、天气/暂停、明显战术调整；
赛后确认赛果、关键事件、伤停变化、教练/球员赛后说法、战术得失、比赛暴露出的状态问题，
都可作为 AI 后续讨论、提交预测和复盘材料。
噪声必须跳过：转播信息、购票、纯 SEO 前瞻、纯名单聚合、营销、无新增信息、无法判断来源的传闻。
预测/观点/市场参考可以入库，但摘要必须写清“谁认为/市场显示/文章称”，不要写成事实。
如果文章同时包含多个信息点，选择最有价值的一点；每篇最多归属一个 match_no，不确定可填 null。
禁止输出行动建议、胜率调整、直接方向、正向收益结论或“因此看好谁”。这些判断留给各个 AI 自己做。
impact_level 只表示这条信息对赛前/赛后讨论的潜在影响，不表示提交预测方向：
- 高：确认首发、核心伤停/停赛、赛果、红牌、临场突发、明确回报系数大幅变化。
- 中：教练/球员说法、训练状态、旅途体能、战术变化、可信媒体分析。
- 低：舆论背景、泛观点、远期影响、争议但与当前比赛弱相关。

禁止输出推理过程、解释、markdown 或代码块。只输出一个紧凑 JSON 对象，总长度尽量少于 500 个汉字。
JSON 字符串内部不要使用半角双引号；需要引用原话时改用中文引号「」或直接转述：
{
  "decision": "save|skip",
  "kind": "事实|预测|市场参考|观点",
  "match_no": 12,
  "title": "不超过 36 字",
  "summary": "80-220 字，只写事实、来源观点和不确定性",
  "evidence_points": ["1-3个事实点或来源观点，短句"],
  "uncertainty": "还不确定什么；没有就写无明显不确定",
  "impact_level": "高|中|低",
  "impact_axes": ["伤停|首发|体能|战术|士气|赛果|市场参考|市场热度|冷门风险|讨论素材"],
  "entities": ["涉及球队/球员/教练，最多4个"],
  "tags": ["事实", "伤病"],
  "confidence": 0.0,
  "reason": "为什么保存或跳过"
}
"""


@contextmanager
def run_lock(path: Path, stale_minutes: int = 45, break_lock: bool = False):
    OUT.mkdir(exist_ok=True)
    stale_after = max(1, int(stale_minutes or 45)) * 60
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if break_lock or age > stale_after:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            raise SystemExit(
                f"  [intel-update] 上一轮仍在运行，跳过（lock={path}, age={age:.0f}s）")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False).encode("utf-8"))
        os.close(fd)
        fd = -1
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, record: dict) -> None:
    OUT.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load_config() -> dict:
    path = ROOT / "data" / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _intel_cfg(cfg: dict) -> dict:
    return dict((cfg.get("ops") or {}).get("intel_update") or
                cfg.get("intel_update") or {})


def _http_get(url: str, timeout: int = 20, limit: int = 2_000_000) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/rss+xml,application/xml,text/xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit)
        ctype = resp.headers.get("content-type", "")
    return raw.decode("utf-8", "ignore"), ctype


def _strip_html(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _as_list(value: Any, limit: int, item_limit: int = 18) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = re.split(r"[,，、\n]", value)
    else:
        raw = []
    out = []
    for item in raw:
        text = _clip(item, item_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _impact_score(level: str, confidence: float, kind: str,
                  axes: list[str], tags: list[str]) -> float:
    level_base = {"高": 0.82, "中": 0.56, "低": 0.28}
    score = level_base.get(level, 0.45)
    haystack = set(axes + tags + [kind])
    if haystack & {"首发", "伤停", "伤病", "停赛", "赛果", "红牌", "市场参考"}:
        score += 0.08
    if haystack & {"观点", "讨论素材"}:
        score -= 0.05
    score = score * 0.72 + confidence * 0.28
    return round(max(0.05, min(score, 1.0)), 3)


def _similarity(left: str, right: str) -> float:
    left = re.sub(r"\s+", " ", left or "").strip().lower()
    right = re.sub(r"\s+", " ", right or "").strip().lower()
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left[:600], right[:600]).ratio()


def _content_hash(*parts: str) -> str:
    norm = "\n".join(re.sub(r"\s+", " ", str(p or "")).strip().lower()
                     for p in parts)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _parse_feed(raw: str, source: dict) -> list[dict]:
    root = ET.fromstring(raw.encode("utf-8"))
    items: list[dict] = []
    if root.find("./channel") is not None:
        for item in root.findall("./channel/item"):
            items.append({
                "source": source.get("name") or source.get("id") or "RSS",
                "source_id": source.get("id"),
                "source_priority": int(source.get("priority", 50)),
                "title": _clip(item.findtext("title"), 180),
                "url": _clip(item.findtext("link"), 600),
                "summary": _strip_html(item.findtext("description") or ""),
                "published_at": _clip(item.findtext("pubDate"), 80),
            })
        return items

    ns = {"a": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    if root.tag.endswith("feed"):
        for item in root.findall("./a:entry" if ns else "./entry", ns):
            link_el = item.find("a:link" if ns else "link", ns)
            items.append({
                "source": source.get("name") or source.get("id") or "RSS",
                "source_id": source.get("id"),
                "source_priority": int(source.get("priority", 50)),
                "title": _clip(item.findtext("a:title" if ns else "title", "", ns), 180),
                "url": _clip(link_el.get("href") if link_el is not None else "", 600),
                "summary": _strip_html(item.findtext("a:summary" if ns else "summary", "", ns)),
                "published_at": _clip(item.findtext("a:updated" if ns else "updated", "", ns), 80),
            })
    return items


def _published_ts(value: str) -> float:
    if not value:
        return 0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0


def _extract_article(url: str, timeout: int) -> dict:
    try:
        raw, ctype = _http_get(url, timeout=timeout, limit=2_000_000)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": str(exc)[:200], "content": "", "title": ""}
    title = ""
    m = re.search(
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
        raw, re.I)
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = _strip_html(m.group(1))

    body_match = re.search(r"<article\b[^>]*>(.*?)</article>", raw, re.S | re.I)
    body = body_match.group(1) if body_match else raw
    paras = []
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I):
        text = _strip_html(p)
        if len(text) < 40:
            continue
        if re.search(r"cookies|subscribe|sign up|advertisement", text, re.I):
            continue
        paras.append(text)
    return {
        "title": title,
        "content": "\n".join(paras[:30]),
        "content_type": ctype,
        "paragraphs": len(paras),
    }


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _matches(window_hours: int, lookback_hours: int) -> list[dict]:
    path = ROOT / "out" / "results.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {t["code"]: t.get("name_zh") or t.get("name_en") or t["code"]
             for t in data.get("teams", [])}
    teams = {t["code"]: t for t in data.get("teams", [])}
    now = datetime.now(timezone.utc)
    out = []
    for m in data.get("schedule", []):
        if not (m.get("home") and m.get("away")):
            continue
        ko = _parse_utc(m.get("date_utc"))
        if not ko:
            continue
        delta_h = (ko - now).total_seconds() / 3600
        if -lookback_hours <= delta_h <= window_hours:
            home = teams.get(m["home"], {})
            away = teams.get(m["away"], {})
            out.append({
                "match_no": int(m["match"]),
                "date_utc": m.get("date_utc"),
                "home_zh": names.get(m["home"], m["home"]),
                "away_zh": names.get(m["away"], m["away"]),
                "home_en": home.get("name_en") or names.get(m["home"], m["home"]),
                "away_en": away.get("name_en") or names.get(m["away"], m["away"]),
                "label": f"{names.get(m['home'], m['home'])} vs {names.get(m['away'], m['away'])}",
                "hours_to_kickoff": round(delta_h, 1),
            })
    out.sort(key=lambda x: abs(float(x["hours_to_kickoff"])))
    return out[:24]


def _team_terms(matches: list[dict]) -> list[str]:
    terms = []
    for m in matches:
        for key in ("home_zh", "away_zh", "home_en", "away_en"):
            value = str(m.get(key) or "").strip()
            if value and value not in terms:
                terms.append(value)
    return terms


def _candidate_score(item: dict, matches: list[dict]) -> int:
    text = " ".join([item.get("title", ""), item.get("summary", ""),
                     item.get("url", "")]).lower()
    score = int(item.get("source_priority", 50))
    for kw in SIGNAL_KEYWORDS:
        if kw.lower() in text:
            score += 12
    for kw in NOISE_KEYWORDS:
        if kw in text:
            score -= 45
    for term in _team_terms(matches):
        if term.lower() in text:
            score += 24
    if "world cup" in text or "世界杯" in text:
        score += 30
    if "prediction" in text or "odds" in text or "picks" in text:
        score += 8
    return score


def _collect_candidates(cfg: dict, matches: list[dict],
                        max_per_feed: int, timeout: int) -> tuple[list[dict], list[dict]]:
    feeds = cfg.get("feeds") or DEFAULT_FEEDS
    candidates, logs = [], []
    seen = set()
    for source in feeds:
        if not isinstance(source, dict) or source.get("enabled") is False:
            continue
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        started = time.time()
        try:
            raw, ctype = _http_get(url, timeout=timeout, limit=2_000_000)
            items = _parse_feed(raw, source)[:max_per_feed]
            logs.append({"source": source.get("name") or source.get("id"),
                         "url": url, "status": "ok", "items": len(items),
                         "duration_sec": round(time.time() - started, 2)})
        except Exception as exc:  # noqa: BLE001
            logs.append({"source": source.get("name") or source.get("id"),
                         "url": url, "status": "error",
                         "error": str(exc)[:240],
                         "duration_sec": round(time.time() - started, 2)})
            continue
        for item in items:
            link = str(item.get("url") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            item["score"] = _candidate_score(item, matches)
            if item["score"] <= int(cfg.get("min_candidate_score", 85)):
                continue
            candidates.append(item)
    candidates.sort(key=lambda x: (
        int(x.get("score", 0)),
        _published_ts(str(x.get("published_at") or "")),
    ), reverse=True)
    return candidates, logs


def _choose_model(cfg: dict, args: argparse.Namespace) -> str | None:
    return (args.model or os.getenv("AIWC_INTEL_MODEL")
            or os.getenv("OPS_EDITOR_MODEL")
            or cfg.get("editor_model")
            or "glm")


def _heuristic_decision(item: dict) -> dict:
    text = " ".join([item.get("title", ""), item.get("summary", ""),
                     item.get("article_text", "")]).lower()
    if any(x in text for x in NOISE_KEYWORDS):
        return {"decision": "skip", "reason": "明显是转播/购票/低信息内容"}
    if "odds" in text or "picks" in text or "市场参考" in text or "回报系数" in text:
        kind = "市场参考"
    elif "prediction" in text or "predict" in text or "预测" in text:
        kind = "预测"
    elif any(x in text for x in (
        "injury", "injured", "squad", "lineup", "press conference",
        "training", "weather", "stadium", "伤病", "首发", "发布会", "训练")):
        kind = "事实"
    else:
        kind = "观点"
    return {
        "decision": "save",
        "kind": kind,
        "match_no": None,
        "title": _clip(item.get("title"), 36),
        "summary": _clip(item.get("summary") or item.get("article_text"), 180),
        "evidence_points": [_clip(item.get("summary") or item.get("article_text"), 90)],
        "uncertainty": "offline 启发式未做来源细读",
        "impact_level": "中" if kind == "事实" else "低",
        "impact_axes": [kind],
        "entities": [],
        "tags": [kind],
        "confidence": 0.55,
        "reason": "offline 启发式分类，仅用于管线测试",
    }


def _glm_decision(gw: Gateway, model: str, item: dict,
                  matches: list[dict], cfg: dict) -> dict:
    payload = {
        "近期比赛": matches,
        "候选文章": {
            "source": item.get("source"),
            "title": item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "feed_summary": _clip(item.get("summary"), 700),
            "article_title": item.get("article_title"),
            "article_excerpt": _clip(item.get("article_text"), 2200),
        },
    }
    out = gw.chat(
        model,
        INTEL_EDITOR_SYSTEM,
        json.dumps(payload, ensure_ascii=False),
        max_tokens=int(cfg.get("editor_max_tokens", 1800)),
        temperature=float(cfg.get("editor_temperature", 0.15)),
        agent="intel-update",
    )
    try:
        return _parse_json(out["text"])
    except Exception as exc:  # noqa: BLE001
        loose = _parse_loose_json(out.get("text") or "")
        if loose:
            return loose
        raise ValueError(f"{exc}; raw={_clip(out.get('text'), 180)}") from exc


def _parse_loose_json(text: str) -> dict:
    """Best-effort recovery for model JSON with unescaped quotes in strings."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]

    keys = [
        "decision", "kind", "match_no", "title", "summary",
        "evidence_points", "uncertainty", "impact_level", "impact_axes",
        "entities", "tags", "confidence", "reason",
    ]
    out: dict[str, Any] = {}
    for idx, key in enumerate(keys):
        next_keys = "|".join(re.escape(k) for k in keys[idx + 1:])
        if next_keys:
            pat = rf'"{key}"\s*:\s*(.*?)(?=,\s*"({next_keys})"\s*:|\s*}})'
        else:
            pat = rf'"{key}"\s*:\s*(.*?)(?=\s*}})'
        m = re.search(pat, raw, flags=re.S)
        if not m:
            continue
        value = m.group(1).strip().rstrip(",")
        if key in {"match_no"}:
            if value in {"null", "None", '""'}:
                out[key] = None
            else:
                try:
                    out[key] = int(re.sub(r"[^0-9-]", "", value))
                except ValueError:
                    out[key] = None
        elif key == "confidence":
            try:
                out[key] = float(re.search(r"-?\d+(?:\.\d+)?", value).group(0))  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                out[key] = 0.55
        elif key in {"tags", "impact_axes", "entities", "evidence_points"}:
            try:
                parsed = json.loads(value)
                out[key] = parsed if isinstance(parsed, list) else []
            except Exception:  # noqa: BLE001
                out[key] = [
                    x.strip().strip('"').strip("'")
                    for x in re.split(r"[,，]", value.strip("[] "))
                    if x.strip().strip('"').strip("'")
                ]
        else:
            out[key] = value.strip().strip('"').strip("'")
    return out if out.get("decision") else {}


def _normalize_decision(raw: dict, item: dict, payload_matches: list[dict]) -> dict:
    decision = str(raw.get("decision") or "").strip().lower()
    if decision != "save":
        return {"decision": "skip", "reason": _clip(raw.get("reason"), 160)}
    kind = str(raw.get("kind") or "").strip()
    if kind not in KINDS:
        return {"decision": "skip", "reason": f"非法 kind: {kind}"}
    match_no = raw.get("match_no")
    try:
        match_no = int(match_no) if match_no not in (None, "", "null") else None
    except (TypeError, ValueError):
        match_no = None
    valid_matches = {int(m["match_no"]) for m in payload_matches}
    if match_no is not None and match_no not in valid_matches:
        match_no = None
    title = _clip(raw.get("title") or item.get("title"), 36)
    summary = _clip(raw.get("summary"), 260)
    if len(summary) < 30:
        return {"decision": "skip", "reason": "摘要过短"}
    try:
        confidence = float(raw.get("confidence", 0.55))
    except (TypeError, ValueError):
        confidence = 0.55
    confidence = max(0.0, min(confidence, 1.0))
    impact_level = str(raw.get("impact_level") or "").strip()
    if impact_level not in {"高", "中", "低"}:
        impact_level = "中" if kind == "事实" else "低"
    evidence_points = _as_list(raw.get("evidence_points"), 3, 90)
    if not evidence_points:
        evidence_points = [summary[:90]]
    uncertainty = _clip(raw.get("uncertainty") or "未说明", 80)
    impact_axes = _as_list(raw.get("impact_axes"), 5, 12)
    entities = _as_list(raw.get("entities"), 4, 18)
    tags = _as_list(raw.get("tags"), 6, 12)
    tags = [kind, *[t for t in tags if t != kind]]
    if impact_level not in tags:
        tags.append(impact_level)
    score = _impact_score(impact_level, confidence, kind, impact_axes, tags)
    return {
        "decision": "save",
        "kind": kind,
        "match_no": match_no,
        "title": title,
        "summary": summary,
        "evidence_points": evidence_points,
        "uncertainty": uncertainty,
        "impact_level": impact_level,
        "impact_score": score,
        "impact_axes": impact_axes,
        "entities": entities,
        "tags": tags[:6],
        "confidence": confidence,
        "reason": _clip(raw.get("reason"), 160),
    }


def _near_duplicate(dec: dict, threshold: float) -> dict | None:
    match_no = dec.get("match_no")
    if match_no is None:
        return None
    try:
        recent = db.intel_recent_for_match(int(match_no), 20)
    except Exception:  # noqa: BLE001
        return None
    probe = " ".join([
        str(dec.get("kind") or ""),
        str(dec.get("title") or ""),
        str(dec.get("summary") or ""),
    ])
    for row in recent:
        row_kind = str(row.get("kind") or "").strip()
        if row_kind and row_kind != dec.get("kind"):
            continue
        target = " ".join([
            str(row.get("kind") or ""),
            str(row.get("title") or ""),
            str(row.get("content") or ""),
        ])
        ratio = _similarity(probe, target)
        if ratio >= threshold:
            return {"id": row.get("id"), "title": row.get("title"),
                    "similarity": round(ratio, 3)}
    return None


def _save_intel(item: dict, dec: dict, dry_run: bool) -> dict:
    source_url = item["url"]
    evidence = "\n".join(f"- {p}" for p in dec.get("evidence_points") or [])
    content = (
        f"类型：{dec['kind']}\n"
        f"影响：{dec.get('impact_level')}（{dec.get('impact_score')}）\n"
        f"影响维度：{', '.join(dec.get('impact_axes') or []) or '未标注'}\n"
        f"涉及对象：{', '.join(dec.get('entities') or []) or '未标注'}\n"
        f"事实/来源点：\n{evidence}\n"
        f"不确定性：{dec.get('uncertainty') or '未说明'}\n"
        f"摘要：{dec['summary']}\n"
        f"来源：{item.get('source')}\n"
        f"原文：{source_url}\n"
        f"编辑说明：{dec.get('reason') or ''}\n"
        "使用原则：这是公共证据，不是行动建议；各 AI 需要自行判断是否已被市场消化。"
    )
    content_hash = _content_hash(
        str(dec.get("match_no") or ""),
        dec["kind"],
        dec["title"],
        dec["summary"],
    )
    rec = {
        "title": dec["title"],
        "content": content,
        "source": item.get("source") or "RSS",
        "match_no": dec.get("match_no"),
        "source_url": source_url,
        "content_hash": content_hash,
        "tags": dec.get("tags") or [dec["kind"]],
        "confidence": dec.get("confidence"),
        "kind": dec.get("kind"),
        "impact_score": dec.get("impact_score"),
        "impact_level": dec.get("impact_level"),
        "impact_axes": dec.get("impact_axes"),
        "entities": dec.get("entities"),
        "uncertainty": dec.get("uncertainty"),
    }
    if dry_run:
        rec["id"] = None
    else:
        rec["id"] = db.intel_add(**rec)
    return rec


def run(args: argparse.Namespace) -> dict:
    started = time.time()
    cfg_all = _load_config()
    cfg = _intel_cfg(cfg_all)
    db.init_db()
    matches = _matches(
        int(args.window_hours or cfg.get("window_hours", 72)),
        int(args.lookback_hours or cfg.get("lookback_hours", 36)),
    )
    candidates, source_logs = _collect_candidates(
        cfg, matches,
        max_per_feed=int(args.max_per_feed or cfg.get("max_per_feed", 30)),
        timeout=int(args.timeout or cfg.get("timeout", 20)),
    )
    for log in source_logs:
        _append_jsonl(SOURCES_PATH, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "provider": "rss", **log})

    limit = max(1, int(args.limit or cfg.get("max_articles_per_run", 8)))
    model = _choose_model(cfg, args)
    gw = None if args.offline else Gateway()
    saved, skipped, reviewed = [], [], []

    for item in candidates[:limit]:
        url = item["url"]
        if db.intel_exists(source_url=url):
            skipped.append({"url": url, "title": item.get("title"),
                            "reason": "source_url 已入库"})
            continue
        article = _extract_article(url, int(args.timeout or cfg.get("timeout", 20)))
        item["article_title"] = article.get("title") or item.get("title")
        item["article_text"] = article.get("content") or item.get("summary") or ""
        if args.offline:
            raw_decision = _heuristic_decision(item)
        else:
            try:
                raw_decision = _glm_decision(gw, model, item, matches, cfg)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                skipped.append({"url": url, "title": item.get("title"),
                                "reason": f"GLM 失败: {str(exc)[:180]}"})
                continue
        dec = _normalize_decision(raw_decision, item, matches)
        reviewed.append({"url": url, "title": item.get("title"),
                         "decision": dec})
        if dec["decision"] != "save":
            skipped.append({"url": url, "title": item.get("title"),
                            "reason": dec.get("reason")})
            continue
        duplicate = _near_duplicate(
            dec, float(cfg.get("semantic_duplicate_threshold", 0.86)))
        if duplicate:
            skipped.append({"url": url, "title": item.get("title"),
                            "reason": "近重复情报",
                            "duplicate": duplicate})
            continue
        rec = _save_intel(item, dec, args.dry_run)
        saved.append(rec)
        label = "待入库" if args.dry_run else "已入库"
        print(f"  [intel-update] {label} #{rec.get('id') or '-'} "
              f"[{dec['kind']}/{dec.get('impact_level')}] {rec['title']}")

    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
        "duration_sec": round(time.time() - started, 2),
        "dry_run": bool(args.dry_run),
        "offline": bool(args.offline),
        "model": None if args.offline else model,
        "source_count": len(source_logs),
        "candidate_count": len(candidates),
        "reviewed": len(reviewed),
        "saved": len(saved),
        "skipped": len(skipped),
        "saved_samples": [
            {"title": r["title"], "source": r["source"], "match_no": r["match_no"],
             "tags": r["tags"], "confidence": r["confidence"],
             "impact_score": r.get("impact_score"),
             "impact_level": r.get("impact_level"),
             "source_url": r["source_url"]}
            for r in saved[:8]
        ],
        "skip_samples": skipped[:8],
    }
    _append_jsonl(RUNS_PATH, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="世界杯情报广场 RSS/GLM 更新")
    parser.add_argument("--dry-run", action="store_true", help="不写真库")
    parser.add_argument("--offline", action="store_true",
                        help="不用 GLM，仅用启发式分类测试抓取管线")
    parser.add_argument("--model", default=None, help="覆盖编辑模型，例如 glm")
    parser.add_argument("--limit", type=int, default=None,
                        help="本轮最多交给编辑器处理的文章数")
    parser.add_argument("--max-per-feed", type=int, default=None)
    parser.add_argument("--window-hours", type=int, default=None)
    parser.add_argument("--lookback-hours", type=int, default=None,
                        help="纳入已开球/已完赛比赛的回看小时数")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--break-lock", action="store_true")
    parser.add_argument("--lock-stale-minutes", type=int, default=45)
    args = parser.parse_args()

    try:
        with run_lock(LOCK_PATH, args.lock_stale_minutes, args.break_lock):
            record = run(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "failed",
            "error": str(exc)[:500],
            "dry_run": bool(args.dry_run),
            "offline": bool(args.offline),
        }
        _append_jsonl(RUNS_PATH, record)
        raise
    print("  [intel-update] 完成: "
          + json.dumps({k: record[k] for k in (
              "status", "duration_sec", "candidate_count",
              "reviewed", "saved", "skipped")},
              ensure_ascii=False))


if __name__ == "__main__":
    main()
