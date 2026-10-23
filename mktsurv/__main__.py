"""
CLI Entrypoint for `mktsurv`:
  - `python -m mktsurv bench`
  - `python -m mktsurv serve --port 8015`
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

import uvicorn

from .bench import format_benchmark_table, run_benchmark


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mktsurv",
        description="Level-3 LOB Market Manipulation Surveillance Engine",
    )
    sub = parser.add_subparsers(dest="command")

    bench_parser = sub.add_parser("bench", help="Run surveillance benchmark suite")
    bench_parser.add_argument("--seed", type=int, default=42, help="Random seed")
    bench_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    serve_parser = sub.add_parser("serve", help="Run FastAPI surveillance server")
    serve_parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8015, help="Bind port (default 8015)")

    args = parser.parse_args(argv)

    if args.command == "serve":
        uvicorn.run("mktsurv.api:app", host=args.host, port=args.port, reload=False)
        return 0

    # Default or `bench` command
    res = run_benchmark(seed=getattr(args, "seed", 42))
    print(format_benchmark_table(res))
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
