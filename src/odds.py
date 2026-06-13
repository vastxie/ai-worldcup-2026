"""博彩市场赔率：抓取 → 去水 → 多庄家取中位数 → 本地缓存。

数据源 the-odds-api.com（免费档每月 500 credits）：
- soccer_fifa_world_cup        逐场胜平负（h2h），每次更新抓一次（1 credit）
- soccer_fifa_world_cup_winner 夺冠赔率（outrights），每天最多抓一次（1 credit）
按服务器每天 7 个更新窗口算，月消耗约 250，免费额度内。

key 来源：环境变量 ODDS_API_KEY 优先，否则 data/config.json 的 odds_api_key。
任何失败（断网/额度耗尽/盘口未开）都静默降级——没有市场数据时纯模型运行。
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.request
from pathlib import Path

from .fetch import NAME_TO_CODE

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "odds.json"
BASE = "https://api.the-odds-api.com/v4/sports"

H2H_MAX_AGE = 90 * 60          # 逐场盘口缓存 90 分钟
WINNER_MAX_AGE = 20 * 3600     # 夺冠盘口缓存 20 小时

# the-odds-api 的队名写法与 fetch.NAME_TO_CODE 的差异补充
EXTRA_ALIASES = {
    "Bosnia & Herzegovina": "BIH",
    "Korea Republic": "KOR",
    "Republic of Korea": "KOR",
}


def _api_key() -> str | None:
    import os
    key = os.environ.get("ODDS_API_KEY")
    if key:
        return key
    cfg = ROOT / "data" / "config.json"
    if cfg.exists():
        return json.loads(cfg.read_text(encoding="utf-8")).get("odds_api_key")
    return None


def _to_code(name: str) -> str | None:
    return EXTRA_ALIASES.get(name) or NAME_TO_CODE.get(name)


def _get(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _devig(prices: dict[str, float]) -> dict[str, float]:
    """赔率倒数归一化，去掉庄家抽水。"""
    inv = {k: 1.0 / v for k, v in prices.items() if v and v > 1.0}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()} if s else {}


def _fetch_h2h(key: str) -> dict:
    """逐场胜平负市场概率：{ "HOME|AWAY": {p_home, p_draw, p_away, books} }"""
    events = _get(f"{BASE}/soccer_fifa_world_cup/odds/"
                  f"?apiKey={key}&regions=eu&markets=h2h&oddsFormat=decimal")
    out = {}
    for ev in events:
        hc, ac = _to_code(ev["home_team"]), _to_code(ev["away_team"])
        if not hc or not ac:
            continue
        ph, pd, pa = [], [], []
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in mk["outcomes"]}
                probs = _devig({"H": prices.get(ev["home_team"], 0),
                                "D": prices.get("Draw", 0),
                                "A": prices.get(ev["away_team"], 0)})
                if len(probs) == 3:
                    ph.append(probs["H"]); pd.append(probs["D"]); pa.append(probs["A"])
        if len(ph) >= 3:  # 至少 3 家庄家才采信
            out[f"{hc}|{ac}"] = {
                "p_home": round(statistics.median(ph), 4),
                "p_draw": round(statistics.median(pd), 4),
                "p_away": round(statistics.median(pa), 4),
                "books": len(ph),
            }
    return out


def _fetch_winner(key: str) -> dict:
    """夺冠市场概率：{code: p}（去水后按庄家取中位数，整体归一）。"""
    events = _get(f"{BASE}/soccer_fifa_world_cup_winner/odds/"
                  f"?apiKey={key}&regions=eu&markets=outrights&oddsFormat=decimal")
    per_team: dict[str, list[float]] = {}
    for ev in events:
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] != "outrights":
                    continue
                probs = _devig({o["name"]: o["price"] for o in mk["outcomes"]})
                for name, p in probs.items():
                    code = _to_code(name)
                    if code:
                        per_team.setdefault(code, []).append(p)
    med = {c: statistics.median(ps) for c, ps in per_team.items()}
    s = sum(med.values())
    return {c: round(p / s, 4) for c, p in med.items()} if s else {}


def load() -> dict | None:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return None


def sync(quiet: bool = False, dry_run: bool = False) -> dict | None:
    """按缓存时效抓取市场数据，返回最新缓存（失败返回旧缓存或 None）。"""
    key = _api_key()
    cache = load() or {}
    if not key:
        return cache or None
    now = time.time()
    try:
        from . import db
        if now - cache.get("h2h_ts", 0) > H2H_MAX_AGE:
            cache["h2h"] = _fetch_h2h(key)
            cache["h2h_ts"] = now
            if not dry_run:
                db.snapshot_odds("h2h", cache["h2h"])
            if not quiet:
                print(f"  [odds] 已更新 {len(cache['h2h'])} 场盘口")
        if now - cache.get("winner_ts", 0) > WINNER_MAX_AGE:
            cache["winner"] = _fetch_winner(key)
            cache["winner_ts"] = now
            if not dry_run:
                db.snapshot_odds("winner", cache["winner"])
            if not quiet:
                print(f"  [odds] 已更新夺冠赔率（{len(cache['winner'])} 队）")
        if not dry_run:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - 市场数据可降级
        if not quiet:
            print(f"  [odds] 抓取失败（{exc}），沿用缓存/纯模型")
    return cache or None


if __name__ == "__main__":
    sync()
