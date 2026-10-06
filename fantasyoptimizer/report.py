"""Markdown summary of a run (printed, saved, and posted to the GitHub job summary)."""
from __future__ import annotations

import datetime as dt

from .engine import RunResult, describe_trade
from .models import IR_SLOT, SLOT_NAMES, League, Player
from .projections import per_game_rate
from .waivers import min_gain

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


def lineup_section(result: RunResult) -> list[str]:
    check, league, valuer = result.lineup_check, result.league, result.valuer
    if not check or not valuer:
        return []

    def pts(pid: int, col: int) -> float:
        return float(valuer.points[valuer.row[pid], col])

    def name(pid: int) -> str:
        return league.players[pid].name

    lines = ["", "## Lineup check", ""]
    if check.swaps:
        done = (not result.dry_run
                and any(a.kind == "lineup" and a.ok for a in result.actions))
        lines.append(f"**Week {check.week}: swaps worth +{check.gain:.1f} projected points**"
                     + (" (the bot already made them)" if done else ""))
        lines += [f"- Start **{name(a)}** ({pts(a, 0):.1f}) over **{name(b)}** ({pts(b, 0):.1f})"
                  for a, b in check.swaps]
    else:
        lines.append(f"**Week {check.week}:** your lineup is already your best one "
                     f"({check.best:.1f} projected points).")
    if check.next_week:
        lines.append("")
        if not check.holes:
            lines.append(f"**Week {check.next_week}:** no byes or injuries among your starters.")
        else:
            lines.append(f"**Week {check.next_week} heads-up:**")
            for h in check.holes:
                text = f"- **{name(h.player)}** ({league.players[h.player].position}) is {h.reason}"
                if h.fill is not None:
                    text += f": start **{name(h.fill)}** ({pts(h.fill, 1):.1f}) instead."
                elif h.free_agent is not None:
                    fa = league.players[h.free_agent]
                    where = "on waivers" if fa.status == "WAIVERS" else "a free agent"
                    text += (f", and nobody on your bench can play {SLOT_NAMES.get(h.slot, h.slot)}. "
                             f"Best pickup: **{fa.name}** ({pts(fa.id, 1):.1f}, {where}).")
                else:
                    text += ", and there's nobody to fill in."
                lines.append(text)
    return lines


def asked_trade_section(result: RunResult) -> list[str]:
    league, t = result.league, result.asked_trade
    if result.asked_trade_error:
        return ["", "## The trade you asked about", "", f"Couldn't score it: {result.asked_trade_error}"]
    if not t:
        return []
    if t.my_title_gain > 0.25 and t.my_gain > 0:
        verdict = "**Worth sending.**"
    elif t.my_gain > 0 and t.my_title_gain > 0:
        verdict = "**Small win for you.** Fine to send, but it won't move the needle much."
    else:
        verdict = "**Not worth it for you.**"
    lines = ["", "## The trade you asked about", "",
             f"**{league.team_name(t.partner)}**: {describe_trade(league, t)}", "",
             f"{verdict} You: {t.my_gain:+.1f} season pts, {t.my_title_gain:+.1f}% title odds. "
             f"Them: {t.partner_gain:+.1f} season pts, {t.partner_title_gain:+.1f}% title odds. "
             f"Accept chance ~{100 * t.accept_chance:.0f}%."]
    if t.partner_drops:
        drops = ", ".join(league.players[pid].name for pid in t.partner_drops)
        lines.append(f"They'd have to drop {drops} to fit everyone.")
    if t.pitch:
        lines.append(f"\nPitch: \"{t.pitch}\"")
    if result.asked_trade_why:
        lines.append(f"\nWhy: {result.asked_trade_why}")
    return lines


def trade_list(league: League, ideas) -> list[str]:
    lines = []
    for n, t in enumerate(ideas, 1):
        lines.append(f"{n}. **{league.team_name(t.partner)}**: {describe_trade(league, t)}")
        lines.append(f"   - You: {t.my_gain:+.1f} season pts, {t.my_title_gain:+.1f}% title odds. "
                     f"Them: {t.partner_gain:+.1f} season pts. Accept chance ~{100 * t.accept_chance:.0f}%.")
        if t.pitch:
            lines.append(f"   - Pitch: \"{t.pitch}\"")
    return lines


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


POSITION_ORDER = ["QB", "RB", "WR", "TE", "DT", "DE", "LB", "CB", "S", "D/ST", "K", "P"]


def available_by_position(result: RunResult, per_position: int) -> list[Player]:
    """Best free agents at each position you can start, instead of one big list."""
    league, valuer = result.league, result.valuer
    groups: dict[str, list[Player]] = {}
    for p in league.players.values():
        row = valuer.row.get(p.id)
        if p.available and row is not None and valuer.can_start[row]:
            groups.setdefault(p.position, []).append(p)
    order = sorted(groups, key=lambda pos: POSITION_ORDER.index(pos) if pos in POSITION_ORDER else 99)
    out = []
    for pos in order:
        out += sorted(groups[pos], key=lambda p: valuer.ros_points(p.id), reverse=True)[:per_position]
    return out


def free_agent_section(result: RunResult, per_position: int = 3) -> list[str]:
    league, valuer = result.league, result.valuer
    if not league.horizon or not valuer:
        return []
    n = len(league.horizon[:WEEKS_SHOWN])
    roster = list(league.my_team.roster)
    lines = ["", f"## Best available players (top {per_position} per position)", "",
             "_Would start = weeks he'd make your best lineup if you added him._", "",
             f"| Pos | Player | NFL | Status | {_week_headers(league)} | ROS/gm | Bye | Rostered "
             "| Would start |",
             "|---|---|---|---|" + "---:|" * (n + 3) + "---|"]
    for p in available_by_position(result, per_position):
        status = "Waivers" if p.status == "WAIVERS" else "FA"
        if p.injury_status != "ACTIVE":
            status += ", " + p.injury_status.replace("_", " ").title()
        nfl = league.pro_team_abbrevs.get(p.pro_team_id, "")
        starts = starts_text(valuer.lineup_weeks(valuer.streaming(roster + [p.id]), p.id),
                             len(league.horizon))
        lines.append(f"| {p.position} | {p.name} | {nfl} | {status} | "
                     + " | ".join(_player_cells(result, p)) + f" | {p.percent_owned:.0f}% | {starts} |")
    return lines


def _status(action) -> str:
    if not action.executed:
        return "DRY RUN"
    return "SENT" if action.ok else "FAILED"


def _auto_streams(result: RunResult) -> list[str]:
    cfg = result.valuer.cfg if result.valuer else None
    return list(cfg.streaming.positions) if cfg and cfg.streaming.auto else []


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
    ]
    lines += lineup_section(result)
    lines += asked_trade_section(result)
    lines += ["", "## Moves"]
    if result.move_error:
        lines.append(f"- **Couldn't make the move you asked for:** {result.move_error}")
    if result.actions:
        for a in result.actions:
            extra = f" ({a.response})" if a.response else ""
            lines.append(f"- **{_status(a)}** {a.summary}{extra}")
            if a.why:
                lines.append(f"  - Why: {a.why}")
            if a.pitch:
                lines.append(f"  - Pitch to send them: \"{a.pitch}\"")
    for claim in league.pending_claims:
        add = league.players.get(claim.add)
        drop = league.players.get(claim.drop)
        lines.append(f"- **PENDING** Claim on ESPN: {add or claim.add}"
                     + (f", drop {drop or claim.drop}" if claim.drop is not None else ""))
    if not (result.actions or result.move_error or league.pending_claims):
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
                  "_Alternatives, best first (at most two per position). "
                  + (f"The bot makes at most {result.max_moves} per run._" if result.max_moves
                     else "Suggestions only: the bot won't make any (`max_moves_per_run = 0`)"
                     + (f", except {'/'.join(streams)} streams, which it makes itself._"
                        if (streams := _auto_streams(result)) else "._")),
                  "",
                  "| Add | Drop | Type | Starts | Season pts | Title odds | Note |",
                  "|---|---|---|---|---:|---:|---|"]
        for i in pickup_rows(result.add_ideas, max_rows):
            kind = (f"Waiver ${i.bid}" if league.uses_faab else "Waiver") if i.waiver else "Free agent"
            title = f"{i.title_gain:+.1f}%" if i.title_gain is not None else ""
            if i.patch:
                note = f"Only fills wk {', '.join(map(str, i.start_weeks))}: add it that week"
            elif i.wait:
                note = f"Next week's stream: make it after {i.drop.name} plays this week"
            elif result.valuer and i.gain < min_gain(i, result.valuer.cfg):
                note = "Small gain: optional (the bot won't make it)"
            else:
                note = ""
            lines.append(f"| {i.add} | {i.drop or '(open spot)'} | {kind} | "
                         f"{starts_text(i.start_weeks, len(league.horizon))} | {i.gain:+.1f} | {title} "
                         f"| {note} |")

    if result.trade_ideas:
        lines += ["", "## Best trade ideas", "",
                  "_Good for you and good for them: the ones most likely to happen._", ""]
        lines += trade_list(league, result.trade_ideas[:5])

    if result.long_shots:
        lines += ["", "## Long shots", "",
                  "_Better for you, less likely to be accepted. Some even look slightly worse for "
                  "them on paper, so the pitch matters. Worst case, they say no._", ""]
        lines += trade_list(league, result.long_shots[:5])

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
    if result.valuer and result.valuer.stream_level:
        levels = ", ".join(f"{pos} ~{level:.1f} pts" for pos, level in result.valuer.stream_level.items())
        lines += ["", f"_Streamed each week: {levels}. For weeks ESPN hasn't projected, the bot "
                  "assumes you'll pick up a free agent worth what the best one has been projected "
                  "at lately, so holding one all season is worth little._"]
    return "\n".join(lines) + "\n"


def short_summary(result: RunResult) -> str:
    league, base = result.league, result.baseline
    me = league.my_team_id
    head = (f"{league.my_team.name}: title odds {_pct(base.title_odds[me])}, "
            f"playoffs {_pct(base.playoff_odds[me])}")
    moves = [f"[{_status(a)}] {a.summary}" for a in result.actions] or ["No moves today."]
    check = result.lineup_check
    if check and check.swaps:
        moves.insert(0, f"Lineup: {len(check.swaps)} swap(s) worth +{check.gain:.1f} pts this week")
    if check and check.holes:
        names = ", ".join(league.players[h.player].name for h in check.holes)
        moves.insert(0, f"Week {check.next_week} heads-up: {names} can't play")
    offers = [f"Offer from {league.team_name(o.partner)}: {o.title_gain:+.1f}% title odds"
              for o in result.incoming]
    return "\n".join([head, *moves, *offers])


def snapshot(result: RunResult, free_agents_per_position: int = 10) -> dict:
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

    def trade_json(t) -> dict:
        return {"partner": league.team_name(t.partner), "deal": describe_trade(league, t),
                "pitch": t.pitch, "my_points": round(t.my_gain, 1),
                "their_points": round(t.partner_gain, 1), "my_title_odds": t.my_title_gain,
                "their_title_odds": t.partner_title_gain, "accept_chance": round(t.accept_chance, 2)}

    rostered = [league.players[pid] for t in league.teams.values() for pid in t.roster]
    available = available_by_position(result, free_agents_per_position) if valuer else []
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
        "asked_trade": (trade_json(result.asked_trade) if result.asked_trade
                        else {"error": result.asked_trade_error} if result.asked_trade_error else None),
        "lineup_check": None if not result.lineup_check else {
            "week": result.lineup_check.week,
            "best_points": round(result.lineup_check.best, 1),
            "gain": round(result.lineup_check.gain, 1),
            "swaps": [{"start": league.players[a].name, "sit": league.players[b].name}
                      for a, b in result.lineup_check.swaps],
            "next_week": result.lineup_check.next_week,
            "holes": [{"player": league.players[h.player].name, "reason": h.reason,
                       "fill": league.players[h.fill].name if h.fill is not None else None,
                       "free_agent": (league.players[h.free_agent].name
                                      if h.free_agent is not None else None)}
                      for h in result.lineup_check.holes],
        },
        "moves": [{"kind": a.kind, "summary": a.summary, "why": a.why, "pitch": a.pitch,
                   "status": _status(a), "response": a.response} for a in result.actions],
        "pickup_ideas": [{"add": i.add.name, "drop": i.drop.name if i.drop else None,
                          "season_points": round(i.gain, 1), "title_odds": i.title_gain,
                          "waiver": i.waiver, "start_weeks": i.start_weeks,
                          "patch": i.patch, "wait": i.wait} for i in result.add_ideas],
        "streaming": ({pos: round(level, 2) for pos, level in valuer.stream_level.items()}
                      if valuer else {}),
        "trade_ideas": [trade_json(t) for t in result.trade_ideas],
        "long_shots": [trade_json(t) for t in result.long_shots],
        "players": [player(p) for p in rostered + available],
    }
