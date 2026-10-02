"""Roster valuation: optimal lineups every remaining week, summed over the season."""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from scipy.optimize import linear_sum_assignment

from .config import Config
from .models import League
from .projections import projection_matrix

_INELIGIBLE = -1e6


class Valuer:
    """Scores rosters by the points their best possible lineups would score.

    A roster's value is the sum over remaining scoring periods of
    (optimal starting lineup points + a little credit for bench depth),
    with fantasy playoff weeks weighted up. Because each week is solved
    separately, byes and injuries are handled naturally: depth only matters
    when it actually gets into the lineup.
    """

    def __init__(self, league: League, cfg: Config):
        self.league = league
        self.cfg = cfg
        self.player_ids, self.points = projection_matrix(league, cfg)
        self.row = {pid: i for i, pid in enumerate(self.player_ids)}
        self.slots = league.starting_slots
        self.eligible = np.array(
            [[slot in league.players[pid].eligible_slots for slot in self.slots]
             for pid in self.player_ids],
            dtype=bool,
        ).reshape(len(self.player_ids), len(self.slots))
        self.can_start = self.eligible.any(axis=1)

        # Replacement level: the best free agent at each position, week by week. Bench
        # depth only counts what a player adds over that, since anyone can grab him.
        n_weeks = len(league.horizon)
        self.replacement: dict[str, np.ndarray] = {}
        for i, pid in enumerate(self.player_ids):
            p = league.players[pid]
            if p.available:
                best = self.replacement.get(p.position)
                self.replacement[p.position] = (self.points[i].copy() if best is None
                                                else np.maximum(best, self.points[i]))
        self._no_replacement = np.zeros(n_weeks)

        playoff_sps = {sp for mp in league.playoff_periods for sp in league.scoring_periods_of(mp)}
        self.weights = np.array([
            cfg.value.playoff_weight if sp in playoff_sps else 1.0 for sp in league.horizon
        ])
        # Which horizon columns make up each remaining matchup period.
        self.period_columns = {
            mp: [j for j, sp in enumerate(league.horizon) if sp in league.scoring_periods_of(mp)]
            for mp in league.remaining_matchup_periods
        }
        self._cache: dict[frozenset[int], tuple[np.ndarray, np.ndarray]] = {}

    # ------------------------------------------------------------ lineups

    def solve(self, roster: Iterable[int], points: np.ndarray | None = None,
              column: int | None = None, slots: list[int] | None = None,
              prefer: dict[int, int] | None = None) -> tuple[float, dict[int, int]]:
        """Best lineup for one scoring period. Returns (points, {player_id: slot_index}).

        `slots` restricts which entries of self.slots are open; `prefer` maps
        player id -> slot id they sit in now, so ties keep players where they are.
        """
        roster = [pid for pid in roster if pid in self.row]
        rows = [self.row[pid] for pid in roster]
        if points is None:
            points = self.points[rows, column]
        slot_idx = list(range(len(self.slots))) if slots is None else slots
        if not rows or not slot_idx:
            return 0.0, {}
        elig = self.eligible[np.ix_(rows, slot_idx)]
        weights = np.where(elig, np.maximum(points, 0.0)[:, None], _INELIGIBLE)
        solve_weights = weights
        if prefer:
            bonus = np.array([[1e-3 if prefer.get(pid) == self.slots[j] else 0.0 for j in slot_idx]
                              for pid in roster])
            solve_weights = weights + bonus
        r, c = linear_sum_assignment(solve_weights, maximize=True)
        total, assignment = 0.0, {}
        for i, j in zip(r, c):
            if weights[i, j] > _INELIGIBLE / 2:
                total += weights[i, j]
                assignment[roster[i]] = slot_idx[j]
        return float(total), assignment

    def _weekly(self, roster: frozenset[int]) -> tuple[np.ndarray, np.ndarray]:
        cached = self._cache.get(roster)
        if cached is not None:
            return cached
        n = len(self.league.horizon)
        starters, depth = np.zeros(n), np.zeros(n)
        ids = [pid for pid in roster if pid in self.row]
        rows = np.array([self.row[pid] for pid in ids], dtype=int)
        k = self.cfg.value.bench_depth
        for j in range(n):
            total, assignment = self.solve(ids, self.points[rows, j] if len(rows) else None, j)
            starters[j] = total
            if k and len(rows):
                bench = [self.points[self.row[pid], j] - self._replacement(pid)[j] for pid in ids
                         if pid not in assignment and self.can_start[self.row[pid]]]
                bench = [b for b in bench if b > 0]
                depth[j] = sum(sorted(bench, reverse=True)[:k])
        self._cache[roster] = (starters, depth)
        return starters, depth

    def _replacement(self, pid: int) -> np.ndarray:
        return self.replacement.get(self.league.players[pid].position, self._no_replacement)

    # ------------------------------------------------------------ public

    def weekly_points(self, roster: Iterable[int]) -> np.ndarray:
        return self._weekly(frozenset(roster))[0]

    def value(self, roster: Iterable[int]) -> float:
        starters, depth = self._weekly(frozenset(roster))
        return float(self.weights @ (starters + self.cfg.value.bench_weight * depth))

    def value_window(self, roster: Iterable[int], weeks: int) -> float:
        """Like value(), but only the next `weeks` scoring periods."""
        starters, depth = self._weekly(frozenset(roster))
        w = self.weights[:weeks]
        return float(w @ (starters[:weeks] + self.cfg.value.bench_weight * depth[:weeks]))

    def lineup_weeks(self, roster: Iterable[int], pid: int) -> list[int]:
        """Scoring periods in which `pid` makes this roster's best lineup."""
        roster = list(roster)
        return [sp for j, sp in enumerate(self.league.horizon)
                if pid in self.solve(roster, column=j)[1]]

    def period_means(self, roster: Iterable[int]) -> dict[int, float]:
        """Expected score of this roster in each remaining matchup period."""
        weekly = self.weekly_points(roster)
        return {mp: float(weekly[cols].sum()) for mp, cols in self.period_columns.items()}

    def ros_points(self, pid: int) -> float:
        """A player's raw rest-of-season expected points (roster context ignored)."""
        row = self.row.get(pid)
        return float(self.weights @ self.points[row]) if row is not None else 0.0
