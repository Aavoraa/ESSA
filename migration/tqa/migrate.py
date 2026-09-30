"""Stage 3 — transform the staged legacy data into ESSA's own tables.

Runs sql/NN_*.sql in order, each step in ONE transaction: a step either lands
whole or not at all, and the run stops at the first failure with the database
as it was before that step. Steps named *.py are run with the BACKEND's
interpreter, so they call ESSA's own services (locations sync, stock rebuild,
the shop's own catalogue sync) instead of re-implementing them.

Repeatable by construction: the target is rebuilt from nothing by
``prepare_target.py --recreate`` and the source is read-only, so a re-run is
"recreate, prepare, migrate" and always produces the same result.

    ESSA_DATABASE_URL=postgresql://postgres:***@localhost:5432/essa_staging \
        python migrate.py [--only 10] [--from 30]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent / "backend"


def _steps():
    return sorted(p for p in (HERE / "sql").iterdir() if p.suffix in (".sql", ".py"))


def _backend_python() -> str:
    for p in (BACKEND / ".venv" / "Scripts" / "python.exe", BACKEND / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    sys.exit("backend/.venv not found — run setup.bat first")


def run_step(url: str, step: Path) -> None:
    t0 = time.time()
    if step.suffix == ".sql":
        with psycopg.connect(url) as conn:
            conn.execute("set statement_timeout = 0")
            conn.execute("set work_mem = '256MB'")
            conn.execute(step.read_text(encoding="utf-8"))
            conn.execute("insert into migration.run_log(step, started_at, finished_at) "
                         "values (%s, now() - make_interval(secs => %s), now()) "
                         "on conflict (step) do update set started_at = excluded.started_at, "
                         "finished_at = excluded.finished_at",
                         (step.stem, time.time() - t0))
            conn.commit()
    else:
        env = dict(os.environ, ESSA_DATABASE_URL=url, ESSA_BOOT_MIGRATE="0",
                   PYTHONPATH=str(BACKEND))
        subprocess.run([_backend_python(), str(step)], cwd=BACKEND, env=env, check=True)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("insert into migration.run_log(step, started_at, finished_at) "
                         "values (%s, now() - make_interval(secs => %s), now()) "
                         "on conflict (step) do update set started_at = excluded.started_at, "
                         "finished_at = excluded.finished_at", (step.stem, time.time() - t0))
    print(f"  {step.name:40s} {time.time() - t0:8.1f}s", flush=True)


def main() -> int:
    url = os.environ.get("ESSA_DATABASE_URL")
    if not url:
        print(__doc__)
        return 2
    args = sys.argv[1:]
    only = args[args.index("--only") + 1] if "--only" in args else None
    start = args[args.index("--from") + 1] if "--from" in args else None
    for step in _steps():
        num = step.name[:2]
        if only and num != only:
            continue
        if start and num < start:
            continue
        run_step(url, step)
    print("migration steps complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
