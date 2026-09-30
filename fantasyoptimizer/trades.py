"""Trade search: deals that make us better AND look good to the other manager."""
from __future__ import annotations

import datetime as dt
import itertools
import math
from dataclasses import dataclass

from .config import Config
from .models import IR_SLOT, League, Player
from .state import State, trade_key
from .valuation import Valuer
from .waivers import droppable


@dataclass
class TradeIdea:
    partner: int
    give: tuple[int, ...]
    get: tuple[int, ...]
    my_drops: tuple[int, ...]
    partner_drops: tuple[int, ...]
    my_gain: float            # weighted rest-of-season points, our model
    partner_gain: float       # same, for them
    fairness: float           # trade value they receive / they give (points above waivers)
    accept_chance: float = 0.5
    my_title_gain: float | None = None
    partner_title_gain: float | None = None
    pitch: str = ""           # one line to send them: why it helps their team

    @property
    def key(self) -> str:
        return trade_key(self.partner, self.give, self.get)

    @property
    def expected_title_gain(self) -> float:
        return (self.my_title_gain or 0.0) * self.accept_chance


def accept_chance(fairness: float, partner_gain: float, cfg: Config) -> float:
    """Rough odds a human accepts: do the names look fair, and does it fill a need?"""
    tc = cfg.trades
    z = tc.accept_fairness_weight * (fairness - 1.0) + partner_gain / tc.accept_gain_scale
    return min(0.95, 1.0 / (1.0 + math.exp(-z)))  # nobody accepts anything 100% of the time


def _matches(p: Player, names: set[str]) -> bool:
    return p.name.lower() in names or str(p.id) in names


def _active_after(league: League, original: list[int], new: list[int]) -> int:
    """Roster count after a trade; incoming players land on the bench."""
    return sum(1 for pid in new if not (pid in original and league.players[pid].lineup_slot == IR_SLOT))


def _forced_drops(league: League, valuer: Valuer, original: list[int], new: list[int],
                  incoming: tuple[int, ...], can_drop) -> tuple[int, ...] | None:
    over = _active_after(league, original, new) - league.roster_limit
    if over <= 0:
        return ()
    candidates = [pid for pid in new if pid not in incoming
                  and league.players[pid].lineup_slot != IR_SLOT and can_drop(league.players[pid])]
    if len(candidates) < over:
        return None
    candidates.sort(key=valuer.ros_points)
    return tuple(candidates[:over])


def blocked_partners(league: League, cfg: Config, state: State, now: dt.datetime) -> set[int]:
    names = {n.lower() for n in cfg.trades.do_not_trade_with}
    blocked = {t.id for t in league.teams.values()
               if str(t.id) in names or t.name.lower() in names or t.abbrev.lower() in names}
    for pending in league.pending_trades:
        if pending.proposer == league.my_team_id:
            blocked |= pending.team_ids
    blocked |= {t for t in league.teams
                if state.proposed_recently(t, cfg.trades.team_cooldown_days, now)}
    blocked.discard(league.my_team_id)
    return blocked


def replacement_levels(league: League, valuer: Valuer) -> dict[str, float]:
    """Rest-of-season points of the best player anyone can grab for free, by position."""
    levels: dict[str, float] = {}
    for p in league.players.values():
        if p.available:
            levels[p.position] = max(levels.get(p.position, 0.0), valuer.ros_points(p.id))
    return levels


def trade_value(league: League, valuer: Valuer) -> dict[int, float]:
    """What a player is worth in a trade: points above the best free agent at his position.

    This is how sharp managers think. A linebacker scoring 19 a game isn't worth much
    when there's one scoring 18.6 on waivers.
    """
    levels = replacement_levels(league, valuer)
    return {pid: max(0.0, valuer.ros_points(pid) - levels.get(p.position, 0.0))
            for pid, p in league.players.items()}


def trade_sets(players: list[Player], value: dict[int, float], pool_size: int, max_side: int):
    ranked = sorted(players, key=lambda p: value[p.id], reverse=True)
    sets = [(p.id,) for p in ranked]
    top = [p.id for p in ranked[:pool_size]]
    for size in range(2, max_side + 1):
        sets += list(itertools.combinations(top, size))
    return sets


def find_trades(league: League, rosters: dict[int, list[int]], valuer: Valuer, cfg: Config,
                state: State, now: dt.datetime) -> list[TradeIdea]:
    tc = cfg.trades
    me = league.my_team_id
    untouchable = {n.lower() for n in tc.untouchable}
    blocked = blocked_partners(league, cfg, state, now)
    ros = {pid: valuer.ros_points(pid) for pid in league.players}
    value = trade_value(league, valuer)

    my_roster = rosters[me]
    base_me = valuer.value(my_roster)
    # Players already offered in one of our pending proposals aren't offered again.
    offered = {item.get("playerId") for pending in league.pending_trades if pending.proposer == me
               for item in pending.items if item.get("fromTeamId") == me}
    mine = [league.players[pid] for pid in my_roster
            if not league.players[pid].trade_locked and not _matches(league.players[pid], untouchable)
            and ros[pid] > 0 and pid not in offered]
    give_sets = trade_sets(mine, value, tc.pool_size, tc.max_players_per_side)
    my_can_drop = lambda p: droppable(p, cfg, state, now) and not _matches(p, untouchable)  # noqa: E731
    their_can_drop = lambda p: not (p.roster_locked or p.lineup_locked)  # noqa: E731

    ideas: list[TradeIdea] = []
    for partner, their_roster in rosters.items():
        if partner == me or partner in blocked:
            continue
        base_them = valuer.value(their_roster)
        theirs = [league.players[pid] for pid in their_roster
                  if not league.players[pid].trade_locked and ros[pid] > 0]
        for get in trade_sets(theirs, value, tc.pool_size, tc.max_players_per_side):
            value_get = sum(value[pid] for pid in get)
            if value_get <= 0:
                continue
            for give in give_sets:
                fairness = sum(value[pid] for pid in give) / value_get
                if not tc.min_fairness <= fairness <= tc.max_overpay:
                    continue
                new_me = [pid for pid in my_roster if pid not in give] + list(get)
                my_drops = _forced_drops(league, valuer, my_roster, new_me, get, my_can_drop)
                if my_drops is None:
                    continue
                new_me = [pid for pid in new_me if pid not in my_drops]
                my_gain = valuer.value(new_me) - base_me
                if my_gain < tc.min_gain_points:
                    continue
                new_them = [pid for pid in their_roster if pid not in get] + list(give)
                their_drops = _forced_drops(league, valuer, their_roster, new_them, give, their_can_drop)
                if their_drops is None:
                    continue
                new_them = [pid for pid in new_them if pid not in their_drops]
                partner_gain = valuer.value(new_them) - base_them
                if partner_gain < tc.min_partner_gain_points:
                    continue
                chance = accept_chance(fairness, partner_gain, cfg)
                if chance < tc.min_accept_chance:
                    continue
                idea = TradeIdea(partner, give, get, my_drops, their_drops,
                                 my_gain, partner_gain, fairness, chance)
                if not state.proposed_before(idea.key, tc.repeat_cooldown_days, now):
                    ideas.append(idea)
    ideas.sort(key=lambda i: i.my_gain * i.accept_chance, reverse=True)
    return ideas


def apply(rosters: dict[int, list[int]], me: int, idea: TradeIdea) -> dict[int, list[int]]:
    out = dict(rosters)
    out[me] = [pid for pid in rosters[me] if pid not in idea.give and pid not in idea.my_drops] + list(idea.get)
    out[idea.partner] = ([pid for pid in rosters[idea.partner]
                          if pid not in idea.get and pid not in idea.partner_drops] + list(idea.give))
    return out
