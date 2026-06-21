"""运营硬数据更新：比分/回报系数/结算/预测产物。

这个入口给服务器 cron 使用，只跑硬数据链路：
比分/回报系数同步 -> 预测结算 -> Elo/市场融合 -> 蒙特卡洛 -> web 数据发布。

情报整理、战报/单场看点、AI 讨论都由独立流程触发，不挂在本入口后面。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import db, update

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
RUNS_PATH = OUT / "ops_runs.jsonl"
LOCK_PATH = OUT / "ops_update.lock"


@contextmanager
def run_lock(path: Path, stale_minutes: int = 180, break_lock: bool = False):
    OUT.mkdir(exist_ok=True)
    stale_after = max(1, int(stale_minutes or 180)) * 60
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if break_lock or age > stale_after:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            raise SystemExit(
                f"  [ops] 上一轮仍在运行，跳过（lock={path}, age={age:.0f}s）")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        payload = {"pid": os.getpid(), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        os.close(fd)
        fd = -1
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _append_run(record: dict) -> None:
    OUT.mkdir(exist_ok=True)
    with RUNS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _snapshot() -> dict:
    db.init_db()
    matches = db.load_matches()
    reports = db.load_reports()
    blurbs = db.load_blurbs()
    return {
        "played": sum(1 for m in matches if m.get("score")
                      and m.get("home") and m.get("away")),
        "reports": len(reports),
        "latest_report_no": reports[-1]["no"] if reports else 0,
        "blurbs": len(blurbs),
        "intel": db.intel_count(),
    }


def run_pipeline(args: argparse.Namespace) -> dict:
    started = time.time()
    before = _snapshot()

    print("  [ops] 硬数据更新开始")
    payload = update.run(
        sims=args.sims,
        seed=args.seed,
        do_fetch=not args.no_fetch and not args.offline,
        workers=args.workers,
        dry_run=args.dry_run,
        force_publish=args.force_publish,
        publish_content=False,
    )
    after = _snapshot()
    events = {
        "new_finished": max(0, int(payload["meta"]["played"]) - before["played"]),
        "played": payload["meta"]["played"],
        "sims": payload["meta"]["sims"],
        "market": payload["meta"].get("market", False),
        "content_generated": False,
        "agent_rounds": 0,
    }

    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
        "duration_sec": round(time.time() - started, 2),
        "dry_run": bool(args.dry_run),
        "offline": bool(args.offline),
        "before": before,
        "after": after,
        "events": events,
    }
    _append_run(record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="世界杯 AI 站运营硬数据更新")
    parser.add_argument("--sims", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--no-fetch", action="store_true", help="跳过比分/回报系数联网")
    parser.add_argument("--offline", action="store_true",
                        help="离线演练：跳过比分/回报系数联网")
    parser.add_argument("--dry-run", action="store_true",
                        help="不写真库/发布产物")
    parser.add_argument("--force-publish", action="store_true")
    parser.add_argument("--break-lock", action="store_true")
    parser.add_argument("--lock-stale-minutes", type=int, default=180)
    args = parser.parse_args()

    try:
        with run_lock(LOCK_PATH, args.lock_stale_minutes, args.break_lock):
            record = run_pipeline(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "failed",
            "error": str(exc)[:500],
            "dry_run": bool(args.dry_run),
            "offline": bool(args.offline),
        }
        _append_run(record)
        raise
    print("  [ops] 完成: "
          + json.dumps({k: record[k] for k in (
              "status", "duration_sec", "events")},
              ensure_ascii=False))


if __name__ == "__main__":
    main()
