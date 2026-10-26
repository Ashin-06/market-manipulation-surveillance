"""
CLI Entrypoint for `mktsurv`:
  - `python -m mktsurv bench`
  - `python -m mktsurv serve`                (settings: MKTSURV_* environment variables)
  - `python -m mktsurv migrate`              (apply database migrations and exit)
  - `python -m mktsurv realdata --data DIR`   (LOBSTER sample files -> results/real_data.json)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .bench import format_benchmark_table, format_sweep, run_benchmark, run_sweep


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mktsurv",
        description="Level-3 LOB Market Manipulation Surveillance Engine",
    )
    sub = parser.add_subparsers(dest="command")

    bench_parser = sub.add_parser("bench", help="Run surveillance benchmark suite")
    bench_parser.add_argument("--seed", type=int, default=42, help="Random seed")
    bench_parser.add_argument("--json", action="store_true", help="Output raw JSON")
    bench_parser.add_argument("--sessions", type=int, default=200, help="Sessions in the multi-seed sweep (0 to skip)")
    bench_parser.add_argument("--inclusion", type=float, default=0.6, help="Probability each episode is injected in the sweep")

    serve_parser = sub.add_parser("serve", help="Run FastAPI surveillance server")
    serve_parser.add_argument("--host", type=str, help="Bind host (default MKTSURV_HOST or 127.0.0.1; others need API keys)")
    serve_parser.add_argument("--port", type=int, help="Bind port (default MKTSURV_PORT or 8015)")
    sub.add_parser("migrate", help="Apply database migrations to MKTSURV_DATABASE_URL")

    real_parser = sub.add_parser("realdata", help="Count false alerts on real Nasdaq order flow (LOBSTER samples)")
    real_parser.add_argument("--data", required=True, help="Folder with LOBSTER *_message_10.csv and *_orderbook_10.csv files")
    real_parser.add_argument("--traders", type=int, nargs="+", default=[10, 50, 200], help="Pseudo-trader counts")
    real_parser.add_argument("--out", default="results/real_data.json")

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.command == "realdata":
        from .realdata import main as realdata

        realdata(args.data, args.traders, args.out)
        return 0

    if args.command in ("serve", "migrate"):
        from . import db, ops

        for name, val in (("HOST", getattr(args, "host", None)), ("PORT", getattr(args, "port", None))):
            if val is not None:
                os.environ[f"MKTSURV_{name}"] = str(val)
        try:
            settings = ops.load_settings()  # fail here, before uvicorn, with a readable message
        except ops.ConfigError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return 2
        if args.command == "migrate":
            print(*(db.migrate(db.engine(settings.database_url)) or ["up to date"]), sep="\n")
            return 0
        import uvicorn

        ops.setup_logging(settings.log_level)
        uvicorn.run("mktsurv.api:app", host=settings.host, port=settings.port, log_config=None, access_log=False)
        return 0

    # Default or `bench` command
    res = run_benchmark(seed=getattr(args, "seed", 42))
    print(format_benchmark_table(res))
    sessions = getattr(args, "sessions", 200)
    if sessions > 0:
        sweep = run_sweep(sessions=sessions, inclusion_prob=getattr(args, "inclusion", 0.6))
        print(format_sweep(sweep))
        res["sweep"] = sweep
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
