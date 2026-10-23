"""
CLI Entrypoint for `mktsurv`:
  - `python -m mktsurv bench`
  - `python -m mktsurv serve --port 8015`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

import uvicorn

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
    serve_parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8015, help="Bind port (default 8015)")

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.command == "serve":
        if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("MKTSURV_API_KEY"):
            print("Refusing to listen on a non-loopback address without MKTSURV_API_KEY set.", file=sys.stderr)
            return 2
        uvicorn.run("mktsurv.api:app", host=args.host, port=args.port, reload=False)
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
