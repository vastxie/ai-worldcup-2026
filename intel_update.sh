#!/usr/bin/env bash
# 情报广场更新：RSS/公开源 → 逐篇 GLM 整理 → intel 入库。
cd "$(dirname "$0")"
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
"$PYTHON" -u -m src.intel_update "$@"
