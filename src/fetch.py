"""赛程与比分同步：fixturedownload.com 公开 JSON 源 → data/matches.json。

- 自动把英文队名映射为队伍三字码；淘汰赛占位符（"2A"/"3ABCDF"/"To be announced"）
  保留在 slot 字段，等官方确定对阵后 feed 会换成真实队名，下次同步自动识别。
- 网络失败时保留现有 matches.json，不中断流程（手动比分见 src/record.py）。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED_URL = "https://fixturedownload.com/feed/json/fifa-world-cup-2026"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# feed 队名 → 三字码（覆盖常见别名写法）
NAME_TO_CODE = {
    "Algeria": "ALG", "Argentina": "ARG", "Australia": "AUS", "Austria": "AUT",
    "Belgium": "BEL", "Bosnia and Herzegovina": "BIH", "Brazil": "BRA",
    "Cabo Verde": "CPV", "Cape Verde": "CPV", "Canada": "CAN",
    "Colombia": "COL", "Congo DR": "COD", "DR Congo": "COD", "Croatia": "CRO",
    "Curaçao": "CUW", "Curacao": "CUW", "Czechia": "CZE", "Czech Republic": "CZE",
    "Côte d'Ivoire": "CIV", "Cote d'Ivoire": "CIV", "Ivory Coast": "CIV",
    "Ecuador": "ECU", "Egypt": "EGY", "England": "ENG", "France": "FRA",
    "Germany": "GER", "Ghana": "GHA", "Haiti": "HAI",
    "IR Iran": "IRN", "Iran": "IRN", "Iraq": "IRQ", "Japan": "JPN",
    "Jordan": "JOR", "Korea Republic": "KOR", "South Korea": "KOR",
    "Mexico": "MEX", "Morocco": "MAR", "Netherlands": "NED",
    "New Zealand": "NZL", "Norway": "NOR", "Panama": "PAN", "Paraguay": "PAR",
    "Portugal": "POR", "Qatar": "QAT", "Saudi Arabia": "KSA",
    "Scotland": "SCO", "Senegal": "SEN", "South Africa": "RSA",
    "Spain": "ESP", "Sweden": "SWE", "Switzerland": "SUI", "Tunisia": "TUN",
    "Türkiye": "TUR", "Turkiye": "TUR", "Turkey": "TUR",
    "USA": "USA", "United States": "USA", "Uruguay": "URU", "Uzbekistan": "UZB",
}

STAGE_BY_ROUND = {1: "group", 2: "group", 3: "group",
                  4: "r32", 5: "r16", 6: "qf", 7: "sf", 8: "final"}


def _to_match(row: dict) -> dict:
    round_no = row["RoundNumber"]
    stage = STAGE_BY_ROUND[round_no]
    if stage == "final" and row["MatchNumber"] == 103:
        stage = "third_place"

    def side(name: str):
        code = NAME_TO_CODE.get(name)
        slot = None if code else (name if name != "To be announced" else None)
        return code, slot

    home_code, home_slot = side(row["HomeTeam"])
    away_code, away_slot = side(row["AwayTeam"])

    hs, as_ = row.get("HomeTeamScore"), row.get("AwayTeamScore")
    score = [hs, as_] if hs is not None and as_ is not None else None
    winner = NAME_TO_CODE.get(row.get("Winner") or "", None)

    return {
        "match": row["MatchNumber"],
        "round": round_no,
        "stage": stage,
        "group": (row.get("Group") or "").replace("Group ", "") or None,
        "date_utc": row["DateUtc"],
        "venue": row["Location"],
        "home": home_code, "away": away_code,
        "slot_home": home_slot, "slot_away": away_slot,
        "score": score,
        "winner": winner,  # 仅在点球分胜负等需要时由 feed/手动提供
    }


def sync(quiet: bool = False) -> bool:
    """拉取 feed 并写入数据库（手动录入的比分不会被覆盖）。"""
    from . import db
    try:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            feed = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 网络问题一律降级
        if not quiet:
            print(f"  [fetch] 同步失败（{exc}），沿用数据库现有赛程")
        return False

    matches = sorted((_to_match(r) for r in feed), key=lambda m: m["match"])
    if len(matches) != 104:
        if not quiet:
            print(f"  [fetch] feed 异常：{len(matches)} 场 ≠ 104，忽略本次同步")
        return False
    db.upsert_matches(matches, source="feed")
    played = sum(1 for m in matches if m["score"])
    if not quiet:
        print(f"  [fetch] 已同步 104 场赛程，其中 {played} 场有比分")
    return True


def load_matches() -> list[dict]:
    """从数据库读取赛程（手动录入已在库内合并）。"""
    from . import db
    return db.load_matches()


if __name__ == "__main__":
    sync()
