"""Command line entry point: `python -m fantasyoptimizer`."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import requests

from . import report
from .config import load_config
from .engine import Optimizer
from .espn import EspnClient
from .state import State

log = logging.getLogger("fantasyoptimizer")


def notify(text: str, url: str) -> None:
    """Push a short summary to ntfy.sh, Discord, or Slack."""
    try:
        if "ntfy" in url:
            requests.post(url, data=text.encode(), headers={"Title": "Fantasy optimizer"}, timeout=15)
        else:
            requests.post(url, json={"content": text[:1900], "text": text}, timeout=15)
    except requests.RequestException as exc:
        log.warning("Notification failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fantasyoptimizer", description=__doc__)
    ap.add_argument("--config", default="optimizer.toml")
    ap.add_argument("--state", default=".state/state.json")
    ap.add_argument("--report", default="report.md")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="send moves to ESPN (overrides DRY_RUN)")
    mode.add_argument("--dry-run", action="store_true", help="never send anything to ESPN")
    ap.add_argument("--demo", action="store_true", help="run against a made-up league, offline")
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--no-waivers", action="store_true")
    ap.add_argument("--no-lineup", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True
    cfg.trades.enabled &= not args.no_trades
    cfg.waivers.enabled &= not args.no_waivers
    cfg.lineup.enabled &= not args.no_lineup

    client = None
    if args.demo:
        from .demo import demo_league
        league = demo_league()
        cfg.dry_run = True
    else:
        if not cfg.league_id:
            ap.error("Set ESPN_LEAGUE_ID (and ESPN_S2 / ESPN_SWID for a private league).")
        client = EspnClient(cfg.league_id, cfg.year, cfg.espn_s2, cfg.swid)
        league = client.load_league(cfg.team_id)

    state = State.load(args.state)
    result = Optimizer(league, cfg, state, client).run()
    if not result.dry_run:
        state.save()

    text = report.render(result)
    print(text)
    Path(args.report).write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    if os.environ.get("NOTIFY_URL"):
        notify(report.short_summary(result), os.environ["NOTIFY_URL"])

    failed = [a for a in result.actions if a.executed and not a.ok]
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
