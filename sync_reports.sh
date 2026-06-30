#!/usr/bin/env bash
# 同步「每日战报 + 单场看点」到线上服务器（只动 reports / blurbs 两张表）。
#
# —— 为什么需要这个脚本（Codex 请先读这段）——
#   战报和单场看点是人在本地搜情报后亲自撰写、用 db.save_report / db.save_blurb
#   写进本地 worldcup.db 的 reports / blurbs 两张表的（本地、线上都没配 llm，
#   src/report.py 里的 LLM 自动生成不会触发，只会做导出）。
#   而线上服务器才是「投注数据」的唯一真相源——bets / wallet_ledger / agent_posts
#   每天被竞技场的 16 个 AI 实时写入、还有人类玩家下注。所以：
#     ✗ 绝对不要整库 scp data/worldcup.db —— 会把服务器的真实投注/钱包/评论全冲掉
#     ✗ deploy.sh 也特意 --exclude 了 worldcup.db，不要绕过它去同步整库
#     ✗ 别用 `sqlite3 .dump`/`.mode insert`：本地新版 CLI 会用 unistr() 转义，
#        线上旧版 SQLite 没这函数，导入直接报错（踩过坑）
#     ✓ 纯 python 走 JSON：本地读两表 → JSON → 服务器 save_report/save_blurb 幂等写入，
#        与 SQLite 版本无关，其余表分毫不动
#   写完让服务器从 DB 重新导出 web/reports.js / web/blurbs.js —— 前端就读这两个静态文件。
#   （服务器 cron 每 2 小时跑 update.sh 也会 _publish 导出，本脚本只是即时刷新，不用等。）
#
# —— 用法 ——
#   1) 本地写好战报（搜情报 → 撰写主笔正文 + 跟评 → 入本地 DB）：
#        python3 -c "from src import db; db.init_db(); db.save_report({
#          'no': <期号，接上一期+1>, 'date': '2026-06-13', 'time': '15:00',
#          'played': <当前已赛场次数>, 'report': '<主笔正文 180~280 字>',
#          'comment': '<跟评 40~90 字>'})"
#      单场看点（开球前那句「AI 怎么看」）：
#        python3 -c "from src import db; db.init_db(); db.save_blurb(<场次号>, '<一句话>')"
#   2) 同步上线： ./sync_reports.sh
#
# 服务器地址放 .deploy.env（已 gitignore），格式见 .deploy.env.example
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .deploy.env ]]; then source .deploy.env; fi
: "${SERVER:?请在 .deploy.env 配置 SERVER（参考 .deploy.env.example）}"
: "${DEST:?请在 .deploy.env 配置 DEST（参考 .deploy.env.example）}"

JSON="/tmp/wc_content_$$.json"
APPLY="/tmp/wc_apply_$$.py"
trap 'rm -f "$JSON" "$APPLY"' EXIT

# 1) 本地：把 reports + blurbs 两表导出为 JSON（纯 python，跨 SQLite 版本安全）
.venv/bin/python - "$JSON" <<'PY'
import json, sys, sqlite3
c = sqlite3.connect("data/worldcup.db"); c.row_factory = sqlite3.Row
data = {"reports": [dict(r) for r in c.execute("SELECT * FROM reports")],
        "blurbs":  [dict(r) for r in c.execute("SELECT * FROM blurbs")]}
c.close()
json.dump(data, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
print("本地导出 reports %d 条 / blurbs %d 条" % (len(data["reports"]), len(data["blurbs"])))
PY

# 2) 服务器侧应用脚本：读 JSON → 幂等写入两表 → 从 DB 重新导出 reports.js/blurbs.js
cat > "$APPLY" <<'PY'
import os, sys, json
sys.path.insert(0, os.getcwd())          # cwd=DEST，让 from src import 能找到包
from src import db, report
db.init_db()
data = json.load(open("/tmp/wc_content.json", encoding="utf-8"))
for r in data["reports"]:
    db.save_report(r)                     # INSERT OR REPLACE，按主键 no 覆盖
for b in data["blurbs"]:
    db.save_blurb(b["match_no"], b["text"])
report._publish()
print("synced: reports %d / blurbs %d → reports.js/blurbs.js refreshed"
      % (len(data["reports"]), len(data["blurbs"])))
PY

scp -q "$JSON" "$SERVER:/tmp/wc_content.json"
scp -q "$APPLY" "$SERVER:/tmp/wc_apply.py"
ssh "$SERVER" "cd '$DEST' && .venv/bin/python /tmp/wc_apply.py && rm -f /tmp/wc_content.json /tmp/wc_apply.py"
echo "✓ 已同步到 ${SERVER}；reports.js / blurbs.js 已刷新（前端 Cmd+Shift+R 可见）"
