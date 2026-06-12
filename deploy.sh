#!/usr/bin/env bash
# 部署到服务器
#   ./deploy.sh          # 只同步代码，不动服务器的运行数据
#   ./deploy.sh --init   # 全量同步（含 data.js / history.json），用于初始化或基线推送
# 服务器自己跑定时更新（cron），日常本地开发只需 ./deploy.sh 推代码。
#
# 服务器地址等私密配置放在 .deploy.env（已被 .gitignore），格式见 .deploy.env.example
set -e
cd "$(dirname "$0")"
if [[ -f .deploy.env ]]; then
  source .deploy.env
fi
: "${SERVER:?请先创建 .deploy.env 并配置 SERVER（参考 .deploy.env.example）}"
: "${DEST:?请先创建 .deploy.env 并配置 DEST（参考 .deploy.env.example）}"

EXCLUDES=(--exclude '__pycache__' --exclude '.DS_Store' --exclude 'out/'
          --exclude 'share/' --exclude '.claude/' --exclude '.git/'
          --exclude '.deploy.env' --exclude '.venv/' --exclude 'data/worldcup.db*' --exclude 'backups/')
if [[ "$1" != "--init" ]]; then
  EXCLUDES+=(--exclude 'web/data.js' --exclude 'data/history.json'
             --exclude 'data/matches.json' --exclude 'data/manual_results.json'
             --exclude 'data/odds.json' --exclude 'data/locked_preds.json')
fi

rsync -avz --delete "${EXCLUDES[@]}" ./ "$SERVER:$DEST/"
echo "--- 代码已同步，触发服务器重算 ---"
ssh "$SERVER" "cd $DEST && mkdir -p out && nohup ./update.sh >> out/update.log 2>&1 & echo started"
echo "--- 部署完成 ---"
