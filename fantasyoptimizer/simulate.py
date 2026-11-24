"""Monte Carlo of the rest of the season: playoff odds and championship odds.

The same random draws are reused for every scenario (common random numbers),
so comparing "before" vs "after" a move measures the move, not sampling noise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import SimConfig
from .models import League


@dataclass
class SimResult:
    playoff_odds: dict[int, float]
    title_odds: dict[int, float]
    expected_wins: dict[int, float]


@dataclass
class Rooting:
    """A game you're not in whose result moves your playoff odds."""
    period: int
    root_for: int
    against: int
    swing: float              # playoff odds if root_for wins minus if they lose


@dataclass
class Scenarios:
    games_left: int
    # your wins over the rest of the regular season -> (chance, playoff odds, title odds)
    by_wins: dict[int, tuple[float, float, float]]
    rooting: list[Rooting]
    seeds: dict[int, float]   # playoff seed -> chance


@dataclass
class _Season:
    wins: np.ndarray          # (sims, teams)
    made: np.ndarray          # (sims, teams) made the playoffs
    champion: np.ndarray      # (sims,) team index
    home_won: np.ndarray      # (sims, remaining matchups)
    seed: np.ndarray | None   # (sims, teams), 0 = missed


def bracket_order(size: int) -> list[int]:
    """Standard seeding order, e.g. 8 -> [1, 8, 4, 5, 2, 7, 3, 6]."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [s for seed in order for s in (seed, n + 1 - seed)]
    return order


class SeasonSimulator:
    def __init__(self, league: League, cfg: SimConfig):
        self.league = league
        self.cfg = cfg
        self.team_ids = sorted(league.teams)
        self.idx = {t: i for i, t in enumerate(self.team_ids)}
        n_teams = len(self.team_ids)

        in_playoffs = league.current_matchup_period > league.reg_season_periods
        self.remaining = [] if in_playoffs else [
            m for m in league.schedule
            if league.current_matchup_period <= m.period <= league.reg_season_periods
            and m.winner == "UNDECIDED" and m.away is not None
        ]
        self.reg_periods = sorted({m.period for m in self.remaining})

        playoff_periods = [p for p in league.playoff_periods if p >= league.current_matchup_period]
        n_playoff = min(league.playoff_team_count, n_teams)
        self.rounds_total = math.ceil(math.log2(n_playoff)) if n_playoff > 1 else 0

        # Mid-playoffs: bracket positions for this round, in schedule order (None = bye).
        self.bracket_slots: list[int | None] | None = None
        if in_playoffs:
            slots: list[int | None] = []
            for m in league.schedule:
                if m.period == league.current_matchup_period and m.playoff_tier in ("WINNERS_BRACKET", "NONE"):
                    slots.extend([m.home, m.away])
            self.bracket_slots = slots or None
        if self.bracket_slots:
            rounds_left = math.ceil(math.log2(len(self.bracket_slots))) if len(self.bracket_slots) > 1 else 0
        else:
            rounds_left = self.rounds_total
        self.playoff_rounds = playoff_periods[:rounds_left]

        rng = np.random.default_rng(cfg.seed)
        self.z_reg = rng.standard_normal((cfg.n_sims, n_teams, max(1, len(self.reg_periods))))
        self.z_po = rng.standard_normal((cfg.n_sims, n_teams, max(1, len(self.playoff_rounds))))
        self.base_wins = np.array([league.teams[t].wins + 0.5 * league.teams[t].ties
                                   for t in self.team_ids])
        self.base_pf = np.array([league.teams[t].points_for for t in self.team_ids])

    def _scores(self, means: dict[int, dict[int, float]], periods: list[int], z: np.ndarray) -> np.ndarray:
        mu = np.array([[means[t].get(p, 0.0) for p in periods] for t in self.team_ids])
        mu = mu.reshape(len(self.team_ids), len(periods))
        sd = np.maximum(self.cfg.min_sd, self.cfg.sd_fraction * mu)
        return mu[None, :, :] + sd[None, :, :] * z[:, :, :len(periods)]

    def run(self, means: dict[int, dict[int, float]]) -> SimResult:
        """means: team id -> {matchup period: expected points}."""
        season = self._season(means)
        n_teams = len(self.team_ids)
        champion = season.champion
        titles = np.bincount(champion[champion >= 0], minlength=n_teams) / self.cfg.n_sims
        return SimResult(
            playoff_odds={t: float(season.made[:, i].mean()) for t, i in self.idx.items()},
            title_odds={t: float(titles[i]) for t, i in self.idx.items()},
            expected_wins={t: float(season.wins[:, i].mean()) for t, i in self.idx.items()},
        )

    def scenarios(self, means: dict[int, dict[int, float]], team_id: int) -> Scenarios:
        """What one team's odds look like depending on how the rest of the season goes."""
        season = self._season(means)
        i = self.idx[team_id]
        made, title = season.made[:, i], season.champion == i
        mine = [k for k, m in enumerate(self.remaining) if team_id in (m.home, m.away)]
        my_wins = np.zeros(self.cfg.n_sims, dtype=int)
        for k in mine:
            m = self.remaining[k]
            my_wins += season.home_won[:, k] if m.home == team_id else ~season.home_won[:, k]
        by_wins = {}
        for w in range(len(mine) + 1):
            hit = my_wins == w
            if hit.any():
                by_wins[w] = (float(hit.mean()), float(made[hit].mean()), float(title[hit].mean()))
        rooting = []
        for k, m in enumerate(self.remaining):
            if k in mine:
                continue
            hw = season.home_won[:, k]
            if hw.all() or (~hw).all():
                continue
            home_case, away_case = float(made[hw].mean()), float(made[~hw].mean())
            root_for, against = (m.home, m.away) if home_case >= away_case else (m.away, m.home)
            rooting.append(Rooting(period=m.period, root_for=root_for, against=against,
                                   swing=abs(home_case - away_case)))
        rooting.sort(key=lambda r: (-r.swing, r.period))
        seed_counts = {}
        if season.seed is not None:
            seeds = season.seed[:, i]
            for s_ in sorted(set(seeds[seeds > 0].tolist())):
                seed_counts[int(s_)] = float((seeds == s_).mean())
        return Scenarios(games_left=len(mine), by_wins=by_wins, rooting=rooting, seeds=seed_counts)

    def _season(self, means) -> _Season:
        n_sims, n_teams = self.cfg.n_sims, len(self.team_ids)
        wins = np.tile(self.base_wins, (n_sims, 1))
        pf = np.tile(self.base_pf, (n_sims, 1))
        home_won_all = np.zeros((n_sims, len(self.remaining)), dtype=bool)

        if self.remaining:
            scores = self._scores(means, self.reg_periods, self.z_reg)
            col = {p: k for k, p in enumerate(self.reg_periods)}
            for j, m in enumerate(self.remaining):
                h, a, k = self.idx[m.home], self.idx[m.away], col[m.period]
                home_won = scores[:, h, k] > scores[:, a, k]
                home_won_all[:, j] = home_won
                wins[:, h] += home_won
                wins[:, a] += ~home_won
                pf[:, h] += scores[:, h, k]
                pf[:, a] += scores[:, a, k]

        made = np.zeros((n_sims, n_teams), dtype=bool)
        seed_of = None
        if self.bracket_slots:
            size = 2 ** math.ceil(math.log2(max(2, len(self.bracket_slots))))
            bracket = np.full((n_sims, size), -1, dtype=int)
            for pos, t in enumerate(self.bracket_slots):
                if t is not None:
                    bracket[:, pos] = self.idx[t]
                    made[:, self.idx[t]] = True
        else:
            n_playoff = min(self.league.playoff_team_count, n_teams)
            # Seed by wins, then points for.
            order = np.argsort(-(wins * 1e7 + pf), axis=1, kind="stable")
            seeds = order[:, :n_playoff]
            np.put_along_axis(made, seeds, True, axis=1)
            seed_of = np.zeros((n_sims, n_teams), dtype=int)
            np.put_along_axis(seed_of, seeds, np.arange(1, n_playoff + 1)[None, :].repeat(n_sims, 0), axis=1)
            size = 2 ** math.ceil(math.log2(max(2, n_playoff)))
            bracket = np.full((n_sims, size), -1, dtype=int)
            for pos, seed in enumerate(bracket_order(size)):
                if seed <= n_playoff:
                    bracket[:, pos] = seeds[:, seed - 1]

        return _Season(wins=wins, made=made, champion=self._playoffs(means, bracket),
                       home_won=home_won_all, seed=seed_of)

    def _playoffs(self, means, bracket: np.ndarray) -> np.ndarray:
        if self.playoff_rounds:
            scores = self._scores(means, self.playoff_rounds, self.z_po)
            for r in range(len(self.playoff_rounds)):
                if bracket.shape[1] == 1:
                    break
                left, right = bracket[:, 0::2], bracket[:, 1::2]
                round_scores = scores[:, :, r]
                ls = np.where(left >= 0, np.take_along_axis(round_scores, np.maximum(left, 0), 1), -np.inf)
                rs = np.where(right >= 0, np.take_along_axis(round_scores, np.maximum(right, 0), 1), -np.inf)
                bracket = np.where(ls >= rs, left, right)
        # Leagues with fewer playoff periods than rounds: the top remaining bracket spot wins.
        first = np.argmax(bracket >= 0, axis=1)
        return bracket[np.arange(bracket.shape[0]), first]
