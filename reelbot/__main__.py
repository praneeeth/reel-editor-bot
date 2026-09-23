"""python -m reelbot [run | install-skill | metrics]"""

from __future__ import annotations

import argparse
import logging

from .config import Settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="reelbot")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "install-skill", "metrics"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()

    if args.command == "install-skill":
        from .skill import ensure_skill_registered
        for backend in ("claude", "codex"):
            print(f"{backend}: {ensure_skill_registered(backend)}")
    elif args.command == "metrics":
        from .db import DB
        rows = DB(settings.db_path).all_metrics()
        print(f"{'job':<24} {'step':<15} {'backend':<8} {'ok':<3} {'turns':>5} {'in_tok':>8} "
              f"{'out_tok':>8} {'cache_rd':>9} {'cost$':>7} {'wall_s':>7}")
        for m in rows:
            print(f"{m['job_id']:<24} {m['step']:<15} {m['backend']:<8} {m['ok']:<3} "
                  f"{m['turns'] or 0:>5} {m['input_tokens'] or 0:>8} {m['output_tokens'] or 0:>8} "
                  f"{m['cache_read_tokens'] or 0:>9} {m['cost_usd'] or 0:>7.3f} {m['wall_s']:>7.1f}")
    else:
        from .bot import run
        run(settings)


if __name__ == "__main__":
    main()
