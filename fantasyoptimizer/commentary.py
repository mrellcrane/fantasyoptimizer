"""The season story: fun color commentary at the top of the report.

Two halves. `season_story` gathers the facts (weekly scores, luck, form, injuries,
schedule, what has to happen to make the playoffs). `write` turns them into a few
paragraphs: Claude writes them when ANTHROPIC_API_KEY is set, and a plain template
does it otherwise (or if the API call fails), so the report never goes without.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
from pathlib import Path

from .models import IR_SLOT, League, Matchup

log = logging.getLogger(__name__)

CLOSE_GAME = 10.0      # points
RECENT_WEEKS = 3


# ---------------------------------------------------------------- facts

def _played(league: League) -> list[Matchup]:
    """Regular-season and playoff games with both scores in, final or not."""
    return [m for m in league.schedule
            if m.away is not None and m.home_points is not None and m.away_points is not None
            and (m.winner != "UNDECIDED" or m.home_points + m.away_points > 0)]


def _final(m: Matchup) -> bool:
    return m.winner != "UNDECIDED"


def _result(m: Matchup, team: int) -> str:
    if m.winner == "TIE":
        return "T"
    won = (m.winner == "HOME") == (m.home == team)
    return "W" if won else "L"


def _week_scores(league: League) -> dict[int, dict[int, float]]:
    """Final weeks only: matchup period -> team -> points."""
    weeks: dict[int, dict[int, float]] = {}
    for m in _played(league):
        if _final(m):
            weeks.setdefault(m.period, {})[m.home] = m.home_points
            weeks.setdefault(m.period, {})[m.away] = m.away_points
    return weeks


def _rank(values: dict[int, float], team: int, high_is_1: bool = True) -> int:
    ordered = sorted(values, key=lambda t: -values[t] if high_is_1 else values[t])
    return ordered.index(team) + 1


def _streak(results: list[str]) -> str:
    if not results:
        return ""
    last, n = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        n += 1
    return f"{last}{n}"


def _win_chance(mine: float, theirs: float, sd_fraction: float, min_sd: float) -> float:
    sd = math.hypot(max(min_sd, sd_fraction * mine), max(min_sd, sd_fraction * theirs))
    return 0.5 * (1 + math.erf((mine - theirs) / (sd * math.sqrt(2))))


def _week_over(league: League) -> bool:
    """Has every NFL game of the current scoring period kicked off more than 4 hours ago?"""
    sp = league.current_scoring_period
    kicks = [k[sp] for k in league.kickoffs.values() if sp in k]
    return bool(kicks) and league.loaded_at > max(kicks) + dt.timedelta(hours=4)


def _standings(league: League) -> list[int]:
    return sorted(league.teams, key=lambda t: (-(league.teams[t].wins + 0.5 * league.teams[t].ties),
                                               -league.teams[t].points_for))


def load_odds_history(folder: str | Path | None, team_id: int) -> list[dict]:
    """Your odds after each past daily run, from the reports branch's history folder."""
    if not folder or not Path(folder).is_dir():
        return []
    rows = []
    for path in sorted(Path(folder).glob("*.json")):
        if path.stem.endswith("pregame"):
            continue
        try:
            data = json.loads(path.read_text())
            me = next(t for t in data["teams"] if t["id"] == team_id)
            rows.append({"date": data["generated_at"][:10], "week": data["league"]["week"],
                         "record": f"{me['wins']:g}-{me['losses']:g}",
                         "playoff_odds": me["playoff_odds"], "title_odds": me["title_odds"]})
        except (ValueError, KeyError, StopIteration, TypeError):
            continue
    by_date = {r["date"]: r for r in rows}   # last run of each day
    return [by_date[d] for d in sorted(by_date)]


def season_story(result, odds_history: list[dict] | None = None) -> dict:
    """Everything worth talking about, as plain JSON-able facts."""
    league, base = result.league, result.baseline
    me = league.my_team_id
    team = league.my_team
    cfg = result.valuer.cfg if result.valuer else None
    pct = lambda x: round(100 * x, 1)  # noqa: E731
    name = league.team_name

    # --- game log
    weeks = _week_scores(league)
    log_rows, results = [], []
    in_progress = None
    for m in sorted((m for m in _played(league) if me in (m.home, m.away)), key=lambda m: m.period):
        opp = m.opponent(me)
        mine, theirs = m.points(me), m.points(opp)
        if not _final(m):
            in_progress = {"week": m.period, "opponent": name(opp), "you": round(mine, 1),
                           "them": round(theirs, 1),
                           "status": ("every game has been played; ESPN hasn't made it final yet"
                                      if _week_over(league) else "games still being played")}
            continue
        week = weeks[m.period]
        beat = sum(1 for t, p in week.items() if t != me and mine > p)
        res = _result(m, me)
        results.append(res)
        log_rows.append({
            "week": m.period, "opponent": name(opp), "you": round(mine, 1), "them": round(theirs, 1),
            "result": res, "margin": round(mine - theirs, 1),
            "league_rank_that_week": _rank(week, me), "would_have_beaten": f"{beat} of {len(week) - 1}",
            "playoffs": m.period > league.reg_season_periods,
        })

    # --- luck and scoring
    pf = {t: 0.0 for t in league.teams}
    pa = {t: 0.0 for t in league.teams}
    for m in _played(league):
        if _final(m):
            pf[m.home] += m.home_points
            pf[m.away] += m.away_points
            pa[m.home] += m.away_points
            pa[m.away] += m.home_points
    ap_w = ap_l = 0
    for wk in weeks.values():
        if me in wk:
            ap_w += sum(1 for t, p in wk.items() if t != me and wk[me] > p)
            ap_l += sum(1 for t, p in wk.items() if t != me and wk[me] < p)
    games = len(log_rows)
    expected = games * ap_w / (ap_w + ap_l) if ap_w + ap_l else 0.0
    wins = sum(1 for r in results if r == "W") + 0.5 * sum(1 for r in results if r == "T")
    scores = [r["you"] for r in log_rows]
    recent = scores[-RECENT_WEEKS:]
    team_recent = {}
    for t in league.teams:
        pts = [wk[t] for _, wk in sorted(weeks.items()) if t in wk][-RECENT_WEEKS:]
        if pts:
            team_recent[t] = sum(pts) / len(pts)
    league_avg = (sum(p for wk in weeks.values() for p in wk.values())
                  / max(1, sum(len(wk) for wk in weeks.values())))
    luck = {
        "all_play_record": f"{ap_w}-{ap_l}",
        "all_play_win_pct": round(ap_w / (ap_w + ap_l), 3) if ap_w + ap_l else None,
        "expected_wins_from_scoring": round(expected, 1),
        "actual_wins": wins,
        "luck_wins": round(wins - expected, 1),  # negative = unlucky
        "points_against": round(pa[me], 1),
        "points_against_rank": _rank(pa, me),     # 1 = most points scored against you
        "losses_while_top_half_scorer": sum(1 for r in log_rows if r["result"] == "L"
                                            and r["league_rank_that_week"] <= len(league.teams) // 2),
        "wins_while_bottom_half_scorer": sum(1 for r in log_rows if r["result"] == "W"
                                             and r["league_rank_that_week"] > len(league.teams) // 2),
        "close_losses": sum(1 for r in log_rows if r["result"] == "L" and -r["margin"] < CLOSE_GAME),
        "close_wins": sum(1 for r in log_rows if r["result"] == "W" and r["margin"] < CLOSE_GAME),
    }
    scoring = {
        "points_for": round(pf[me], 1),
        "points_for_rank": _rank(pf, me),
        "per_week": round(sum(scores) / len(scores), 1) if scores else None,
        "league_average_per_week": round(league_avg, 1),
        f"last_{RECENT_WEEKS}_weeks_per_week": round(sum(recent) / len(recent), 1) if recent else None,
        f"last_{RECENT_WEEKS}_weeks_rank": _rank(team_recent, me) if me in team_recent else None,
        "before_that_per_week": (round(sum(scores[:-RECENT_WEEKS]) / len(scores[:-RECENT_WEEKS]), 1)
                                 if len(scores) > RECENT_WEEKS else None),
        "best_week": max(log_rows, key=lambda r: r["you"], default=None),
        "worst_week": min(log_rows, key=lambda r: r["you"], default=None),
    }

    # --- roster: stars and the injured
    roster = [league.players[pid] for pid in team.roster]
    team_games = max(1, games)
    hurt = []
    for p in roster:
        if p.injury_status in ("ACTIVE", "NORMAL") and p.lineup_slot != IR_SLOT:
            continue
        hurt.append({"player": p.name, "position": p.position,
                     "status": p.injury_status.replace("_", " ").title(),
                     "season_avg": round(p.season_average, 1) if p.season_average else None,
                     "games_played": round(p.games_played),
                     "team_games": team_games,
                     "on_ir_slot": p.lineup_slot == IR_SLOT})
    hurt.sort(key=lambda h: -(h["season_avg"] or 0))
    stars = sorted((p for p in roster if p.season_actual), key=lambda p: -p.season_actual)[:4]
    top = [{"player": p.name, "position": p.position, "season_points": round(p.season_actual, 1),
            "per_game": round(p.season_average, 1) if p.season_average else None} for p in stars]

    # --- what's left
    means = result.period_means or {}
    upcoming = []
    for m in sorted(league.schedule, key=lambda m: m.period):
        if (me not in (m.home, m.away) or m.away is None or _final(m)
                or m.period > league.reg_season_periods or m.period < league.current_matchup_period):
            continue
        opp = m.opponent(me)
        ot = league.teams[opp]
        row = {"week": m.period, "opponent": name(opp), "their_record": f"{ot.wins:g}-{ot.losses:g}",
               "their_playoff_odds": pct(base.playoff_odds[opp])}
        if m.period in means.get(me, {}) and m.period in means.get(opp, {}) and cfg:
            mine, theirs = means[me][m.period], means[opp][m.period]
            row.update(you_projected=round(mine, 1), them_projected=round(theirs, 1),
                       win_chance=pct(_win_chance(mine, theirs, cfg.sim.sd_fraction, cfg.sim.min_sd)))
        upcoming.append(row)

    order = _standings(league)
    cut = league.playoff_team_count
    standings = [{"seed": i + 1, "team": name(t), "you": t == me,
                  "record": f"{league.teams[t].wins:g}-{league.teams[t].losses:g}",
                  "points_for": round(league.teams[t].points_for, 1),
                  "playoff_odds": pct(base.playoff_odds[t]), "title_odds": pct(base.title_odds[t])}
                 for i, t in enumerate(order)]
    my_seed = order.index(me) + 1
    last_in = league.teams[order[cut - 1]] if cut <= len(order) else None
    games_back = ((last_in.wins - team.wins) + (team.losses - last_in.losses)) / 2 if last_in else 0

    sc = result.scenarios
    scenarios = None
    if sc and sc.games_left:
        scenarios = {
            "regular_season_games_left": sc.games_left,
            "by_your_wins": [{"your_wins": w, "chance_it_happens": pct(c), "playoff_odds": pct(po),
                              "title_odds": pct(to)}
                             for w, (c, po, to) in sorted(sc.by_wins.items(), reverse=True)],
            "root_for": [{"week": r.period, "root_for": name(r.root_for), "against": name(r.against),
                          "your_playoff_odds_swing": pct(r.swing)}
                         for r in sc.rooting
                         if r.swing >= 0.02 and r.period <= league.current_matchup_period + 1][:4],
            "likely_seeds": {str(s): pct(c) for s, c in sc.seeds.items() if c >= 0.03},
        }

    # --- how the odds have moved
    trend = None
    if odds_history:
        today = league.loaded_at.date()
        week_ago = [r for r in odds_history
                    if dt.date.fromisoformat(r["date"]) <= today - dt.timedelta(days=6)]
        peak = max(odds_history, key=lambda r: r["playoff_odds"])
        low = min(odds_history, key=lambda r: r["playoff_odds"])
        by_week = {}
        for r in odds_history:
            by_week[r["week"]] = r
        trend = {
            "playoff_odds_by_week": {str(w): {"record": r["record"], "playoff_odds": pct(r["playoff_odds"]),
                                              "title_odds": pct(r["title_odds"])}
                                     for w, r in sorted(by_week.items())},
            "a_week_ago": ({"playoff_odds": pct(week_ago[-1]["playoff_odds"]),
                            "title_odds": pct(week_ago[-1]["title_odds"]),
                            "record": week_ago[-1]["record"]} if week_ago else None),
            "season_high": {"playoff_odds": pct(peak["playoff_odds"]), "week": peak["week"],
                            "record": peak["record"]},
            "season_low": {"playoff_odds": pct(low["playoff_odds"]), "week": low["week"],
                           "record": low["record"]},
        }

    hottest = max(team_recent, key=team_recent.get) if team_recent else None
    return {
        "team": team.name,
        "league": league.name,
        "teams_in_league": len(league.teams),
        "playoff_spots": cut,
        "regular_season_weeks": league.reg_season_periods,
        "current_week": league.current_matchup_period,
        "in_playoffs_now": league.current_matchup_period > league.reg_season_periods,
        "record": f"{team.wins:g}-{team.losses:g}" + (f"-{team.ties:g}" if team.ties else ""),
        "streak": _streak(results),
        "standing": my_seed,
        "games_out_of_last_playoff_spot": games_back if my_seed > cut else None,
        "games_ahead_of_first_team_out": (
            ((team.wins - league.teams[order[cut]].wins) + (league.teams[order[cut]].losses - team.losses)) / 2
            if my_seed <= cut < len(order) else None),
        "playoff_odds": pct(base.playoff_odds[me]),
        "title_odds": pct(base.title_odds[me]),
        "projected_final_wins": round(base.expected_wins[me], 1),
        "game_log": log_rows,
        "this_week_so_far": in_progress,
        "luck": luck,
        "scoring": scoring,
        "injured_or_out": hurt,
        "top_scorers": top,
        "schedule_left": upcoming[:6],
        "what_has_to_happen": scenarios,
        "standings": standings,
        "hottest_team_lately": name(hottest) if hottest else None,
        "odds_trend": trend,
        "bot_moves_today": [a.summary for a in result.actions],
    }


# ---------------------------------------------------------------- writing

SYSTEM_PROMPT = """\
You write the opening segment of a daily fantasy football report for one manager, \
about their own team. Think of a sharp, funny color commentator who has watched \
every one of their games: warm, a little roasty, never mean, and honest about \
where things stand.

You get the facts as JSON. Write 3 or 4 short paragraphs (about 200 to 300 words in \
all) that cover, in whatever order tells the best story:
- What just happened: the latest game (or the one in progress), with the score.
- How the season has gone: the arc of the record, hot and cold stretches, standout \
players, and injuries that changed things.
- Luck: compare the record with how they've actually scored (all-play record, \
points against, close losses, losses while scoring in the top half). Call out good \
or bad luck when the numbers back it up, and say so plainly when it's been fair.
- The playoff picture: odds, where they sit against the cut line, and what has to \
happen. Use the scenarios: how many wins they need, and which other games to root in.

Rules:
- Talk to the manager as "you". Name players and opponents.
- Use only the facts given. Don't invent stats, injuries, trades or quotes. If a \
fact isn't there, leave it out. Round numbers the way a person would say them.
- Odds are percentages. "luck_wins" below zero means they've won fewer games than \
their scoring deserved.
- Plain prose, no headings, no bullet lists. Bold at most two or three key phrases.
- Never use em dashes or en dashes. Use commas, periods, or parentheses instead.
- Don't open with "Well," or a greeting, and don't sign off.
"""


def write(story: dict, model: str = "claude-opus-5-5", effort: str = "medium",
          client=None) -> tuple[str, str]:
    """(commentary, who wrote it: "claude" or "template")."""
    if client is None and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(timeout=180.0, max_retries=2)
        except ImportError:
            log.warning("ANTHROPIC_API_KEY is set but the anthropic package isn't installed")
    if client is not None:
        try:
            text = _ask_claude(client, story, model, effort)
            if text:
                return text, "claude"
        except Exception as exc:  # noqa: BLE001 - the report must never fail over this
            log.warning("Commentary from Claude failed (%s); using the template", exc)
    return template(story), "template"


def _ask_claude(client, story: dict, model: str, effort: str) -> str:
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content":
                   "Here's where the team stands today. Write the segment.\n\n"
                   + json.dumps(story, indent=1, default=str)}],
    )
    if response.stop_reason == "refusal":
        log.warning("Claude declined to write the commentary; using the template")
        return ""
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return _no_dashes(text)


def _no_dashes(text: str) -> str:
    """The prompt asks for no em or en dashes; this catches any that slip through."""
    text = re.sub(r"(\d)\s*\u2013\s*(\d)", r"\1-\2", text)  # 3\u20131 -> 3-1
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)


def _num(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def template(s: dict) -> str:
    """Plainer commentary from the same facts, for when Claude isn't available."""
    paras = []
    log_rows, luck, scoring = s["game_log"], s["luck"], s["scoring"]

    # What just happened
    now = s.get("this_week_so_far")
    if now:
        done = now["status"].startswith("every")
        if done:
            verb = "beat" if now["you"] > now["them"] else "lost to" if now["you"] < now["them"] else "tied"
            first = (f"Week {now['week']} looks done: you {verb} {now['opponent']} "
                     f"{_num(now['you'])} to {_num(now['them'])}, pending ESPN making it official.")
        else:
            lead = ("leading" if now["you"] > now["them"] else "trailing" if now["you"] < now["them"]
                    else "tied with")
            first = (f"Week {now['week']} is underway: you're {lead} {now['opponent']} "
                     f"{_num(now['you'])} to {_num(now['them'])}.")
    elif log_rows:
        g = log_rows[-1]
        verb = {"W": "beat", "L": "lost to", "T": "tied"}[g["result"]]
        rank, n = g["league_rank_that_week"], s["teams_in_league"]
        where = ("the best score in the league" if rank == 1 else "the worst score in the league"
                 if rank == n else f"the {_ordinal(rank)} best score in the league")
        first = (f"Week {g['week']}: you {verb} {g['opponent']} {_num(g['you'])} to {_num(g['them'])}"
                 f", {where} that week.")
        if g["result"] == "L" and g["league_rank_that_week"] <= s["teams_in_league"] // 2:
            first += " Wrong opponent, wrong week."
        elif g["result"] == "W" and g["league_rank_that_week"] > s["teams_in_league"] // 2:
            first += " Not pretty, but they all count the same."
    else:
        first = "No games in the books yet."
    streak = s.get("streak") or ""
    if len(streak) > 1 and int(streak[1:]) >= 3:
        first += f" That's {streak[1:]} {'wins' if streak[0] == 'W' else 'losses'} in a row."
    paras.append(first)

    # The season
    season = f"You're **{s['record']}**, {_ordinal(s['standing'])} in the standings"
    if scoring["per_week"] is not None:
        season += (f", averaging {_num(scoring['per_week'])} a week ({_ordinal(scoring['points_for_rank'])} "
                   f"in points) against a league average of {_num(scoring['league_average_per_week'])}.")
    else:
        season += "."
    recent_key = f"last_{RECENT_WEEKS}_weeks_per_week"
    if scoring.get("before_that_per_week") and scoring.get(recent_key):
        diff = scoring[recent_key] - scoring["before_that_per_week"]
        if diff <= -10:
            season += (f" Lately it's gone cold: {_num(scoring[recent_key])} a week over the last "
                       f"{RECENT_WEEKS}, down from {_num(scoring['before_that_per_week'])} before that.")
        elif diff >= 10:
            season += (f" And it's heating up: {_num(scoring[recent_key])} a week over the last "
                       f"{RECENT_WEEKS}, up from {_num(scoring['before_that_per_week'])}.")
    hurt = [h for h in s["injured_or_out"]
            if h["season_avg"] and h["status"] not in ("Questionable", "Day To Day")]
    if hurt:
        names = [f"{h['player']} ({h['status'].lower()}, {_num(h['season_avg'])} a game)" for h in hurt[:2]]
        season += f" The infirmary isn't helping: {' and '.join(names)}."
    if s["top_scorers"]:
        star = s["top_scorers"][0]
        season += f" {star['player']} has carried the most weight with {_num(star['season_points'])} points."
    paras.append(season)

    # Luck
    if log_rows:
        delta = luck["luck_wins"]
        text = (f"Your scoring would have gone {luck['all_play_record']} against the whole league, "
                f"worth about {_num(luck['expected_wins_from_scoring'])} wins. You have {_num(luck['actual_wins'])}.")
        if delta <= -1:
            text += f" That's {_num(-delta)} wins the schedule gods took from you"
            text += (f", with the {_ordinal(luck['points_against_rank'])} most points scored against you."
                     if luck["points_against_rank"] <= 3 else ".")
        elif delta >= 1:
            text += f" That's {_num(delta)} more than you've earned. Enjoy it while it lasts."
        else:
            text += " Luck has been about even, so the record is honest."
        n = luck["losses_while_top_half_scorer"]
        if n == 1:
            text += " One of your losses came in a week you were a top-half scorer."
        elif n > 1:
            text += f" {n} of your losses came in weeks you were a top-half scorer."
        paras.append(text)

    # Playoffs
    po = s["playoff_odds"]
    text = (f"Playoff odds: **{_num(po)}%** ({s['playoff_spots']} of {s['teams_in_league']} teams get in), "
            f"title odds {_num(s['title_odds'])}%.")
    trend = s.get("odds_trend") or {}
    if trend.get("a_week_ago"):
        then = trend["a_week_ago"]["playoff_odds"]
        if abs(po - then) >= 3:
            text += f" That's {'up' if po > then else 'down'} from {_num(then)}% a week ago."
    sc = s.get("what_has_to_happen")
    if sc:
        rows = sorted(sc["by_your_wins"], key=lambda r: r["your_wins"])
        left = sc["regular_season_games_left"]
        enough = next((r for r in rows if r["playoff_odds"] >= 50), None)
        if enough is None:
            text += (f" Even winning all {left} left only gets you in "
                     f"{_num(rows[-1]['playoff_odds'])}% of the time, so you need help.")
        elif enough["your_wins"] == left and left <= 4:
            text += (f" It's win-out territory: take all {left} and you're in "
                     f"{_num(enough['playoff_odds'])}% of the time")
            text += (f", drop one and it's {_num(rows[-2]['playoff_odds'])}%." if len(rows) > 1 else ".")
        elif enough["your_wins"] == 0:
            text += " You'd have to work pretty hard to miss from here."
        else:
            text += (f" The magic number is about {enough['your_wins']} more wins out of {left}: "
                     f"that gets you in {_num(enough['playoff_odds'])}% of the time.")
        if sc["root_for"]:
            r = sc["root_for"][0]
            text += f" Also: root for {r['root_for']} over {r['against']} in week {r['week']}."
    paras.append(text)
    return "\n\n".join(paras)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
