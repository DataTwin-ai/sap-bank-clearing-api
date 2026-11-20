"""
Builds the deployment bundle dist/sap-extraction-api-<YYYYMMDD-HHMM>.tar.gz

    python deploy/build_bundle.py

Contains only what the server needs. Never included: config.ini (secrets - copied separately),
*.bak_* backups, log/, data/, dist/, __pycache__/, Windows-only start_worker.bat.
Shell scripts, systemd units and SQL get LF line endings; *.sh are executable.
"""

import io
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "sap-extraction-api"

INCLUDE_ROOT = [
    "cli.py", "context.py", "db_manager.py", "delta.py", "ingestion.py", "logger_setup.py",
    "main.py", "pipeline.py", "sap_odata_extractor.py", "settings.py", "sync_bsid_to_bt3i.py",
    "worker.py", "requirements.txt", "config.example.ini", "README.md",
]
INCLUDE_DIRS = ["deploy", "docs"]
SKIP_NAMES = {"config.ini", "start_worker.bat", "build_bundle.py"}
SKIP_PARTS = {"__pycache__", "log", "data", "dist", ".venv", "venv"}
LF_SUFFIXES = {".sh", ".service", ".sql", ".example", ".py", ".ini", ".md", ".txt"}


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return (path.is_file() and path.name not in SKIP_NAMES and ".bak_" not in path.name
            and not (set(rel.parts) & SKIP_PARTS))


def main() -> int:
    files = [ROOT / f for f in INCLUDE_ROOT]
    missing = [str(f.relative_to(ROOT)) for f in files if not f.exists()]
    if missing:
        print(f"missing files: {missing}", file=sys.stderr)
        return 1
    for d in INCLUDE_DIRS:
        files += sorted(p for p in (ROOT / d).rglob("*") if wanted(p))

    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{TOP}-{datetime.now():%Y%m%d-%H%M}.tar.gz"
    now = int(time.time())
    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            data = f.read_bytes()
            if f.suffix in LF_SUFFIXES or f.name == "crontab.example":
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f"{TOP}/{rel}")
            info.size, info.mtime = len(data), now
            info.mode = 0o755 if f.suffix == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
            print(f"  {rel}")
    print(f"\n{len(files)} files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
