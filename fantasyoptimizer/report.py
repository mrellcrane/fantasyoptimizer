"""Markdown summary of a run (printed, saved, and posted to the GitHub job summary)."""
from __future__ import annotations

from .engine import RunResult, describe_trade


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


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
                  "| Add | Drop | Type | Season pts | Title odds |",
                  "|---|---|---|---:|---:|"]
        for i in result.add_ideas[:max_rows]:
            kind = (f"Waiver ${i.bid}" if league.uses_faab else "Waiver") if i.waiver else "Free agent"
            title = f"{i.title_gain:+.1f}%" if i.title_gain is not None else ""
            lines.append(f"| {i.add} | {i.drop or '(open spot)'} | {kind} | {i.gain:+.1f} | {title} |")

    if result.trade_ideas:
        lines += ["", "## Best trade ideas", "",
                  "| With | Deal | Your pts | Their pts | Your title odds | Their title odds | Accept chance |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for t in result.trade_ideas[:max_rows]:
            lines.append(f"| {league.team_name(t.partner)} | {describe_trade(league, t)} "
                         f"| {t.my_gain:+.1f} | {t.partner_gain:+.1f} "
                         f"| {t.my_title_gain:+.1f}% | {t.partner_title_gain:+.1f}% "
                         f"| ~{100 * t.accept_chance:.0f}% |")

    lines += ["", "## League outlook", "",
              "| Team | Record | Proj. wins | Playoffs | Title |", "|---|---|---:|---:|---:|"]
    for t in sorted(league.teams.values(), key=lambda t: -base.title_odds[t.id]):
        name = f"**{t.name}**" if t.id == me.id else t.name
        lines.append(f"| {name} | {t.wins:g}-{t.losses:g} | {base.expected_wins[t.id]:.1f} "
                     f"| {_pct(base.playoff_odds[t.id])} | {_pct(base.title_odds[t.id])} |")

    if result.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in result.notes]
    lines += ["", "_Season pts = change in projected starting-lineup points over the rest of the "
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
