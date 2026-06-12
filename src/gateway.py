"""自研统一模型网关：多协议适配、超时重试、用量记账。纯标准库。

配置在 data/config.json（gitignored，密钥绝不入库）：
  "gateway": {
    "models": [
      {"id": "claude",  "label": "Claude",  "protocol": "anthropic",
       "base_url": "https://api.anthropic.com", "api_key": "...",
       "model": "claude-sonnet-4-6"},
      {"id": "gpt",     "label": "GPT",     "protocol": "openai",
       "base_url": "https://api.openai.com/v1", "api_key": "...",
       "model": "gpt-5.2"},
      {"id": "gemini",  "label": "Gemini",  "protocol": "gemini",
       "base_url": "https://generativelanguage.googleapis.com",
       "api_key": "...", "model": "gemini-3-flash"}
    ]
  }

协议适配（参考 99Agent modelGateway 的 adapter 划分）：
  openai    → POST {base}/chat/completions
  anthropic → POST {base}/v1/messages
  gemini    → POST {base}/v1beta/models/{model}:generateContent
  mock      → 离线测试用，返回 canned 文本

用法：
  from src.gateway import Gateway
  gw = Gateway()
  out = gw.chat("claude", system="...", user="...", agent="claude-bettor")
  out -> {"text": str, "prompt_tokens": int, "completion_tokens": int}
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 120
RETRIES = 2


def _post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------- 协议适配器 --

def _call_openai(cfg: dict, system: str, user: str, max_tokens: int,
                 temperature: float) -> dict:
    out = _post(cfg["base_url"].rstrip("/") + "/chat/completions",
                {"Authorization": f"Bearer {cfg['api_key']}"},
                {"model": cfg["model"],
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                 "max_tokens": max_tokens, "temperature": temperature})
    usage = out.get("usage") or {}
    return {"text": out["choices"][0]["message"]["content"] or "",
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0)}


def _call_anthropic(cfg: dict, system: str, user: str, max_tokens: int,
                    temperature: float) -> dict:
    out = _post(cfg["base_url"].rstrip("/") + "/v1/messages",
                {"x-api-key": cfg["api_key"],
                 "anthropic-version": "2023-06-01"},
                {"model": cfg["model"], "system": system,
                 "messages": [{"role": "user", "content": user}],
                 "max_tokens": max_tokens, "temperature": temperature})
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if b.get("type") == "text")
    usage = out.get("usage") or {}
    return {"text": text,
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0)}


def _call_gemini(cfg: dict, system: str, user: str, max_tokens: int,
                 temperature: float) -> dict:
    url = (cfg["base_url"].rstrip("/")
           + f"/v1beta/models/{cfg['model']}:generateContent"
           + f"?key={cfg['api_key']}")
    out = _post(url, {}, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens,
                             "temperature": temperature}})
    cand = (out.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "")
                   for p in cand.get("content", {}).get("parts", []))
    usage = out.get("usageMetadata") or {}
    return {"text": text,
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0)}


def _call_mock(cfg: dict, system: str, user: str, max_tokens: int,
               temperature: float) -> dict:
    return {"text": cfg.get("mock_response", "{}"),
            "prompt_tokens": len(system + user) // 4, "completion_tokens": 50}


ADAPTERS = {"openai": _call_openai, "anthropic": _call_anthropic,
            "gemini": _call_gemini, "mock": _call_mock}


# ----------------------------------------------------------------- 网关类 --

class Gateway:
    def __init__(self):
        cfg = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
        self.models = {m["id"]: m for m in cfg.get("gateway", {}).get("models", [])}

    def available(self) -> list[dict]:
        return [{"id": m["id"], "label": m.get("label", m["id"]),
                 "model": m.get("model", "")} for m in self.models.values()]

    def chat(self, model_id: str, system: str, user: str,
             max_tokens: int = 2000, temperature: float = 0.7,
             agent: str = "") -> dict:
        cfg = self.models.get(model_id)
        if not cfg:
            raise KeyError(f"网关未配置模型: {model_id}")
        adapter = ADAPTERS.get(cfg.get("protocol", "openai"))
        if not adapter:
            raise KeyError(f"未知协议: {cfg.get('protocol')}")

        last_err = None
        for attempt in range(RETRIES + 1):
            t0 = time.time()
            try:
                out = adapter(cfg, system, user, max_tokens, temperature)
                self._log(agent, cfg, out, ok=1,
                          note=f"{time.time() - t0:.1f}s")
                return out
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, json.JSONDecodeError, KeyError,
                    OSError) as exc:
                last_err = exc
                detail = ""
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        detail = exc.read().decode()[:200]
                    except Exception:  # noqa: BLE001
                        pass
                self._log(agent, cfg, None, ok=0,
                          note=f"attempt{attempt}: {exc} {detail}"[:300])
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"网关调用失败 [{model_id}]: {last_err}")

    @staticmethod
    def _log(agent: str, cfg: dict, out: dict | None, ok: int, note: str):
        try:
            with db.transaction() as conn:
                conn.execute("""INSERT INTO gateway_usage
                    (ts, agent, model, prompt_tokens, completion_tokens, ok, note)
                    VALUES (?,?,?,?,?,?,?)""",
                    (db.now(), agent, cfg.get("model", cfg.get("id")),
                     (out or {}).get("prompt_tokens", 0),
                     (out or {}).get("completion_tokens", 0), ok, note))
        except Exception:  # noqa: BLE001 - 记账失败不阻塞调用
            pass
