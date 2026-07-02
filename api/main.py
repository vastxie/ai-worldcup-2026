"""预测 API 服务：GitHub 登录 + 虚拟积分预测 + 排行榜。

运行：.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8643
nginx 把 /api/ 反代到本服务；静态站照旧由 nginx 直接服务。

安全要点：
- 会话 = HMAC 签名 cookie（HttpOnly/Secure/SameSite=Lax），无服务端会话存储
- 写接口校验 Origin + 限频（IP 维度）
- 预测守卫：开球即锁定（沿用赛中数据事故的教训），回报系数提交瞬间快照
- 积分为站内虚拟积分
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402
from src.model import exact_score_prob  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

def _load_config() -> dict:
    path = Path(os.environ.get("WORLDCUP_CONFIG", ROOT / "data" / "config.json"))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是合法 JSON: {path}") from exc


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _origin_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [x.strip().rstrip("/") for x in value.split(",") if x.strip()]
    return [str(x).strip().rstrip("/") for x in value if str(x).strip()]


def _url_origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(str(url))
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").split(",", 1)[0].strip().lower()


CONFIG = _load_config()
SITE = (_env_first("WORLDCUP_SITE_URL", "SITE_URL")
        or CONFIG.get("site_url") or "https://wc.lightai.io").rstrip("/")

OAUTH = dict(CONFIG.get("github_oauth", {}))
for env, key in (("GITHUB_CLIENT_ID", "client_id"),
                 ("GITHUB_CLIENT_SECRET", "client_secret"),
                 ("GITHUB_CALLBACK_URL", "callback_url")):
    if os.environ.get(env):
        OAUTH[key] = os.environ[env]

SESSION_SECRET = (_env_first("WORLDCUP_SESSION_SECRET", "SESSION_SECRET")
                  or CONFIG.get("session_secret"))
if not SESSION_SECRET:
    raise RuntimeError("缺少 session_secret：请设置 SESSION_SECRET 或 data/config.json")
SECRET = SESSION_SECRET.encode()

SESSION_COOKIE = "wc_session"
SESSION_TTL = 30 * 24 * 3600
MIN_STAKE, MAX_STAKE = 10, 100000
ODDS_CAP_P = 0.02          # 概率下限 → 回报系数上限 50x
SCORE_MIN_STAKE, SCORE_MAX_STAKE = 10, 50
SCORE_MAX_GOALS = 6
SCORE_BOOK_MARGIN = 0.10
SCORE_MAX_COEFFICIENT = 80.0

app = FastAPI(title="worldcup-arena", docs_url=None, redoc_url=None)
db.init_db()


# ---------------------------------------------------------------- session --

def _sign(payload: bytes) -> str:
    sig = hmac.new(SECRET, payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." +
            base64.urlsafe_b64encode(sig).decode().rstrip("="))


def _unsign(token: str) -> dict | None:
    try:
        p64, s64 = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        payload = base64.urlsafe_b64decode(pad(p64))
        sig = base64.urlsafe_b64decode(pad(s64))
        if not hmac.compare_digest(
                hmac.new(SECRET, payload, hashlib.sha256).digest(), sig):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def session_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = _unsign(token)
    if not data:
        return None
    return db.get_user(data["uid"])


def require_user(request: Request) -> dict:
    user = session_user(request)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


# -------------------------------------------------------------- rate limit --

_BUCKETS: dict[str, list[float]] = {}

def rate_limit(request: Request, key: str, per_min: int) -> None:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    k = f"{key}:{ip}"
    now = time.time()
    bucket = [t for t in _BUCKETS.get(k, []) if now - t < 60]
    if len(bucket) >= per_min:
        raise HTTPException(429, "请求太频繁，歇一会儿")
    bucket.append(now)
    _BUCKETS[k] = bucket
    if len(_BUCKETS) > 10000:  # 防膨胀
        _BUCKETS.clear()


ALLOWED_ORIGINS = tuple(dict.fromkeys([
    SITE, "http://localhost:8642", "http://localhost:8643",
    "http://127.0.0.1:8642", "http://127.0.0.1:8643",
    *_origin_list(_env_first("WORLDCUP_ALLOWED_ORIGINS", "ALLOWED_ORIGINS")),
    *_origin_list(CONFIG.get("allowed_origins")),
]))

def require_oauth_config() -> dict:
    missing = [k for k in ("client_id", "client_secret", "callback_url")
               if not OAUTH.get(k)]
    if missing:
        raise HTTPException(503, "GitHub OAuth 未配置: " + ", ".join(missing))
    return OAUTH

def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, "来源不被允许")


# ---------------------------------------------------------------- projection --

_odds_cache = {"mtime": 0.0, "data": {}}

def current_odds() -> dict:
    """从最新预测结果换算固定回报系数（按 mtime 缓存）。"""
    path = ROOT / "out" / "results.json"
    mtime = path.stat().st_mtime
    if mtime != _odds_cache["mtime"]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        teams = {t["code"]: t for t in payload.get("teams", [])}
        table = {}
        for m in payload["schedule"]:
            p = m.get("pred")
            if not p or m.get("score"):
                continue
            score_probs, score_odds = {}, {}
            home, away = teams.get(m.get("home")), teams.get(m.get("away"))
            if home and away:
                for gh in range(SCORE_MAX_GOALS + 1):
                    for ga in range(SCORE_MAX_GOALS + 1):
                        key = f"{gh}-{ga}"
                        prob = exact_score_prob(home, away, gh, ga,
                                                we_override=p)
                        score_probs[key] = round(prob, 5)
                        if prob > 0:
                            score_odds[key] = round(
                                min(SCORE_MAX_COEFFICIENT,
                                    1 / (prob * (1 + SCORE_BOOK_MARGIN))),
                                2,
                            )
            table[m["match"]] = {
                "date_utc": m["date_utc"],
                "home": m["home"], "away": m["away"],
                "probs": {"H": p["p_home"], "D": p["p_draw"], "A": p["p_away"]},
                "odds": {k: round(1 / max(v, ODDS_CAP_P), 2)
                         for k, v in
                         {"H": p["p_home"], "D": p["p_draw"],
                          "A": p["p_away"]}.items()},
                "score_odds": score_odds,
                "score_probs": score_probs,
                "score_max_goals": SCORE_MAX_GOALS,
            }
        _odds_cache.update(mtime=mtime, data=table)
    return _odds_cache["data"]


def public_projections() -> dict:
    """公开接口使用低敏字段名；内部 odds 字段不透出。"""
    out = {}
    for match_no, item in current_odds().items():
        out[match_no] = {
            "date_utc": item["date_utc"],
            "home": item["home"],
            "away": item["away"],
            "probs": item["probs"],
            "coefficients": item["odds"],
            "score_coefficients": item.get("score_odds") or {},
            "score_probs": item.get("score_probs") or {},
            "score_max_goals": item.get("score_max_goals"),
        }
    return out


def public_pick_row(row: dict) -> dict:
    out = dict(row)
    if "odds" in out:
        out["coefficient"] = out.pop("odds")
    return out


def public_pick_rows(rows: list[dict]) -> list[dict]:
    return [public_pick_row(r) for r in rows]


def kickoff_passed(date_utc: str) -> bool:
    dt = datetime.fromisoformat(date_utc.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


# ------------------------------------------------------------------ routes --

@app.get("/api/health")
def health():
    return {"ok": True, "ts": db.now()}


@app.get("/api/auth/login")
def auth_login(request: Request):
    oauth = require_oauth_config()
    callback_origin = _url_origin(oauth.get("callback_url"))
    callback_host = urllib.parse.urlparse(callback_origin or "").netloc.lower()
    request_host = _request_host(request)
    if callback_origin and callback_host and request_host and request_host != callback_host:
        return RedirectResponse(callback_origin + "/api/auth/login")
    state_payload = json.dumps({"n": secrets.token_hex(8),
                                "exp": time.time() + 600}).encode()
    state = _sign(state_payload)
    url = ("https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": oauth["client_id"],
        "redirect_uri": oauth["callback_url"],
        "state": state,
    }))
    resp = RedirectResponse(url)
    resp.set_cookie("wc_oauth_state", state, max_age=600, httponly=True,
                    secure=True, samesite="lax")
    return resp


@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    oauth = require_oauth_config()
    saved = request.cookies.get("wc_oauth_state")
    if not code or not state or state != saved or not _unsign(state):
        raise HTTPException(400, "OAuth state 校验失败，请重新登录")

    body = urllib.parse.urlencode({
        "client_id": oauth["client_id"],
        "client_secret": oauth["client_secret"],
        "code": code,
        "redirect_uri": oauth["callback_url"],
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token", data=body,
        headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        token = json.loads(r.read()).get("access_token")
    if not token:
        raise HTTPException(400, "GitHub 换取 token 失败")

    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "worldcup-arena"})
    with urllib.request.urlopen(req, timeout=20) as r:
        gh = json.loads(r.read())

    user = db.get_or_create_github_user(
        gh["id"], gh["login"], gh.get("name"), gh.get("avatar_url"))

    session = _sign(json.dumps({"uid": user["id"],
                                "exp": time.time() + SESSION_TTL}).encode())
    resp = RedirectResponse(SITE + "/#schedule")
    resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL,
                    httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("wc_oauth_state")
    return resp


@app.get("/api/auth/logout")
def auth_logout():
    resp = RedirectResponse(SITE)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request):
    user = session_user(request)
    if not user:
        return {"login": None}
    return {"id": user["id"], "login": user["login"], "name": user["name"],
            "avatar_url": user["avatar_url"], "balance": user["balance"],
            "kind": user["kind"]}


@app.get("/api/projections")
def projections():
    return public_projections()


async def _create_bet(request: Request):
    check_origin(request)
    rate_limit(request, "bet", 20)
    user = require_user(request)
    try:
        body = await request.json()
        match_no = int(body["match_no"])
        pick = str(body["pick"]).upper()
        stake = int(body["stake"])
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "参数格式错误")

    if pick not in ("H", "D", "A"):
        raise HTTPException(422, "pick 必须是 H/D/A")
    if not (MIN_STAKE <= stake <= MAX_STAKE):
        raise HTTPException(422, f"投入积分范围 {MIN_STAKE}~{MAX_STAKE}")

    table = current_odds()
    entry = table.get(match_no)
    if not entry:
        raise HTTPException(400, "该场暂不可提交预测（对阵未定或已完赛）")
    if kickoff_passed(entry["date_utc"]):
        raise HTTPException(400, "已开球，预测入口关闭")

    odds_val = entry["odds"][pick]
    try:
        bet = db.place_bet(user["id"], match_no, pick, stake, odds_val)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    fresh = db.get_user(user["id"])
    return {"bet": {k: bet[k] for k in
                    ("id", "match_no", "pick", "stake", "odds", "placed_at")},
            "balance": fresh["balance"]}


@app.post("/api/picks")
async def create_pick(request: Request):
    resp = await _create_bet(request)
    return {"pick": public_pick_row(resp["bet"]), "balance": resp["balance"]}


async def _create_score_bet(request: Request):
    check_origin(request)
    rate_limit(request, "score-bet", 20)
    user = require_user(request)
    try:
        body = await request.json()
        match_no = int(body["match_no"])
        home_score = int(body["home_score"])
        away_score = int(body["away_score"])
        stake = int(body["stake"])
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "参数格式错误")

    if not (0 <= home_score <= SCORE_MAX_GOALS
            and 0 <= away_score <= SCORE_MAX_GOALS):
        raise HTTPException(422, f"比分范围 0~{SCORE_MAX_GOALS}")
    if not (SCORE_MIN_STAKE <= stake <= SCORE_MAX_STAKE):
        raise HTTPException(422, f"比分预测投入范围 {SCORE_MIN_STAKE}~{SCORE_MAX_STAKE}")

    table = current_odds()
    entry = table.get(match_no)
    if not entry:
        raise HTTPException(400, "该场暂不可提交预测（对阵未定或已完赛）")
    if kickoff_passed(entry["date_utc"]):
        raise HTTPException(400, "已开球，预测入口关闭")

    key = f"{home_score}-{away_score}"
    odds_val = (entry.get("score_odds") or {}).get(key)
    if odds_val is None:
        raise HTTPException(400, "该比分暂不可提交预测")
    try:
        bet = db.place_score_bet(user["id"], match_no, home_score,
                                 away_score, stake, odds_val)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    fresh = db.get_user(user["id"])
    return {"bet": {k: bet[k] for k in
                    ("id", "match_no", "home_score", "away_score",
                     "stake", "odds", "placed_at")},
            "balance": fresh["balance"]}


@app.post("/api/score-picks")
async def create_score_pick(request: Request):
    resp = await _create_score_bet(request)
    return {"pick": public_pick_row(resp["bet"]), "balance": resp["balance"]}


@app.get("/api/picks/me")
def my_picks(request: Request):
    user = require_user(request)
    return public_pick_rows(db.user_bets(user["id"]))


@app.get("/api/picks/match/{match_no}")
def picks_of_match(match_no: int):
    return public_pick_rows(db.match_bets(match_no, agents_only=True))


@app.get("/api/picks/agent/{login}")
def agent_pick_history(login: str):
    u = db.user_by_login(login)
    if not u or u["kind"] != "agent":
        raise HTTPException(404, "只有 AI 选手的预测记录是公开的")
    return public_pick_rows(db.user_bets(u["id"]))


@app.get("/api/picks/recent")
def recent_picks(limit: int = 100, offset: int = 0, with_total: bool = False):
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    items = public_pick_rows(db.recent_agent_bets(limit, offset))
    if with_total:
        return {"items": items, "total": db.recent_agent_bets_count()}
    return items


@app.post("/api/posts/{post_id}/like")
async def like_post(post_id: int, request: Request):
    check_origin(request)
    rate_limit(request, "like", 30)
    user = require_user(request)
    try:
        return db.toggle_like(post_id, user["id"])
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/agents")
def agents_info():
    """AI 选手专区：选手卡 + 积分曲线。讨论流走 /api/posts。"""
    rows = db.leaderboard(100)
    agents = [r for r in rows if r["kind"] == "agent"]
    for a in agents:
        a["timeline"] = db.balance_timeline(a["id"])
    return {
        "agents": agents,
        "system_bank": db.system_bank_public_summary(20),
        "daily_reward": db.daily_reward_context(),
        "point_pool": db.point_pool_summary(),
    }


@app.get("/api/posts")
def posts_feed(limit: int = 30, before_id: int | None = None,
               topic_type: str | None = None,
               topic_id: int | None = None):
    """统一 AI 讨论区：主帖按最新回复倒序分页，回帖嵌套返回。"""
    limit = max(1, min(limit, 100))
    if topic_type not in {None, "match", "report", "general"}:
        raise HTTPException(400, "topic_type 不合法")
    return db.discussion_threads(limit, before_id=before_id,
                                 topic_type=topic_type, topic_id=topic_id)


@app.get("/api/posts/match/{match_no}")
def match_posts(match_no: int):
    """兼容旧入口：某场比赛相关讨论。"""
    return db.agent_posts(200, match_no=match_no)


@app.get("/api/leaderboard")
def get_leaderboard(request: Request, limit: int = 100, offset: int = 0,
                    with_total: bool = False):
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    kind = None if session_user(request) else "agent"
    rows = db.leaderboard(limit, offset=offset, kind=kind)
    if with_total:
        return {"items": rows, "total": db.leaderboard_count(kind=kind)}
    return rows


@app.get("/api/timeline/{user_id}")
def timeline(user_id: int, request: Request):
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target["kind"] != "agent":
        user = session_user(request)
        if not user or user["id"] != user_id:
            raise HTTPException(404, "只有 AI 选手的积分曲线是公开的")
    return db.balance_timeline(user_id)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "服务器开小差了"})


# 本地开发便利：uvicorn 同端口托管静态站（线上由 nginx 直接服务静态文件）
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
