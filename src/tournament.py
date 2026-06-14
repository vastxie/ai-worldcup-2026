"""2026 世界杯赛制模拟：12 组 ×4 队小组赛 → 32 强 → 决赛。

赛制要点（48 队新规）：
- 12 个小组单循环，每组前两名 + 8 个成绩最好的小组第三晋级 32 强。
- 32 强签表为 FIFA 官方固定结构（Match 73~88），其中 8 个名额留给小组第三，
  每个签位限定了允许的来源小组（见 THIRD_PLACE_SLOTS），用回溯法分配。
- 之后按官方对阵树打到决赛（Match 104，纽约/新泽西 MetLife）。

支持「条件模拟」：传入 fixed 后，已踢完的比赛按真实结果固定，
官方已确定的淘汰赛对阵/胜者也直接采用，只对未来掷骰子。
fixed = {
    "group_results": {(home_code, away_code): (gh, ga)},   # 已赛小组赛
    "ko_teams":      {match_no: (home_code, away_code)},   # 已确定的淘汰赛对阵
    "ko_winners":    {match_no: winner_code},              # 已赛淘汰赛胜者
    "we_overrides":  {(home_code, away_code): override},   # 兼容 float we 或完整概率对象
}
"""

from __future__ import annotations

import random
from itertools import combinations

from .model import simulate_group_match, simulate_knockout_match

GROUPS = "ABCDEFGHIJKL"

# 32 强官方签表：值为 (上签, 下签)。
# "1A"/"2A" = A 组第一/第二；"3:ABCDF" = 来自 A/B/C/D/F 之一的小组第三。
ROUND_OF_32 = {
    73: ("2A", "2B"),
    74: ("1E", "3:ABCDF"),
    75: ("1F", "2C"),
    76: ("1C", "2F"),
    77: ("1I", "3:CDFGH"),
    78: ("2E", "2I"),
    79: ("1A", "3:CEFHI"),
    80: ("1L", "3:EHIJK"),
    81: ("1D", "3:BEFIJ"),
    82: ("1G", "3:AEHIJ"),
    83: ("2K", "2L"),
    84: ("1H", "2J"),
    85: ("1B", "3:EFGIJ"),
    86: ("1J", "2H"),
    87: ("1K", "3:DEIJL"),
    88: ("2D", "2G"),
}

ROUND_OF_16 = {
    89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
}
QUARTERS = {97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96)}
SEMIS = {101: (97, 98), 102: (99, 100)}

THIRD_PLACE_SLOTS = {m: set(spec.split(":")[1])
                     for m, (_, spec) in ROUND_OF_32.items()
                     if spec.startswith("3:")}


def _reverse_override(override):
    if not isinstance(override, dict):
        return 1.0 - override if override is not None else None
    out = dict(override)
    ph = override.get("p_home")
    pa = override.get("p_away")
    if ph is not None and pa is not None:
        out["p_home"], out["p_away"] = pa, ph
    if "probs" in override and isinstance(override["probs"], dict):
        probs = dict(override["probs"])
        probs["p_home"], probs["p_away"] = probs.get("p_away"), probs.get("p_home")
        out["probs"] = probs
    if override.get("we") is not None:
        out["we"] = 1.0 - override["we"]
    elif out.get("p_home") is not None and out.get("p_draw") is not None:
        out["we"] = out["p_home"] + 0.5 * out["p_draw"]
    return out


def _we_lookup(overrides: dict | None, team_a: dict, team_b: dict):
    """查找对阵的预测 override（自动处理主客方向）。"""
    if not overrides:
        return None
    key = (team_a["code"], team_b["code"])
    if key in overrides:
        return overrides[key]
    rkey = (team_b["code"], team_a["code"])
    if rkey in overrides:
        return _reverse_override(overrides[rkey])
    return None


def play_group(teams: list[dict], rng: random.Random,
               fixed_results: dict | None = None,
               we_overrides: dict | None = None) -> list[dict]:
    """单循环小组赛（已赛场次用真实比分），返回排序后的 4 队。"""
    fixed_results = fixed_results or {}
    table = {t["code"]: {"team": t, "pts": 0, "gf": 0, "ga": 0} for t in teams}
    for a, b in combinations(teams, 2):
        key, rkey = (a["code"], b["code"]), (b["code"], a["code"])
        if key in fixed_results:
            ga, gb = fixed_results[key]
        elif rkey in fixed_results:
            gb, ga = fixed_results[rkey]
        else:
            ga, gb = simulate_group_match(a, b, rng,
                                          we_override=_we_lookup(we_overrides, a, b))
        ra, rb = table[a["code"]], table[b["code"]]
        ra["gf"] += ga; ra["ga"] += gb
        rb["gf"] += gb; rb["ga"] += ga
        if ga > gb:
            ra["pts"] += 3
        elif gb > ga:
            rb["pts"] += 3
        else:
            ra["pts"] += 1; rb["pts"] += 1
    rows = list(table.values())
    # 排名：积分 → 净胜球 → 进球 → 随机（简化版官方细则）
    rows.sort(key=lambda r: (r["pts"], r["gf"] - r["ga"], r["gf"], rng.random()),
              reverse=True)
    return rows


def assign_third_places(qualified_groups: list[str],
                        rng: random.Random) -> dict[int, str]:
    """把 8 个晋级的小组第三分配到 8 个限定签位（回溯求可行解）。"""
    slots = list(THIRD_PLACE_SLOTS.keys())
    rng.shuffle(slots)  # 随机化搜索顺序，避免系统性偏置
    assignment: dict[int, str] = {}

    def backtrack(i: int, remaining: set[str]) -> bool:
        if i == len(slots):
            return True
        slot = slots[i]
        candidates = [g for g in remaining if g in THIRD_PLACE_SLOTS[slot]]
        rng.shuffle(candidates)
        for g in candidates:
            assignment[slot] = g
            if backtrack(i + 1, remaining - {g}):
                return True
            del assignment[slot]
        return False

    if backtrack(0, set(qualified_groups)):
        return assignment

    # 理论上极少数组合无完美解：退化为按签位顺序尽量匹配，剩余随意填
    assignment.clear()
    remaining = list(qualified_groups)
    for slot in THIRD_PLACE_SLOTS:
        pick = next((g for g in remaining if g in THIRD_PLACE_SLOTS[slot]),
                    remaining[0])
        assignment[slot] = pick
        remaining.remove(pick)
    return assignment


def simulate_tournament(groups: dict[str, list[dict]], rng: random.Random,
                        fixed: dict | None = None) -> dict:
    """模拟一届完整世界杯（可条件化），返回各阶段结果。"""
    fixed = fixed or {}
    group_results = fixed.get("group_results", {})
    ko_teams = fixed.get("ko_teams", {})
    ko_winners = fixed.get("ko_winners", {})
    we_overrides = fixed.get("we_overrides", {})
    by_code = {t["code"]: t for g in GROUPS for t in groups[g]}

    # ---- 小组赛 ----
    standings = {g: play_group(groups[g], rng, group_results, we_overrides)
                 for g in GROUPS}
    thirds = [(g, standings[g][2]) for g in GROUPS]
    thirds.sort(key=lambda x: (x[1]["pts"], x[1]["gf"] - x[1]["ga"],
                               x[1]["gf"], rng.random()), reverse=True)
    qualified_third_groups = [g for g, _ in thirds[:8]]
    third_team = {g: standings[g][2]["team"] for g in qualified_third_groups}
    slot_assignment = assign_third_places(qualified_third_groups, rng)

    def resolve(spec: str) -> dict:
        pos, grp = int(spec[0]), spec[1]
        return standings[grp][pos - 1]["team"]

    def play_ko(match_no: int, team_a: dict, team_b: dict) -> dict:
        """淘汰赛一场：已有真实胜者则直接采用。"""
        if match_no in ko_winners:
            wcode = ko_winners[match_no]
            return by_code[wcode]
        return simulate_knockout_match(
            team_a, team_b, rng,
            we_override=_we_lookup(we_overrides, team_a, team_b))["winner"]

    # ---- 32 强 ----
    winners: dict[int, dict] = {}
    r32_codes: set[str] = set()
    for match_no, (top, bottom) in ROUND_OF_32.items():
        if match_no in ko_teams:  # 官方已确定对阵（小组赛全部结束后）
            ca, cb = ko_teams[match_no]
            team_a, team_b = by_code[ca], by_code[cb]
        else:
            team_a = resolve(top)
            team_b = (third_team[slot_assignment[match_no]]
                      if bottom.startswith("3:") else resolve(bottom))
        r32_codes.add(team_a["code"])
        r32_codes.add(team_b["code"])
        winners[match_no] = play_ko(match_no, team_a, team_b)

    # ---- 16 强 → 决赛 ----
    def play_round(bracket: dict[int, tuple[int, int]]):
        for match_no, (m1, m2) in bracket.items():
            winners[match_no] = play_ko(match_no, winners[m1], winners[m2])

    play_round(ROUND_OF_16)
    play_round(QUARTERS)
    play_round(SEMIS)

    if 104 in ko_winners:
        champion = by_code[ko_winners[104]]
        runner_up = (winners[102] if winners[101]["code"] == champion["code"]
                     else winners[101])
    else:
        final = simulate_knockout_match(
            winners[101], winners[102], rng,
            we_override=_we_lookup(we_overrides, winners[101], winners[102]))
        champion, runner_up = final["winner"], final["loser"]

    return {
        "standings": standings,
        "r32": r32_codes,
        "r16": {w["code"] for w in (winners[m] for m in ROUND_OF_32)},
        "qf": {w["code"] for w in (winners[m] for m in ROUND_OF_16)},
        "sf": {w["code"] for w in (winners[m] for m in QUARTERS)},
        "finalists": {winners[101]["code"], winners[102]["code"]},
        "champion": champion["code"],
        "runner_up": runner_up["code"],
    }
