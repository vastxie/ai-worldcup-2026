"""统一模型网关客户端：项目只调用 OpenAI-compatible Chat Completions。

模型协议、账号和供应商差异交给 pi-serve 处理；本项目只保留逻辑模型 id、
真实 pi model id、超时重试和用量记账。

配置在 data/config.json（gitignored，密钥绝不入库）：
  "gateway": {
    "base_url": "http://127.0.0.1:8787/v1",
    "api_key": "...",
    "models": [
      {"id": "glm", "label": "GLM", "model": "zai-coding-cn/glm-5.2"},
      {"id": "doubao", "label": "豆包", "model": "volcengine/doubao-..."}
    ]
  }

用法：
  from src.gateway import Gateway
  gw = Gateway()
  out = gw.chat("glm", system="...", user="...", agent="glm-bettor")
  out -> {"text": str, "prompt_tokens": int, "completion_tokens": int}
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = int(os.getenv("AIWC_GATEWAY_TIMEOUT")
              or os.getenv("GATEWAY_TIMEOUT") or "600")
RETRIES = 2


def _config_path() -> Path:
    return Path(os.getenv("WORLDCUP_CONFIG", ROOT / "data" / "config.json"))


def _post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auth_headers(api_key: str | None) -> dict:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _chat_completion(gateway_cfg: dict, model_cfg: dict, system: str, user: str,
                     max_tokens: int | None, temperature: float) -> dict:
    body = {
        "model": model_cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if not model_cfg.get("no_temperature"):
        body["temperature"] = temperature
    mt = max_tokens or model_cfg.get("max_tokens") or gateway_cfg.get("max_tokens")
    if mt:
        body["max_tokens"] = mt
    body.update(gateway_cfg.get("extra") or {})
    body.update(model_cfg.get("extra") or {})

    out = _post(
        gateway_cfg["base_url"].rstrip("/") + "/chat/completions",
        _auth_headers(gateway_cfg.get("api_key")),
        body,
    )
    usage = out.get("usage") or {}
    message = (out.get("choices") or [{}])[0].get("message") or {}
    return {
        "text": message.get("content") or "",
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


class Gateway:
    def __init__(self):
        cfg = json.loads(_config_path().read_text(encoding="utf-8"))
        gateway_cfg = dict(cfg.get("gateway") or {})
        if os.getenv("AIWC_GATEWAY_BASE_URL") or os.getenv("GATEWAY_BASE_URL"):
            gateway_cfg["base_url"] = (
                os.getenv("AIWC_GATEWAY_BASE_URL")
                or os.getenv("GATEWAY_BASE_URL")
            )
        if os.getenv("AIWC_GATEWAY_API_KEY") or os.getenv("GATEWAY_API_KEY"):
            gateway_cfg["api_key"] = (
                os.getenv("AIWC_GATEWAY_API_KEY")
                or os.getenv("GATEWAY_API_KEY")
            )

        if not gateway_cfg.get("base_url"):
            raise RuntimeError("缺少 gateway.base_url，请指向 pi-serve 的 /v1 地址")

        self.gateway_cfg = gateway_cfg
        self.models = {
            m["id"]: m
            for m in gateway_cfg.get("models", [])
            if m.get("id") and m.get("model")
        }

    def available(self) -> list[dict]:
        return [{"id": m["id"], "label": m.get("label", m["id"]),
                 "model": m.get("model", "")} for m in self.models.values()]

    def chat(self, model_id: str, system: str, user: str,
             max_tokens: int | None = None, temperature: float = 0.7,
             agent: str = "") -> dict:
        """max_tokens 默认不设限（推理模型耗 token 不可控），靠 TIMEOUT 兜底。"""
        cfg = self.models.get(model_id)
        if not cfg:
            raise KeyError(f"网关未配置模型: {model_id}")

        last_err = None
        for attempt in range(RETRIES + 1):
            t0 = time.time()
            try:
                out = _chat_completion(
                    self.gateway_cfg, cfg, system, user, max_tokens, temperature)
                self._log(agent, cfg, out, ok=1,
                          note=f"{time.time() - t0:.1f}s")
                return out
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, json.JSONDecodeError, KeyError,
                    IndexError, OSError) as exc:
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
