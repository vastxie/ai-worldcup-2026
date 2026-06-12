#!/usr/bin/env bash
# SQLite 每日备份（在线热备，保留最近 7 份）
cd "$(dirname "$0")"
mkdir -p backups
python3 - <<'EOF'
import sqlite3, time
from pathlib import Path

src = sqlite3.connect("data/worldcup.db")
dst_path = Path(f"backups/worldcup-{time.strftime('%Y%m%d')}.db")
dst = sqlite3.connect(dst_path)
src.backup(dst)
dst.close(); src.close()

backups = sorted(Path("backups").glob("worldcup-*.db"))
for old in backups[:-7]:
    old.unlink()
print(f"backup -> {dst_path} ({dst_path.stat().st_size // 1024} KB)，现存 {min(len(backups), 7)} 份")
EOF
