"""Free agent / waiver pickups: find the add/drop that most improves the season."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import Config
from .models import IR_SLOT, League, Player
from .state import State
from .valuation import Valuer


@dataclass
class AddDrop:
    add: Player
    drop: Player | None
    gain: float                       # weighted rest-of-season points
    waiver: bool
    bid: int = 0
    title_gain: float | None = None   # percentage points of championship odds


def droppable(p: Player, cfg: Config, state: State, now: dt.datetime) -> bool:
    protected = {n.lower() for n in cfg.waivers.never_drop}
    if p.name.lower() in protected or str(p.id) in protected:
        return False
    if p.roster_locked or p.lineup_locked:
        return False
    return not state.added_recently(p.id, cfg.waivers.hold_days, now)


def faab_bid(gain: float, league: League, cfg: Config) -> int:
    remaining = league.my_team.faab_remaining or 0
    if not league.uses_faab or remaining <= 0:
        return 0
    wc = cfg.waivers
    fraction = wc.faab_max_fraction * min(1.0, gain / wc.faab_gain_for_max_bid)
    return int(min(remaining, max(wc.faab_min_bid, round(fraction * remaining))))


def find_add_drops(league: League, roster: list[int], valuer: Valuer, cfg: Config,
                   state: State, now: dt.datetime, exclude: set[int] = frozenset()) -> list[AddDrop]:
    """Best add/drop for each available player, sorted by gain."""
    wc = cfg.waivers
    base = valuer.value(roster)
    open_spots = league.roster_limit - league.active_count(roster)

    # Players whose game already started this week can't be picked up yet.
    pool = [p for p in league.players.values()
            if p.available and p.id not in exclude and not p.lineup_locked
            and not state.dropped_recently(p.id, wc.readd_cooldown_days, now)]
    pool.sort(key=lambda p: valuer.ros_points(p.id), reverse=True)
    pool = [p for p in pool[:wc.pool_size] if valuer.ros_points(p.id) > 0]

    drops = [league.players[pid] for pid in roster
             if pid not in exclude and droppable(league.players[pid], cfg, state, now)]

    ideas = []
    for fa in pool:
        # (gain, -value of the player dropped): on ties, drop the lesser player.
        best: tuple[float, float, Player | None] | None = None
        if open_spots > 0:
            best = (round(valuer.value(roster + [fa.id]) - base, 6), 0.0, None)
        for d in drops:
            # Dropping someone from an IR slot doesn't free an active roster spot.
            if d.lineup_slot == IR_SLOT and open_spots <= 0:
                continue
            gain = round(valuer.value([pid for pid in roster if pid != d.id] + [fa.id]) - base, 6)
            option = (gain, -valuer.ros_points(d.id), d)
            if best is None or option[:2] > best[:2]:
                best = option
        if best is None or best[0] <= 0:
            continue
        gain, _, drop = best
        waiver = fa.status == "WAIVERS"
        ideas.append(AddDrop(add=fa, drop=drop, gain=gain, waiver=waiver,
                             bid=faab_bid(gain, league, cfg) if waiver else 0))
    ideas.sort(key=lambda i: i.gain, reverse=True)
    return ideas


def apply(roster: list[int], move: AddDrop) -> list[int]:
    out = [pid for pid in roster if move.drop is None or pid != move.drop.id]
    return out + [move.add.id]
