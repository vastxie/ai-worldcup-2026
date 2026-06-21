#!/usr/bin/env bash
# 运营硬数据更新：比分/回报系数 → 结算 → 预测重算 → web 数据发布
cd "$(dirname "$0")"
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
"$PYTHON" -u -m src.ops_update "$@"
