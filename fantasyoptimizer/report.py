"""Markdown summary of a run (printed, saved, and posted to the GitHub job summary)."""
from __future__ import annotations

import datetime as dt

from .engine import RunResult, describe_trade
from .models import IR_SLOT, SLOT_NAMES, League, Player
from .projections import per_game_rate

WEEKS_SHOWN = 3
# How ESPN lists lineup slots: offense, flex spots, defensive players, D/ST, K.
SLOT_DISPLAY_ORDER = [0, 1, 2, 3, 4, 5, 6, 23, 7, 24, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def bye_week(league: League, p: Player) -> int | None:
    games = league.pro_games.get(p.pro_team_id)
    if not p.pro_team_id or not games:
        return None
    return next((sp for sp in range(1, league.final_scoring_period + 1) if not games.get(sp)), None)


def week_projections(result: RunResult, p: Player) -> dict[int, tuple[float, bool]]:
    """Upcoming weeks -> (points, came straight from ESPN)."""
    valuer = result.valuer
    row = valuer.row.get(p.id) if valuer else None
    out = {}
    for j, sp in enumerate(result.league.horizon[:WEEKS_SHOWN]):
        if sp in p.period_projections:
            out[sp] = (p.period_projections[sp], True)
        elif row is not None:
            out[sp] = (float(valuer.points[row, j]), False)
    return out


def _player_cells(result: RunResult, p: Player) -> list[str]:
    league = result.league
    weeks = week_projections(result, p)
    cells = []
    for sp in league.horizon[:WEEKS_SHOWN]:
        pts, espn = weeks.get(sp, (0.0, False))
        if p.pro_team_id and league.games(p.pro_team_id, sp) == 0:
            cells.append("BYE")
        else:
            cells.append(f"{pts:.1f}" if espn else f"~{pts:.1f}")
    bye = bye_week(league, p)
    return cells + [f"{per_game_rate(p, league, result.valuer.cfg):.1f}" if result.valuer else "",
                    str(bye) if bye else ""]


def _slot_order(league: League, p: Player) -> tuple[int, str]:
    if p.lineup_slot in SLOT_DISPLAY_ORDER:
        return SLOT_DISPLAY_ORDER.index(p.lineup_slot), p.name
    return (len(SLOT_DISPLAY_ORDER) + (1 if p.lineup_slot == IR_SLOT else 0)), p.name


def _week_headers(league: League) -> str:
    return " | ".join(f"Wk {sp}" for sp in league.horizon[:WEEKS_SHOWN])


def starts_text(weeks: list[int], total: int) -> str:
    if 0 < len(weeks) <= 3:
        return f"{len(weeks)} of {total} (wk {', '.join(map(str, weeks))})"
    return f"{len(weeks)} of {total}"


def pickup_rows(ideas, limit: int, per_position: int = 2):
    """Best pickups without eight versions of the same move."""
    seen: dict[str, int] = {}
    rows = []
    for i in ideas:
        if seen.get(i.add.position, 0) < per_position:
            seen[i.add.position] = seen.get(i.add.position, 0) + 1
            rows.append(i)
        if len(rows) >= limit:
            break
    return rows


def roster_section(result: RunResult) -> list[str]:
    league = result.league
    if not league.horizon:
        return []
    n = len(league.horizon[:WEEKS_SHOWN])
    lines = ["", "## Your roster", "",
             f"| Slot | Player | NFL | Status | {_week_headers(league)} | ROS/gm | Bye |",
             "|---|---|---|---|" + "---:|" * (n + 2)]
    players = sorted((league.players[pid] for pid in league.my_team.roster),
                     key=lambda p: _slot_order(league, p))
    for p in players:
        slot = SLOT_NAMES.get(p.lineup_slot, "BE") if p.lineup_slot is not None else "BE"
        status = "" if p.injury_status == "ACTIVE" else p.injury_status.replace("_", " ").title()
        if p.lineup_locked:
            status = (status + " (locked)").strip()
        nfl = league.pro_team_abbrevs.get(p.pro_team_id, "")
        lines.append(f"| {slot} | {p.name} ({p.position}) | {nfl} | {status} | "
                     + " | ".join(_player_cells(result, p)) + " |")
    return lines


def free_agent_section(result: RunResult, count: int = 12) -> list[str]:
    league, valuer = result.league, result.valuer
    if not league.horizon or not valuer:
        return []
    pool = sorted((p for p in league.players.values() if p.available),
                  key=lambda p: valuer.ros_points(p.id), reverse=True)[:count]
    n = len(league.horizon[:WEEKS_SHOWN])
    lines = ["", "## Best available players", "",
             f"| Player | NFL | Status | {_week_headers(league)} | ROS/gm | Bye | Rostered |",
             "|---|---|---|" + "---:|" * (n + 3)]
    for p in pool:
        status = "Waivers" if p.status == "WAIVERS" else "FA"
        if p.injury_status != "ACTIVE":
            status += ", " + p.injury_status.replace("_", " ").title()
        nfl = league.pro_team_abbrevs.get(p.pro_team_id, "")
        lines.append(f"| {p.name} ({p.position}) | {nfl} | {status} | "
                     + " | ".join(_player_cells(result, p)) + f" | {p.percent_owned:.0f}% |")
    return lines


def _status(action) -> str:
    if not action.executed:
        return "DRY RUN"
    return "SENT" if action.ok else "FAILED"


def render(result: RunResult, max_rows: int = 8) -> str:
    league, base = result.league, result.baseline
    me = league.my_team
    lines = [
        f"# {league.name}: week {league.current_scoring_period}",
        "",
        ("**Dry run.** Nothing was sent to ESPN. Set `DRY_RUN=false` to go live."
         if result.dry_run else "**Live run.** Moves below were sent to ESPN."),
        "",
        f"**{me.name}** is {me.wins:g}-{me.losses:g}"
        + (f"-{me.ties:g}" if me.ties else "")
        + f". Playoff odds **{_pct(base.playoff_odds[me.id])}**, "
          f"title odds **{_pct(base.title_odds[me.id])}**, "
          f"projected {base.expected_wins[me.id]:.1f} wins.",
        "",
        "## Moves",
    ]
    if result.actions:
        for a in result.actions:
            extra = f" ({a.response})" if a.response else ""
            lines.append(f"- **{_status(a)}** {a.summary}{extra}")
            if a.why:
                lines.append(f"  - Why: {a.why}")
    else:
        lines.append("- None today. Nothing cleared the thresholds.")

    lines += roster_section(result)

    if result.incoming:
        lines += ["", "## Trade offers sent to you", "",
                  "| From | You give | You get | Season pts | Title odds | Verdict |",
                  "|---|---|---|---:|---:|---|"]
        for o in result.incoming:
            give = ", ".join(str(league.players[p]) for p in o.give)
            get = ", ".join(str(league.players[p]) for p in o.get)
            verdict = "Accept" if o.title_gain > 0.25 and o.my_gain > 0 else (
                "Decline" if o.title_gain < 0 or o.my_gain < 0 else "Meh")
            lines.append(f"| {league.team_name(o.partner)} | {give} | {get} | {o.my_gain:+.1f} "
                         f"| {o.title_gain:+.1f}% | {verdict} |")

    if result.add_ideas:
        lines += ["", "## Best pickups", "",
                  "_Alternatives, best first (at most two per position). The bot makes at most "
                  f"{result.max_moves} per run._", "",
                  "| Add | Drop | Type | Starts | Season pts | Title odds | Note |",
                  "|---|---|---|---|---:|---:|---|"]
        for i in pickup_rows(result.add_ideas, max_rows):
            kind = (f"Waiver ${i.bid}" if league.uses_faab else "Waiver") if i.waiver else "Free agent"
            title = f"{i.title_gain:+.1f}%" if i.title_gain is not None else ""
            note = (f"Only fills wk {', '.join(map(str, i.start_weeks))}: add it that week"
                    if i.patch else "")
            lines.append(f"| {i.add} | {i.drop or '(open spot)'} | {kind} | "
                         f"{starts_text(i.start_weeks, len(league.horizon))} | {i.gain:+.1f} | {title} "
                         f"| {note} |")

    if result.trade_ideas:
        lines += ["", "## Best trade ideas", "",
                  "| With | Deal | Your pts | Their pts | Your title odds | Their title odds | Accept chance |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for t in result.trade_ideas[:max_rows]:
            lines.append(f"| {league.team_name(t.partner)} | {describe_trade(league, t)} "
                         f"| {t.my_gain:+.1f} | {t.partner_gain:+.1f} "
                         f"| {t.my_title_gain:+.1f}% | {t.partner_title_gain:+.1f}% "
                         f"| ~{100 * t.accept_chance:.0f}% |")

    lines += free_agent_section(result)

    lines += ["", "## League outlook", "",
              "| Team | Record | Proj. wins | Playoffs | Title |", "|---|---|---:|---:|---:|"]
    for t in sorted(league.teams.values(), key=lambda t: -base.title_odds[t.id]):
        name = f"**{t.name}**" if t.id == me.id else t.name
        lines.append(f"| {name} | {t.wins:g}-{t.losses:g} | {base.expected_wins[t.id]:.1f} "
                     f"| {_pct(base.playoff_odds[t.id])} | {_pct(base.title_odds[t.id])} |")

    if result.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in result.notes]
    lines += ["", "_Weekly numbers are ESPN projections; ~ marks the bot's own estimate (ESPN hasn't "
              "published that week yet). ROS/gm = the bot's rest-of-season points per game. "
              "Season pts = change in projected starting-lineup points over the rest of the "
              "season (playoff weeks weighted up). Title odds = change in simulated championship "
              "probability, in percentage points._"]
    return "\n".join(lines) + "\n"


def short_summary(result: RunResult) -> str:
    league, base = result.league, result.baseline
    me = league.my_team_id
    head = (f"{league.my_team.name}: title odds {_pct(base.title_odds[me])}, "
            f"playoffs {_pct(base.playoff_odds[me])}")
    moves = [f"[{_status(a)}] {a.summary}" for a in result.actions] or ["No moves today."]
    offers = [f"Offer from {league.team_name(o.partner)}: {o.title_gain:+.1f}% title odds"
              for o in result.incoming]
    return "\n".join([head, *moves, *offers])


def snapshot(result: RunResult, free_agents: int = 60) -> dict:
    """Everything from a run as JSON, for looking back or talking it over later."""
    league, base, valuer = result.league, result.baseline, result.valuer

    def player(p: Player) -> dict:
        weeks = week_projections(result, p)
        return {
            "id": p.id, "name": p.name, "position": p.position,
            "nfl_team": league.pro_team_abbrevs.get(p.pro_team_id, ""),
            "fantasy_team": league.team_name(p.team_id) if p.status == "ONTEAM" else None,
            "slot": SLOT_NAMES.get(p.lineup_slot) if p.lineup_slot is not None else None,
            "status": p.status, "injury": p.injury_status, "locked": p.lineup_locked,
            "projections": {str(sp): {"points": round(pts, 2), "source": "espn" if espn else "bot"}
                            for sp, (pts, espn) in weeks.items()},
            "ros_per_game": round(per_game_rate(p, league, valuer.cfg), 2) if valuer else None,
            "ros_points": round(valuer.ros_points(p.id), 1) if valuer else None,
            "season_points": p.season_actual, "season_avg": p.season_average,
            "bye": bye_week(league, p), "percent_rostered": p.percent_owned,
        }

    rostered = [league.players[pid] for t in league.teams.values() for pid in t.roster]
    available = sorted((p for p in league.players.values() if p.available),
                       key=lambda p: valuer.ros_points(p.id) if valuer else p.percent_owned,
                       reverse=True)[:free_agents]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "league": {"id": league.id, "name": league.name, "season": league.year,
                   "week": league.current_scoring_period, "my_team_id": league.my_team_id},
        "dry_run": result.dry_run,
        "teams": [{
            "id": t.id, "name": t.name, "wins": t.wins, "losses": t.losses, "ties": t.ties,
            "points_for": t.points_for, "playoff_odds": round(base.playoff_odds[t.id], 4),
            "title_odds": round(base.title_odds[t.id], 4),
            "projected_wins": round(base.expected_wins[t.id], 2),
        } for t in league.teams.values()],
        "moves": [{"kind": a.kind, "summary": a.summary, "why": a.why,
                   "status": _status(a), "response": a.response} for a in result.actions],
        "pickup_ideas": [{"add": i.add.name, "drop": i.drop.name if i.drop else None,
                          "season_points": round(i.gain, 1), "title_odds": i.title_gain,
                          "waiver": i.waiver, "start_weeks": i.start_weeks,
                          "patch": i.patch} for i in result.add_ideas],
        "trade_ideas": [{"partner": league.team_name(t.partner), "deal": describe_trade(league, t),
                         "my_points": round(t.my_gain, 1), "their_points": round(t.partner_gain, 1),
                         "my_title_odds": t.my_title_gain, "their_title_odds": t.partner_title_gain,
                         "accept_chance": round(t.accept_chance, 2)} for t in result.trade_ideas],
        "players": [player(p) for p in rostered + available],
    }
