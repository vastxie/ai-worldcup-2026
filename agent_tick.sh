#!/usr/bin/env bash
# AI 讨论 tick：每次调度一轮 AI 行动，适合 20-30 分钟 cron。
cd "$(dirname "$0")"
mkdir -p out
LOCK_DIR="out/agent_tick.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "  [agent-tick] 上一轮仍在运行，跳过"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
ROUNDS="${AGENT_TICK_ROUNDS:-1}"
MAX_STEPS="${AGENT_TICK_MAX_STEPS:-20}"
MAX_PUBLIC_POSTS="${AGENT_TICK_MAX_PUBLIC_POSTS:-2}"
MAX_BETS="${AGENT_TICK_MAX_BETS:-3}"
MAX_INTEL_READS="${AGENT_TICK_MAX_INTEL_READS:-5}"
"$PYTHON" -u -m src.agent_session \
  --rounds "$ROUNDS" \
  --max-steps "$MAX_STEPS" \
  --max-public-posts-per-turn "$MAX_PUBLIC_POSTS" \
  --max-bets-per-turn "$MAX_BETS" \
  --max-intel-reads-per-turn "$MAX_INTEL_READS" \
  "$@"
