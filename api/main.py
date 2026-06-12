"""投注 API 服务：GitHub 登录 + 虚拟积分投注 + 排行榜。

运行：.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8643
nginx 把 /api/ 反代到本服务；静态站照旧由 nginx 直接服务。

安全要点：
- 会话 = HMAC 签名 cookie（HttpOnly/Secure/SameSite=Lax），无服务端会话存储
- 写接口校验 Origin + 限频（IP 维度）
- 下注守卫：开球即锁盘（沿用滚球盘口事故的教训），赔率下注瞬间快照
- 积分为虚拟娱乐积分，不可兑现
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
OAUTH = CONFIG.get("github_oauth", {})
SECRET = CONFIG["session_secret"].encode()
SITE = "https://worldcup.lightai.io"

SESSION_COOKIE = "wc_session"
SESSION_TTL = 30 * 24 * 3600
MIN_STAKE, MAX_STAKE = 10, 100000
ODDS_CAP_P = 0.02          # 概率下限 → 赔率上限 50x

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


ALLOWED_ORIGINS = (SITE, "http://localhost:8642", "http://localhost:8643",
                   "http://127.0.0.1:8642", "http://127.0.0.1:8643")

def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, "来源不被允许")


# ------------------------------------------------------------------- odds --

_odds_cache = {"mtime": 0.0, "data": {}}

def current_odds() -> dict:
    """从最新预测结果换算固定赔率：odds = 1 / p（按 mtime 缓存）。"""
    path = ROOT / "out" / "results.json"
    mtime = path.stat().st_mtime
    if mtime != _odds_cache["mtime"]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        table = {}
        for m in payload["schedule"]:
            p = m.get("pred")
            if not p or m.get("score"):
                continue
            table[m["match"]] = {
                "date_utc": m["date_utc"],
                "home": m["home"], "away": m["away"],
                "probs": {"H": p["p_home"], "D": p["p_draw"], "A": p["p_away"]},
                "odds": {k: round(1 / max(v, ODDS_CAP_P), 2)
                         for k, v in
                         {"H": p["p_home"], "D": p["p_draw"],
                          "A": p["p_away"]}.items()},
            }
        _odds_cache.update(mtime=mtime, data=table)
    return _odds_cache["data"]


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
def auth_login():
    state_payload = json.dumps({"n": secrets.token_hex(8),
                                "exp": time.time() + 600}).encode()
    state = _sign(state_payload)
    url = ("https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": OAUTH["client_id"],
        "redirect_uri": OAUTH["callback_url"],
        "state": state,
    }))
    resp = RedirectResponse(url)
    resp.set_cookie("wc_oauth_state", state, max_age=600, httponly=True,
                    secure=True, samesite="lax")
    return resp


@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    saved = request.cookies.get("wc_oauth_state")
    if not code or not state or state != saved or not _unsign(state):
        raise HTTPException(400, "OAuth state 校验失败，请重新登录")

    body = urllib.parse.urlencode({
        "client_id": OAUTH["client_id"],
        "client_secret": OAUTH["client_secret"],
        "code": code,
        "redirect_uri": OAUTH["callback_url"],
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


@app.get("/api/odds")
def odds():
    return current_odds()


@app.post("/api/bets")
async def create_bet(request: Request):
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
        raise HTTPException(422, f"注额范围 {MIN_STAKE}~{MAX_STAKE}")

    table = current_odds()
    entry = table.get(match_no)
    if not entry:
        raise HTTPException(400, "该场暂不可投注（对阵未定或已完赛）")
    if kickoff_passed(entry["date_utc"]):
        raise HTTPException(400, "已开球，投注关闭")

    odds_val = entry["odds"][pick]
    try:
        bet = db.place_bet(user["id"], match_no, pick, stake, odds_val)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    fresh = db.get_user(user["id"])
    return {"bet": {k: bet[k] for k in
                    ("id", "match_no", "pick", "stake", "odds", "placed_at")},
            "balance": fresh["balance"]}


@app.get("/api/bets/me")
def my_bets(request: Request):
    user = require_user(request)
    return db.user_bets(user["id"])


@app.get("/api/bets/match/{match_no}")
def bets_of_match(match_no: int):
    return db.match_bets(match_no)


@app.get("/api/bets/recent")
def recent_bets():
    """全站最近投注流（含 AI），用于比赛卡角标与动态。"""
    conn = db.connect()
    rows = conn.execute("""
        SELECT b.match_no, b.pick, b.stake, b.odds, b.settled, b.payout,
               b.placed_at, b.reason, u.kind, u.login, u.name, u.avatar_url,
               u.model
        FROM bets b JOIN users u ON u.id=b.user_id
        ORDER BY b.id DESC LIMIT 100""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/agents")
def agents_info():
    """AI 选手专区：选手卡 + 积分曲线 + 圆桌发言。"""
    rows = db.leaderboard(100)
    agents = [r for r in rows if r["kind"] == "agent"]
    for a in agents:
        a["timeline"] = db.balance_timeline(a["id"])
    return {"agents": agents, "posts": db.agent_posts(100)}


@app.get("/api/leaderboard")
def get_leaderboard():
    return db.leaderboard(100)


@app.get("/api/timeline/{user_id}")
def timeline(user_id: int):
    return db.balance_timeline(user_id)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "服务器开小差了"})


# 本地开发便利：uvicorn 同端口托管静态站（线上由 nginx 直接服务静态文件）
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
