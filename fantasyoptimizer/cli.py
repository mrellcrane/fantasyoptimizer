"""Command line entry point: `python -m fantasyoptimizer`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import requests

from . import report, watch
from .config import load_config
from .engine import Optimizer
from .espn import EspnClient, EspnError
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
    ap.add_argument("--snapshot", default="snapshot.json", help="full run data as JSON")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="send moves to ESPN (overrides DRY_RUN)")
    mode.add_argument("--dry-run", action="store_true", help="never send anything to ESPN")
    ap.add_argument("--demo", action="store_true", help="run against a made-up league, offline")
    ap.add_argument("--score-trade", default=os.environ.get("TRADE", ""),
                    help='score one trade: "give A, B; get C, D"')
    ap.add_argument("--move", default=os.environ.get("MOVE", ""),
                    help='make one add/drop: "add Jets D/ST, drop Bills D/ST"')
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--no-waivers", action="store_true")
    ap.add_argument("--no-lineup", action="store_true")
    ap.add_argument("--pregame", action="store_true",
                    help="lineup only, and only if one of your players kicks off soon "
                         "(after inactives are out); writes no report unless it changes something")
    ap.add_argument("--watch", action="store_true",
                    help="check free agents and ping (NOTIFY_URL) when a pickup worth making "
                         "shows up; never makes moves. Combine with --pregame for the "
                         "15-minute check")
    ap.add_argument("--watch-state", default=".state/watch.json",
                    help="which pickups --watch already pinged about")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True
    cfg.trades.enabled &= not (args.no_trades or args.pregame)
    cfg.waivers.enabled &= not (args.no_waivers or args.pregame)
    cfg.lineup.enabled &= not args.no_lineup

    client = None
    if args.demo:
        from .demo import demo_league
        league = demo_league()
        cfg.dry_run = True
    else:
        if not cfg.league_id:
            msg = "Set ESPN_LEAGUE_ID (and ESPN_S2 / ESPN_SWID for a private league)."
            if os.environ.get("GITHUB_ACTIONS"):
                # Not set up yet: skip quietly instead of failing (and emailing) every day.
                print(f"::warning::Skipping run. {msg} See README.md.")
                return 0
            ap.error(msg)
        client = EspnClient(cfg.league_id, cfg.year, cfg.espn_s2, cfg.swid)
        try:
            league = client.load_league(cfg.team_id)
        except EspnError as exc:
            if exc.status in (401, 403) and not cfg.espn_s2 and os.environ.get("GITHUB_ACTIONS"):
                print("::warning::Skipping run. The league is private: add the ESPN_S2 and "
                      "ESPN_SWID secrets. See README.md.")
                return 0
            raise

    # Quick checks (--pregame, --watch) run every 15 minutes and stay quiet unless
    # they find something.
    quick = args.pregame or args.watch
    streams_auto = cfg.streaming.auto  # the daily run makes those pickups; don't ping
    soon: list[int] = []
    if args.pregame:
        soon = league.kicking_off(league.my_team.roster, dt.timedelta(minutes=cfg.lineup.pregame_minutes))
        if soon:
            print("Pregame check: kicking off soon: "
                  + ", ".join(league.players[pid].name for pid in soon))
        else:
            print(f"Pregame check: none of your players kick off in the next "
                  f"{cfg.lineup.pregame_minutes} minutes.")
    if quick:
        cfg.lineup.enabled &= bool(soon)
        cfg.trades.enabled = False
        cfg.waivers.enabled = args.watch
        if args.watch:  # alerts only: you make the pickup
            cfg.waivers.max_moves_per_run = 0
            cfg.streaming.auto = False
        if not cfg.lineup.enabled and not args.watch:
            print("Nothing to do.")
            return 0

    state = State.load(args.state)
    result = Optimizer(league, cfg, state, client, score_trade="" if quick else args.score_trade,
                       move="" if quick else args.move).run()
    if not result.dry_run:
        state.save()

    alerts = []
    if args.watch:
        now = dt.datetime.now(dt.timezone.utc)
        memory = watch.WatchMemory.load(args.watch_state)
        worth = watch.worth_it(result, cfg, skip_streams=streams_auto)
        alerts = watch.new_alerts(result, cfg, memory, now, skip_streams=streams_auto)
        if alerts:
            print(watch.alert_text(result, alerts))
            memory.record(alerts, now)
            memory.save()
            if os.environ.get("GITHUB_OUTPUT"):  # the workflow saves the memory
                with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                    fh.write("watch_changed=true\n")
        elif worth:
            print("Free agent watch: already pinged about "
                  + ", ".join(m.add.name for m in worth) + ".")
        else:
            print("Free agent watch: no pickup worth making right now.")
    if soon and not result.actions:
        print("Pregame check: your lineup is already the best one.")
    if quick and not result.actions and not alerts:
        return 0

    text = report.render(result)
    print(text)
    Path(args.report).write_text(text)
    Path(args.snapshot).write_text(json.dumps(report.snapshot(result), indent=1))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    if os.environ.get("GITHUB_OUTPUT"):  # how the workflow files this report
        kind = "" if not quick else "pregame" if result.actions else "watch"
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"report_kind={kind}\n")
    ping = [report.short_summary(result)] if result.actions or not quick else []
    if alerts:
        ping.append(watch.alert_text(result, alerts))
    if os.environ.get("NOTIFY_URL"):
        notify("\n\n".join(ping), os.environ["NOTIFY_URL"])
    elif alerts:
        log.warning("Set the NOTIFY_URL secret to get pickup alerts on your phone.")

    failed = [a for a in result.actions if a.executed and not a.ok]
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
