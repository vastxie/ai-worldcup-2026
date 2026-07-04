"""赛程与比分同步：fixturedownload.com 公开 JSON 源 → data/matches.json。

- 自动把英文队名映射为队伍三字码；淘汰赛占位符（"2A"/"3ABCDF"/"To be announced"）
  保留在 slot 字段，等官方确定对阵后 feed 会换成真实队名，下次同步自动识别。
- 网络失败时保留现有 matches.json，不中断流程（手动比分见 src/record.py）。
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED_URL = "https://fixturedownload.com/feed/json/fifa-world-cup-2026"
ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "fifa.world/scoreboard?dates={date}"
)
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
TEAM_CODES = set(NAME_TO_CODE.values())
ESPN_ABBR_TO_CODE = {
    "CGO": "COD", "DRC": "COD",
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


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%SZ", "%Y-%m-%dT%H:%MZ",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _espn_team_code(comp: dict) -> str | None:
    team = comp.get("team") or {}
    abbr = str(team.get("abbreviation") or "").upper()
    if abbr in TEAM_CODES:
        return abbr
    if abbr in ESPN_ABBR_TO_CODE:
        return ESPN_ABBR_TO_CODE[abbr]
    for key in ("displayName", "shortDisplayName", "name", "location"):
        code = NAME_TO_CODE.get(str(team.get(key) or ""))
        if code:
            return code
    return None


def _score_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _goal_minute(detail: dict) -> int | None:
    clock = detail.get("clock") or {}
    display = str(clock.get("displayValue") or "")
    match = re.match(r"\s*(\d+)", display)
    if match:
        return int(match.group(1))
    try:
        value = float(clock.get("value"))
    except (TypeError, ValueError):
        return None
    return int(value // 60)


def _regular_time_score(comp: dict, home_code: str,
                        away_code: str) -> list[int] | None:
    """Derive 90-minute score from ESPN scoring events.

    ESPN's knockout final score can be AET/PEN. The payload does not expose a
    dedicated regular-time score, but each scoring event has a minute and a
    shootout flag. Count non-shootout goals through minute 90, including 90+.
    """
    team_id_to_code = {
        str((c.get("team") or {}).get("id")): _espn_team_code(c)
        for c in comp.get("competitors") or []
    }
    if not team_id_to_code:
        return None
    details = comp.get("details") or []
    if not details:
        return None
    score = {home_code: 0, away_code: 0}
    for detail in details:
        if not detail.get("scoringPlay") or detail.get("shootout"):
            continue
        minute = _goal_minute(detail)
        if minute is None or minute > 90:
            continue
        code = team_id_to_code.get(str((detail.get("team") or {}).get("id")))
        if code not in score:
            continue
        score[code] += _score_int(detail.get("scoreValue")) or 0
    return [score[home_code], score[away_code]]


def _espn_final(event: dict) -> dict | None:
    comp = (event.get("competitions") or [{}])[0]
    status_type = ((comp.get("status") or {}).get("type") or {})
    if not status_type.get("completed"):
        return None
    if status_type.get("state") and status_type.get("state") != "post":
        return None

    by_side = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    home, away = by_side.get("home"), by_side.get("away")
    if not home or not away:
        return None
    home_code, away_code = _espn_team_code(home), _espn_team_code(away)
    gh, ga = _score_int(home.get("score")), _score_int(away.get("score"))
    if not home_code or not away_code or gh is None or ga is None:
        return None

    status_text = " ".join(str(status_type.get(k) or "")
                           for k in ("name", "description", "detail", "shortDetail")).upper()
    if "PEN" in status_text:
        score_type = "penalties"
    elif "AET" in status_text or "EXTRA TIME" in status_text:
        score_type = "final_aet"
    else:
        score_type = "regular"

    winner = None
    if score_type in {"final_aet", "penalties"} or gh == ga:
        for side in (home, away):
            if side.get("winner"):
                winner = _espn_team_code(side)
                break
    settle_score = None
    if score_type in {"final_aet", "penalties"}:
        settle_score = _regular_time_score(comp, home_code, away_code)
    return {
        "home": home_code,
        "away": away_code,
        "score": [gh, ga],
        "settle_score": settle_score,
        "score_type": score_type,
        "winner": winner,
        "date_utc": comp.get("date") or event.get("date"),
    }


def _espn_dates_to_check(matches: list[dict],
                         include_scored_knockouts: bool = False) -> list[str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=72)
    end = now + timedelta(hours=8)
    dates: set[str] = set()
    for m in matches:
        if not (m.get("home") and m.get("away")):
            continue
        has_score = bool(m.get("score"))
        is_scored_knockout = (
            include_scored_knockouts
            and has_score
            and m.get("stage") != "group"
        )
        if has_score and not is_scored_knockout:
            continue
        dt = _parse_utc(m.get("date_utc"))
        in_window = start <= dt <= end if dt else False
        if is_scored_knockout and dt:
            in_window = True
        if dt and in_window:
            dates.add(dt.strftime("%Y%m%d"))
            # ESPN scoreboard uses the event's local broadcast day, so UTC
            # early-morning matches can appear under the previous date.
            dates.add((dt - timedelta(days=1)).strftime("%Y%m%d"))
            dates.add((dt + timedelta(days=1)).strftime("%Y%m%d"))
    return sorted(dates)


def _find_match(matches: list[dict], final: dict) -> dict | None:
    event_dt = _parse_utc(final.get("date_utc"))
    best, best_delta = None, None
    for m in matches:
        if m.get("home") != final["home"] or m.get("away") != final["away"]:
            continue
        match_dt = _parse_utc(m.get("date_utc"))
        if event_dt and match_dt:
            delta = abs((match_dt - event_dt).total_seconds())
            if delta > 12 * 3600:
                continue
        else:
            delta = 0
        if best is None or delta < best_delta:
            best, best_delta = m, delta
    return best


def sync_espn_scores(matches: list[dict] | None = None,
                     quiet: bool = False,
                     include_scored_knockouts: bool = False) -> int:
    """用 ESPN 终场状态补齐比分；只采纳 completed 的最终结果。"""
    from . import db

    matches = matches or db.load_matches()
    dates = _espn_dates_to_check(matches, include_scored_knockouts)
    if not dates:
        return 0

    patches, seen = [], set()
    for date in dates:
        try:
            req = urllib.request.Request(
                ESPN_SCOREBOARD_URL.format(date=date),
                headers={"User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - ESPN 补丁失败不阻断主 feed
            if not quiet:
                print(f"  [fetch] ESPN 补丁失败 {date}（{exc}）")
            continue
        for event in payload.get("events") or []:
            final = _espn_final(event)
            if not final:
                continue
            match = _find_match(matches, final)
            if not match or match["match"] in seen:
                continue
            patched = dict(match)
            patched["score"] = final["score"]
            patched["settle_score"] = final["settle_score"]
            patched["score_type"] = final["score_type"]
            patched["winner"] = final["winner"]
            patches.append(patched)
            seen.add(match["match"])

    if patches:
        db.upsert_matches(patches, source="espn")
        if not quiet:
            nums = ", ".join(str(m["match"]) for m in patches)
            print(f"  [fetch] ESPN 终场比分补丁 {len(patches)} 场（第 {nums} 场）")
    return len(patches)


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
        return bool(sync_espn_scores(
            db.load_matches(), quiet, include_scored_knockouts=True))

    matches = sorted((_to_match(r) for r in feed), key=lambda m: m["match"])
    if len(matches) != 104:
        if not quiet:
            print(f"  [fetch] feed 异常：{len(matches)} 场 ≠ 104，忽略本次同步")
        return bool(sync_espn_scores(
            db.load_matches(), quiet, include_scored_knockouts=True))
    db.upsert_matches(matches, source="feed")
    sync_espn_scores(db.load_matches(), quiet, include_scored_knockouts=True)
    played = sum(1 for m in db.load_matches() if m["score"])
    if not quiet:
        print(f"  [fetch] 已同步 104 场赛程，当前 {played} 场有比分")
    return True


def load_matches() -> list[dict]:
    """从数据库读取赛程（手动录入已在库内合并）。"""
    from . import db
    return db.load_matches()


if __name__ == "__main__":
    sync()
