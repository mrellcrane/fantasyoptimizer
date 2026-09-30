"""One daily run: pick up free agents, set the lineup, propose trades."""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import espn, trades, waivers
from .config import Config
from .models import BENCH_SLOT, IR_SLOT, SLOT_NAMES, League, PendingTrade
from .projections import per_game_rate
from .simulate import SeasonSimulator, SimResult
from .state import State
from .valuation import Valuer

log = logging.getLogger(__name__)


@dataclass
class Action:
    kind: str                  # add | waiver | lineup | trade
    summary: str
    payload: dict
    executed: bool = False
    ok: bool | None = None
    response: str = ""
    why: str = ""
    pitch: str = ""


@dataclass
class LineupHole:
    player: int                 # a starter who can't play next week
    slot: int
    reason: str                 # "on bye" / "out"
    fill: int | None = None     # bench player who'd take the spot
    free_agent: int | None = None  # if nobody on the bench can


@dataclass
class LineupCheck:
    week: int
    best: float                 # projected points of the best lineup this week
    gain: float                 # ...minus what the current lineup would score
    swaps: list[tuple[int, int]] = field(default_factory=list)  # (start, sit)
    next_week: int | None = None
    holes: list[LineupHole] = field(default_factory=list)


@dataclass
class LineupChange:
    weeks: int
    old_starts: Counter = field(default_factory=Counter)   # weeks each player started before
    new_starts: Counter = field(default_factory=Counter)   # ...and after
    benched: Counter = field(default_factory=Counter)      # starters who lose their spot
    displaced: dict[int, Counter] = field(default_factory=dict)  # added player -> who sat


def _spread(ideas: list, per_partner: int = 2) -> list:
    """Same order, but at most `per_partner` ideas per team before any repeats."""
    first, rest, seen = [], [], Counter()
    for idea in ideas:
        (first if seen[idea.partner] < per_partner else rest).append(idea)
        seen[idea.partner] += 1
    return first + rest


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


@dataclass
class IncomingOffer:
    trade: PendingTrade
    partner: int
    give: list[int]
    get: list[int]
    my_gain: float
    title_gain: float


@dataclass
class RunResult:
    league: League
    dry_run: bool
    baseline: SimResult
    add_ideas: list[waivers.AddDrop] = field(default_factory=list)
    trade_ideas: list[trades.TradeIdea] = field(default_factory=list)
    long_shots: list[trades.TradeIdea] = field(default_factory=list)
    lineup_check: LineupCheck | None = None
    asked_trade: trades.TradeIdea | None = None   # a trade the user asked us to score
    asked_trade_why: str = ""
    asked_trade_error: str = ""
    lineup_gain: float = 0.0
    incoming: list[IncomingOffer] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    valuer: Valuer | None = None
    max_moves: int = 1


class Optimizer:
    def __init__(self, league: League, cfg: Config, state: State,
                 client: espn.EspnClient | None = None, now: dt.datetime | None = None,
                 score_trade: str | None = None):
        self.asked = (score_trade or "").strip()
        self.league = league
        self.cfg = cfg
        self.state = state
        self.client = client
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.me = league.my_team_id
        self.valuer = Valuer(league, cfg)
        self.sim = SeasonSimulator(league, cfg.sim)
        # Rosters as they'll look once today's moves go through (used for planning).
        self.rosters = {t.id: list(t.roster) for t in league.teams.values()}
        # Our roster as it is on ESPN right now: free agent adds count, pending waiver claims don't.
        self.lineup_roster = list(league.my_team.roster)
        self.dry_run = cfg.dry_run or client is None

    # ------------------------------------------------------------ odds

    def odds(self, rosters: dict[int, list[int]]) -> SimResult:
        return self.sim.run({t: self.valuer.period_means(r) for t, r in rosters.items()})

    def title_gain(self, rosters: dict[int, list[int]], baseline: SimResult) -> float:
        """Change in our championship odds, in percentage points."""
        return 100 * (self.odds(rosters).title_odds[self.me] - baseline.title_odds[self.me])

    def lineup_change(self, old: list[int], new: list[int]) -> "LineupChange":
        """Who starts in each remaining week before vs after a roster change."""
        valuer = self.valuer
        added = [pid for pid in new if pid not in old]
        change = LineupChange(weeks=len(self.league.horizon))
        change.displaced = {pid: Counter() for pid in added}
        for j in range(change.weeks):
            before = set(valuer.solve(old, column=j)[1])
            after = set(valuer.solve(new, column=j)[1])
            change.old_starts.update(before)
            change.new_starts.update(after)
            change.benched.update(before - after)
            for pid in added:
                if pid in after:
                    change.displaced[pid].update(before - after)
        return change

    def explain(self, old: list[int], new: list[int], whose: str = "your") -> str:
        """Plain-English reason a roster change helps: who starts, and who sits instead."""
        league = self.league
        change = self.lineup_change(old, new)
        added = [pid for pid in new if pid not in old]
        removed = [pid for pid in old if pid not in new]
        them = "you" if whose == "your" else "them"

        def label(pid: int) -> str:
            p = league.players[pid]
            return f"{p.name} ({p.position}, {per_game_rate(p, league, self.cfg):.1f} pts/gm)"

        parts = [f"{label(pid)} would start {change.new_starts[pid]} of {change.weeks} weeks"
                 for pid in added]
        if change.benched:
            out = ", ".join(f"{label(pid)} {n} wks" for pid, n in change.benched.most_common(3))
            parts.append(f"out of {whose} lineup: {out}")
        idle = [label(pid) for pid in removed if pid not in change.benched]
        if idle:
            parts.append(f"{', '.join(idle)} wouldn't start for {them} anyway")
        return "; ".join(parts) + "."

    def pitch(self, idea: trades.TradeIdea) -> str:
        """One line to send the other manager: what the deal does for their lineup."""
        league = self.league
        old = self.rosters[idea.partner]
        change = self.lineup_change(old, trades.apply(self.rosters, self.me, idea)[idea.partner])
        n = change.weeks
        # Lead with the players who'd start the most for them; a one-week fill-in
        # isn't a selling point unless it's all there is.
        incoming = sorted((pid for pid in idea.give if change.new_starts[pid]),
                          key=lambda pid: change.new_starts[pid], reverse=True)
        regulars = [pid for pid in incoming if change.new_starts[pid] >= 0.4 * n] or incoming[:1]
        bits = []
        for pid in regulars:
            k = change.new_starts[pid]
            p = league.players[pid]
            often = ("every week" if k >= n - 1 else "most weeks" if k >= 0.6 * n
                     else f"{k} of the {n} weeks left")
            text = f"{p.name} would start {often} for you"
            # Name who they're actually starting there now (what the manager sees),
            # falling back to who the model says he'd replace.
            starting_now = [q for q in old if q not in idea.get
                            and league.players[q].position == p.position
                            and league.players[q].lineup_slot not in (None, BENCH_SLOT, IR_SLOT)]
            same_spot = [q for q, _ in change.displaced[pid].most_common()
                         if league.players[q].position == p.position and q in old and q not in idea.get]
            if starting_now:
                over = min(starting_now, key=lambda q: per_game_rate(league.players[q], league, self.cfg))
                text += f" over {league.players[over].name}"
            elif same_spot:
                text += f" over {league.players[same_spot[0]].name}"
            bits.append(text)
        idle = [league.players[pid].name for pid in idea.get if change.old_starts[pid] <= 0.3 * n]
        if idle and bits:
            verb = "mostly sits" if len(idle) == 1 else "mostly sit"
            bits.append(f"{_join(idle)} {verb} on your bench anyway")
        if not bits:
            return ""
        line = _join(bits)
        return line[0].upper() + line[1:] + "."

    def _find(self, name: str, pool: list[int], where: str) -> int:
        needle = name.strip().lower()
        exact = [pid for pid in pool if self.league.players[pid].name.lower() == needle]
        loose = exact or [pid for pid in pool if needle in self.league.players[pid].name.lower()]
        if len(loose) == 1:
            return loose[0]
        if not loose:
            raise ValueError(f"Couldn't find \"{name.strip()}\" {where}.")
        names = ", ".join(self.league.players[pid].name for pid in loose[:5])
        raise ValueError(f'"{name.strip()}" matches more than one player {where}: {names}.')

    def score_trade(self, text: str, result: RunResult) -> None:
        """Score a trade typed as "give A, B; get C, D" (or "A, B for C, D")."""
        m = (re.match(r"(?is)\s*give\s+(.+?)\s*[;|/]\s*(?:get|for)\s+(.+)$", text)
             or re.match(r"(?is)\s*(?:give\s+)?(.+?)\s+for\s+(.+)$", text))
        if not m:
            raise ValueError('Write the trade as "give Player A, Player B; get Player C".')
        split = lambda part: [n for n in re.split(r"\s*(?:,|\+|&|\band\b)\s*", part) if n.strip()]  # noqa: E731
        mine = self.rosters[self.me]
        others = [pid for t, r in self.rosters.items() if t != self.me for pid in r]
        give = tuple(self._find(n, mine, "on your roster") for n in split(m.group(1)))
        get = tuple(self._find(n, others, "on another team") for n in split(m.group(2)))
        partners = {self.league.players[pid].team_id for pid in get}
        if len(partners) != 1:
            raise ValueError("Everyone you get has to come from the same team.")
        partner = partners.pop()
        idea = trades.evaluate_trade(self.league, self.rosters, self.valuer, self.cfg, self.state,
                                     self.now, partner, give, get)
        baseline = self.odds(self.rosters)
        after = trades.apply(self.rosters, self.me, idea)
        odds = self.odds(after)
        idea.my_title_gain = 100 * (odds.title_odds[self.me] - baseline.title_odds[self.me])
        idea.partner_title_gain = 100 * (odds.title_odds[partner] - baseline.title_odds[partner])
        idea.pitch = self.pitch(idea)
        result.asked_trade = idea
        result.asked_trade_why = (
            f"For you: {self.explain(self.rosters[self.me], after[self.me])} "
            f"For them: {self.explain(self.rosters[partner], after[partner], 'their')}")

    # ------------------------------------------------------------ execution

    def execute(self, action: Action) -> Action:
        if self.dry_run:
            log.info("[dry run] %s", action.summary)
            return action
        action.executed = True
        try:
            resp = self.client.submit(action.payload)
            action.ok = True
            action.response = str(resp.get("status", "OK")) if isinstance(resp, dict) else "OK"
            log.info("Done: %s (%s)", action.summary, action.response)
        except espn.EspnError as exc:
            action.ok = False
            action.response = str(exc)
            log.error("Failed: %s: %s", action.summary, exc)
        return action

    # ------------------------------------------------------------ steps

    def run(self) -> RunResult:
        league, cfg = self.league, self.cfg
        baseline = self.odds(self.rosters)
        result = RunResult(league=league, dry_run=self.dry_run, baseline=baseline, valuer=self.valuer,
                           max_moves=cfg.waivers.max_moves_per_run)
        if not league.horizon:
            result.notes.append("Season is over. Nothing to do.")
            return result

        if self.asked:  # scored against today's real rosters, before any planned moves
            try:
                self.score_trade(self.asked, result)
            except ValueError as exc:
                result.asked_trade_error = str(exc)
        if cfg.waivers.enabled:
            self.do_waivers(result)
        result.lineup_check = self.lineup_check()
        if cfg.lineup.enabled:
            self.do_lineup(result)
        if cfg.trades.enabled:
            if league.trade_deadline and self.now > league.trade_deadline:
                result.notes.append("Trade deadline has passed; skipping trades.")
            else:
                self.do_trades(result)
        self.review_incoming(result)
        return result

    def do_waivers(self, result: RunResult) -> None:
        wc = self.cfg.waivers
        claimed: set[int] = set()
        for _ in range(wc.max_moves_per_run):
            roster = self.rosters[self.me]
            ideas = waivers.find_add_drops(self.league, roster, self.valuer, self.cfg,
                                           self.state, self.now, exclude=claimed)
            # Bye-week patches are only shown, so they mustn't crowd out real pickups.
            actionable = [i for i in ideas if not i.patch][:wc.sim_candidates]
            patches = [i for i in ideas if i.patch][:3]
            base = self.odds(self.rosters)
            for idea in actionable + patches:
                trial = {**self.rosters, self.me: waivers.apply(roster, idea)}
                idea.title_gain = 100 * (self.odds(trial).title_odds[self.me] - base.title_odds[self.me])
            if not result.add_ideas:
                result.add_ideas = actionable + patches
            good = [i for i in actionable
                    if i.gain >= wc.min_gain_points and i.title_gain >= wc.min_title_gain]
            if not good:
                break
            best = max(good, key=lambda i: (round(i.title_gain, 2), i.gain))
            drop = f", drop {best.drop}" if best.drop else ""
            verb = f"Waiver claim (${best.bid})" if best.waiver and self.league.uses_faab else (
                "Waiver claim" if best.waiver else "Add")
            action = self.execute(Action(
                kind="waiver" if best.waiver else "add",
                summary=(f"{verb}: {best.add}{drop} "
                         f"(+{best.gain:.1f} season pts, {best.title_gain:+.1f}% title odds)"),
                payload=espn.add_drop_payload(self.league, best.add.id,
                                              best.drop.id if best.drop else None,
                                              waiver=best.waiver, bid=best.bid),
                why=self.explain(roster, waivers.apply(roster, best)),
            ))
            result.actions.append(action)
            claimed.add(best.add.id)
            if best.drop:
                claimed.add(best.drop.id)
            if action.executed and not action.ok:
                break
            if action.ok:
                self.state.record_add_drop(best.add.id, best.drop.id if best.drop else None, self.now)
            # Plan the rest of the day as if the move went through. Waiver claims
            # process later, so the lineup step keeps ignoring them.
            self.rosters[self.me] = waivers.apply(roster, best)
            if not best.waiver:
                self.lineup_roster = waivers.apply(self.lineup_roster, best)
                p = self.league.players[best.add.id]
                p.lineup_slot, p.team_id = BENCH_SLOT, self.me

    def lineup_plan(self) -> tuple[list[tuple[int, int, int]], float, float] | None:
        """This week's best lineup: (moves, its projected points, current lineup's points)."""
        league, valuer = self.league, self.valuer
        if not league.horizon or league.horizon[0] != league.current_scoring_period:
            return None
        players = [league.players[pid] for pid in self.lineup_roster]
        points = {p.id: valuer.points[valuer.row[p.id], 0] for p in players}

        open_slots = list(range(len(valuer.slots)))
        for p in players:  # locked starters stay put and use up their slot
            if p.lineup_locked and p.lineup_slot not in (None, BENCH_SLOT, IR_SLOT):
                j = next((j for j in open_slots if valuer.slots[j] == p.lineup_slot), None)
                if j is not None:
                    open_slots.remove(j)
        movable = [p for p in players if not p.lineup_locked and p.lineup_slot != IR_SLOT]
        ids = [p.id for p in movable]
        current = {p.id: (p.lineup_slot if p.lineup_slot is not None else BENCH_SLOT) for p in movable}
        best, assignment = valuer.solve(ids, np.array([points[pid] for pid in ids]),
                                        slots=open_slots, prefer=current)
        now_pts = sum(points[pid] for pid in ids if current[pid] not in (BENCH_SLOT, IR_SLOT))
        moves = []
        for pid in ids:
            target = valuer.slots[assignment[pid]] if pid in assignment else BENCH_SLOT
            if target != current[pid]:
                moves.append((pid, current[pid], target))
        return moves, best, now_pts

    def do_lineup(self, result: RunResult) -> None:
        plan = self.lineup_plan()
        if plan is None:
            return
        moves, best, now_pts = plan
        result.lineup_gain = best - now_pts
        if not moves or result.lineup_gain < 0.1:
            return
        league = self.league
        desc = "; ".join(f"{league.players[pid].name} {SLOT_NAMES.get(a, a)}->{SLOT_NAMES.get(b, b)}"
                         for pid, a, b in moves)
        result.actions.append(self.execute(Action(
            kind="lineup",
            summary=f"Set lineup (+{result.lineup_gain:.1f} projected pts this week): {desc}",
            payload=espn.lineup_payload(league, moves),
        )))

    def lineup_check(self) -> LineupCheck | None:
        """The quick wins: bench-for-starter swaps this week, and holes coming next week."""
        plan = self.lineup_plan()
        if plan is None:
            return None
        moves, best, now_pts = plan
        league, valuer = self.league, self.valuer

        def pts(pid: int, col: int) -> float:
            return float(valuer.points[valuer.row[pid], col])

        def starting(slot: int | None) -> bool:
            return slot not in (None, BENCH_SLOT, IR_SLOT)

        check = LineupCheck(week=league.horizon[0], best=best, gain=best - now_pts)
        if check.gain >= 0.1:
            # Pair each player coming off the bench with whoever leaves the same slot.
            ins = sorted(((pid, b) for pid, a, b in moves if not starting(a) and starting(b)),
                         key=lambda m: -pts(m[0], 0))
            outs = [(pid, a) for pid, a, b in moves if starting(a) and not starting(b)]
            for pid, slot in ins:
                match = next((o for o in outs if o[1] == slot), outs[0] if outs else None)
                if match is not None:
                    outs.remove(match)
                    check.swaps.append((pid, match[0]))
        if len(league.horizon) < 2:
            return check

        # ESPN carries this week's lineup into next week, so look for starters who can't play.
        check.next_week = league.horizon[1]
        slot_after = {pid: league.players[pid].lineup_slot for pid in self.lineup_roster}
        for pid, _, target in moves:
            slot_after[pid] = target
        starters = [pid for pid, slot in slot_after.items() if starting(slot)]
        _, best_next = valuer.solve([pid for pid, slot in slot_after.items() if slot != IR_SLOT],
                                    column=1)
        newcomers = [pid for pid in best_next if pid not in starters]
        for pid in starters:
            if pts(pid, 1) > 0:
                continue
            p, slot = league.players[pid], slot_after[pid]
            reason = ("on bye" if league.games(p.pro_team_id, check.next_week) == 0
                      else p.injury_status.replace("_", " ").lower())
            hole = LineupHole(player=pid, slot=slot, reason=reason)
            hole.fill = next((q for q in newcomers if slot in league.players[q].eligible_slots), None)
            if hole.fill is not None:
                newcomers.remove(hole.fill)
            else:
                options = [q for q in league.players.values()
                           if q.available and not q.lineup_locked and slot in q.eligible_slots
                           and pts(q.id, 1) > 0]
                if options:
                    hole.free_agent = max(options, key=lambda q: pts(q.id, 1)).id
            check.holes.append(hole)
        return check

    def _shortlist(self, ideas: list[trades.TradeIdea], baseline: SimResult,
                   skip: set[str] = frozenset()) -> list[trades.TradeIdea]:
        """Simulate the most promising ideas (at most 5 per partner) for title odds."""
        per_partner: dict[int, int] = {}
        shortlist = []
        for idea in ideas:
            if idea.key in skip:
                continue
            if per_partner.get(idea.partner, 0) < 5:
                per_partner[idea.partner] = per_partner.get(idea.partner, 0) + 1
                shortlist.append(idea)
            if len(shortlist) >= self.cfg.trades.sim_candidates:
                break
        for idea in shortlist:
            after = self.odds(trades.apply(self.rosters, self.me, idea))
            idea.my_title_gain = 100 * (after.title_odds[self.me] - baseline.title_odds[self.me])
            idea.partner_title_gain = 100 * (after.title_odds[idea.partner]
                                             - baseline.title_odds[idea.partner])
        return shortlist

    def do_trades(self, result: RunResult) -> None:
        tc = self.cfg.trades
        baseline = self.odds(self.rosters)
        ideas = trades.find_trades(self.league, self.rosters, self.valuer, self.cfg, self.state, self.now)
        shortlist = self._shortlist(ideas, baseline)
        shortlist.sort(key=lambda i: (i.expected_title_gain, i.my_gain), reverse=True)
        shortlist = _spread(shortlist)
        for idea in shortlist[:8]:
            idea.pitch = self.pitch(idea)
        result.trade_ideas = shortlist

        if tc.long_shots:
            bold = trades.find_trades(self.league, self.rosters, self.valuer, self.cfg, self.state,
                                      self.now, long_shot=True)
            long_shots = self._shortlist(bold, baseline, skip={i.key for i in shortlist})
            # Ranked purely by what they'd do for you; acceptance is their problem.
            long_shots = [i for i in long_shots if i.my_title_gain > 0]
            long_shots.sort(key=lambda i: (i.my_title_gain, i.my_gain), reverse=True)
            long_shots = _spread(long_shots)
            for idea in long_shots[:8]:
                idea.pitch = self.pitch(idea)
            result.long_shots = long_shots
        if tc.propose_long_shots and result.long_shots:
            shortlist = result.long_shots

        proposed_to: set[int] = set()
        for idea in shortlist:
            if len(proposed_to) >= tc.max_proposals_per_run:
                break
            if idea.partner in proposed_to or idea.my_title_gain < tc.min_title_gain:
                continue
            after = trades.apply(self.rosters, self.me, idea)
            message = idea.pitch if tc.pitch_as_message and idea.pitch else tc.message
            action = self.execute(Action(
                kind="trade",
                summary=f"Propose to {self.league.team_name(idea.partner)}: {describe_trade(self.league, idea)}",
                payload=espn.trade_payload(self.league, idea.partner, list(idea.give), list(idea.get),
                                           list(idea.my_drops), message, tc.expiration_hours, self.now),
                why=(f"For you: {self.explain(self.rosters[self.me], after[self.me])} "
                     f"For them: {self.explain(self.rosters[idea.partner], after[idea.partner], 'their')}"),
                pitch=idea.pitch,
            ))
            result.actions.append(action)
            proposed_to.add(idea.partner)
            if action.ok:
                self.state.record_proposal(idea.partner, idea.give, idea.get, self.now)

    def review_incoming(self, result: RunResult) -> None:
        """Score trade offers other managers sent us. We never auto-accept."""
        baseline = self.odds(self.rosters)
        for pending in self.league.pending_trades:
            if pending.proposer == self.me or self.me not in pending.team_ids:
                continue
            give = [i["playerId"] for i in pending.items
                    if i.get("fromTeamId") == self.me and i.get("type") != "DROP"]
            get = [i["playerId"] for i in pending.items if i.get("toTeamId") == self.me]
            partner = pending.proposer
            if partner not in self.rosters or not all(pid in self.league.players for pid in give + get):
                continue
            rosters = dict(self.rosters)
            rosters[self.me] = [pid for pid in self.rosters[self.me] if pid not in give] + get
            rosters[partner] = [pid for pid in self.rosters[partner] if pid not in get] + give
            gain = self.valuer.value(rosters[self.me]) - self.valuer.value(self.rosters[self.me])
            result.incoming.append(IncomingOffer(pending, partner, give, get, gain,
                                                 self.title_gain(rosters, baseline)))


def describe_trade(league: League, idea: trades.TradeIdea) -> str:
    give = ", ".join(str(league.players[pid]) for pid in idea.give)
    get = ", ".join(str(league.players[pid]) for pid in idea.get)
    text = f"give {give} for {get}"
    if idea.my_drops:
        text += " (drop " + ", ".join(str(league.players[pid]) for pid in idea.my_drops) + ")"
    return text
