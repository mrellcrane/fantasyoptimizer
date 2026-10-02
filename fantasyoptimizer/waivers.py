"""Free agent / waiver pickups: find the add/drop that most improves the season."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

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
    start_weeks: list[int] = field(default_factory=list)  # weeks they'd be in the lineup
    patch: bool = False               # only fills a hole weeks from now; add it later
    wait: bool = False                # a stream for next week: drop costs you this week

    @property
    def patch_weeks(self) -> list[int]:
        return self.start_weeks if self.patch else []

    @property
    def later(self) -> bool:
        """Reported, but not a move to make today."""
        return self.patch or self.wait


def streamed(p: Player, cfg: Config) -> bool:
    """Positions you turn over weekly (D/ST): hold and re-add cooldowns don't apply."""
    return p.position in cfg.streaming.positions


def min_gain(move: AddDrop, cfg: Config) -> float:
    """Points a pickup must add. A stream only needs to win its week."""
    return cfg.streaming.min_gain_points if streamed(move.add, cfg) else cfg.waivers.min_gain_points


def droppable(p: Player, cfg: Config, state: State, now: dt.datetime) -> bool:
    protected = {n.lower() for n in cfg.waivers.never_drop}
    if p.name.lower() in protected or str(p.id) in protected:
        return False
    if p.roster_locked or p.lineup_locked:
        return False
    return streamed(p, cfg) or not state.added_recently(p.id, cfg.waivers.hold_days, now)


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
    mine = valuer.streaming
    base = valuer.value(mine(roster))
    open_spots = league.roster_limit - league.active_count(roster)

    # Players whose game already started this week can't be picked up yet.
    pool = [p for p in league.players.values()
            if p.available and p.id not in exclude and not p.lineup_locked
            and (streamed(p, cfg) or not state.dropped_recently(p.id, wc.readd_cooldown_days, now))]
    # The best few at each position you can start. A single top-N list gets swamped by
    # whichever position scores the most (linebackers, in IDP leagues).
    by_position: dict[str, list[Player]] = {}
    for p in pool:
        row = valuer.row.get(p.id)
        if row is not None and valuer.can_start[row] and valuer.ros_points(p.id) > 0:
            by_position.setdefault(p.position, []).append(p)
    pool = [p for group in by_position.values()
            for p in sorted(group, key=lambda p: valuer.ros_points(p.id), reverse=True)
            [:wc.pool_per_position]]

    drops = [league.players[pid] for pid in roster
             if pid not in exclude and droppable(league.players[pid], cfg, state, now)]

    ideas = []
    for fa in pool:
        # (gain, -value of the player dropped): on ties, drop the lesser player.
        best: tuple[float, float, Player | None] | None = None
        if open_spots > 0:
            best = (round(valuer.value(mine(roster + [fa.id])) - base, 6), 0.0, None)
        options = drops
        if streamed(fa, cfg) and any(league.players[pid].position == fa.position for pid in roster):
            # A stream replaces the one you have (once it's droppable); it's no reason to cut depth.
            options = [d for d in drops if d.position == fa.position]
        for d in options:
            # Dropping someone from an IR slot doesn't free an active roster spot.
            if d.lineup_slot == IR_SLOT and open_spots <= 0:
                continue
            gain = round(valuer.value(mine([pid for pid in roster if pid != d.id] + [fa.id])) - base, 6)
            option = (gain, -valuer.ros_points(d.id), d)
            if best is None or option[:2] > best[:2]:
                best = option
        if best is None or best[0] <= 0:
            continue
        gain, _, drop = best
        waiver = fa.status == "WAIVERS"
        new_roster = apply(roster, AddDrop(fa, drop, gain, waiver))
        weeks = valuer.lineup_weeks(mine(new_roster), fa.id)
        near = (valuer.value_window(mine(new_roster), wc.patch_lookahead_weeks)
                - valuer.value_window(mine(roster), wc.patch_lookahead_weeks))
        patch = (not streamed(fa, cfg) and near < wc.patch_min_near_gain
                 and len(weeks) < wc.patch_max_start_share * len(league.horizon))
        # Streaming next week's matchup by dropping someone who still plays for you
        # this week: make the move after his game, not before.
        this_week = valuer.value_window(mine(new_roster), 1) - valuer.value_window(mine(roster), 1)
        wait = streamed(fa, cfg) and drop is not None and this_week < -1e-6
        ideas.append(AddDrop(add=fa, drop=drop, gain=gain, waiver=waiver,
                             bid=faab_bid(gain, league, cfg) if waiver else 0,
                             start_weeks=weeks, patch=patch, wait=wait and not patch))
    ideas.sort(key=lambda i: i.gain, reverse=True)
    return ideas


def apply(roster: list[int], move: AddDrop) -> list[int]:
    out = [pid for pid in roster if move.drop is None or pid != move.drop.id]
    return out + [move.add.id]
