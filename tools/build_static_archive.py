#!/usr/bin/env python3
"""Build an allowlisted, public-only static site from the frozen archive."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "dist-static"
ASSETS = ("index.html", "archive-api.js", "ai-icons.js", "html2canvas.min.js",
          "og-image.png", "favicon.png", "apple-touch-icon.png", "contact-qr.svg",
          "site-qr.png", "sitemap.xml", "robots.txt")
DATA = ("data.js", "reports.js", "blurbs.js", "intel.js")


def build():
    snapshot = json.loads((ROOT / "archive/data/snapshot.json").read_text())
    assert snapshot["scope"] == "public-ai-only"
    assert all(a["kind"] == "agent" for a in snapshot["leaderboard"])
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir()
    for name in ASSETS:
        source = ROOT / "web" / name
        assert source.is_file() and not source.is_symlink()
        shutil.copyfile(source, DEST / name)
    for name in DATA:
        shutil.copyfile(ROOT / "archive/source-data" / name, DEST / name)
    (DEST / "archive").mkdir()
    shutil.copyfile(ROOT / "archive/data/snapshot.json", DEST / "archive/snapshot.json")
    (DEST / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n  Cache-Control: public, max-age=0, must-revalidate\n")
    manifest = {str(p.relative_to(DEST)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(DEST.rglob("*")) if p.is_file()}
    (DEST / "archive/manifest.json").write_text(json.dumps({"generated_at": snapshot["generated_at"], "counts": snapshot["counts"], "sha256": manifest}, ensure_ascii=False, indent=2))
    assert not any(p.suffix in (".db", ".sqlite", ".py", ".env") for p in DEST.rglob("*"))
    print(json.dumps({"directory": str(DEST), "files": len(manifest) + 1,
                      "bytes": sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file()),
                      "counts": snapshot["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    build()
