"""One daily run: pick up free agents, set the lineup, propose trades."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np

from . import espn, trades, waivers
from .config import Config
from .models import BENCH_SLOT, IR_SLOT, SLOT_NAMES, League, PendingTrade
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
    lineup_gain: float = 0.0
    incoming: list[IncomingOffer] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Optimizer:
    def __init__(self, league: League, cfg: Config, state: State,
                 client: espn.EspnClient | None = None, now: dt.datetime | None = None):
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
        result = RunResult(league=league, dry_run=self.dry_run, baseline=baseline)
        if not league.horizon:
            result.notes.append("Season is over. Nothing to do.")
            return result

        if cfg.waivers.enabled:
            self.do_waivers(result)
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
            base = self.odds(self.rosters)
            for idea in ideas[:wc.sim_candidates]:
                trial = {**self.rosters, self.me: waivers.apply(roster, idea)}
                idea.title_gain = 100 * (self.odds(trial).title_odds[self.me] - base.title_odds[self.me])
            if not result.add_ideas:
                result.add_ideas = ideas[:wc.sim_candidates]
            good = [i for i in ideas[:wc.sim_candidates]
                    if i.gain >= wc.min_gain_points and i.title_gain is not None
                    and i.title_gain >= wc.min_title_gain]
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

    def do_lineup(self, result: RunResult) -> None:
        league, valuer = self.league, self.valuer
        if league.horizon[0] != league.current_scoring_period:
            return
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
        result.lineup_gain = best - now_pts
        moves = []
        for pid in ids:
            target = valuer.slots[assignment[pid]] if pid in assignment else BENCH_SLOT
            if target != current[pid]:
                moves.append((pid, current[pid], target))
        if not moves or result.lineup_gain < 0.1:
            return
        desc = "; ".join(f"{league.players[pid].name} {SLOT_NAMES.get(a, a)}->{SLOT_NAMES.get(b, b)}"
                         for pid, a, b in moves)
        result.actions.append(self.execute(Action(
            kind="lineup",
            summary=f"Set lineup (+{result.lineup_gain:.1f} projected pts this week): {desc}",
            payload=espn.lineup_payload(league, moves),
        )))

    def do_trades(self, result: RunResult) -> None:
        tc = self.cfg.trades
        baseline = self.odds(self.rosters)
        ideas = trades.find_trades(self.league, self.rosters, self.valuer, self.cfg, self.state, self.now)
        # Keep variety: at most 5 candidates per partner go to the simulator.
        per_partner: dict[int, int] = {}
        shortlist = []
        for idea in ideas:
            if per_partner.get(idea.partner, 0) < 5:
                per_partner[idea.partner] = per_partner.get(idea.partner, 0) + 1
                shortlist.append(idea)
            if len(shortlist) >= tc.sim_candidates:
                break
        for idea in shortlist:
            after = self.odds(trades.apply(self.rosters, self.me, idea))
            idea.my_title_gain = 100 * (after.title_odds[self.me] - baseline.title_odds[self.me])
            idea.partner_title_gain = 100 * (after.title_odds[idea.partner]
                                             - baseline.title_odds[idea.partner])
        shortlist.sort(key=lambda i: (i.expected_title_gain, i.my_gain), reverse=True)
        result.trade_ideas = shortlist

        proposed_to: set[int] = set()
        for idea in shortlist:
            if len(proposed_to) >= tc.max_proposals_per_run:
                break
            if idea.partner in proposed_to or idea.my_title_gain < tc.min_title_gain:
                continue
            action = self.execute(Action(
                kind="trade",
                summary=f"Propose to {self.league.team_name(idea.partner)}: {describe_trade(self.league, idea)}",
                payload=espn.trade_payload(self.league, idea.partner, list(idea.give), list(idea.get),
                                           list(idea.my_drops), tc.message, tc.expiration_hours, self.now),
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
