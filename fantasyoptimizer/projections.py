"""Turn ESPN's projections into expected points for every remaining scoring period."""
from __future__ import annotations

import numpy as np

from .config import Config
from .models import League, Player


def per_game_rate(p: Player, league: League, cfg: Config) -> float:
    """Blend of this week's projection, season projection, and actual production."""
    pc = cfg.projection
    signals: list[tuple[float, float]] = []

    week = p.period_projections.get(league.current_scoring_period)
    # A zero here usually means bye/injury, which availability handles separately.
    if week is not None and week > 0:
        signals.append((pc.weight_week, week))
    if p.ros_rate is not None and p.ros_rate > 0:
        signals.append((pc.weight_ros, p.ros_rate))
    if p.season_projection:
        signals.append((pc.weight_season, p.season_projection / pc.season_games))
    games = p.games_played
    if games > 0 and p.season_average is not None:
        trust = min(1.0, games / pc.actual_full_weight_games)
        signals.append((pc.weight_actual * trust, p.season_average))

    total_weight = sum(w for w, _ in signals)
    if total_weight <= 0:
        return 0.0
    return sum(w * v for w, v in signals) / total_weight


def availability(p: Player, weeks_ahead: int, cfg: Config) -> float:
    curve = cfg.injury.get(p.injury_status)
    if not curve or weeks_ahead >= len(curve):
        return 1.0
    return float(curve[weeks_ahead])


def expected_points(p: Player, league: League, cfg: Config, rate: float | None = None) -> np.ndarray:
    """Expected fantasy points for each scoring period in league.horizon."""
    rate = per_game_rate(p, league, cfg) if rate is None else rate
    current = league.current_scoring_period
    out = np.zeros(len(league.horizon))
    for j, sp in enumerate(league.horizon):
        if sp in p.period_projections:
            # ESPN's own weekly number already accounts for matchup, bye and injury.
            out[j] = max(0.0, p.period_projections[sp])
            continue
        games = league.games(p.pro_team_id, sp)
        out[j] = rate * games * availability(p, sp - current, cfg)
    return out


def projection_matrix(league: League, cfg: Config) -> tuple[list[int], np.ndarray]:
    """Rows are player ids (every player we know about), columns are league.horizon."""
    ids = sorted(league.players)
    if not league.horizon:
        return ids, np.zeros((len(ids), 0))
    matrix = np.vstack([expected_points(league.players[pid], league, cfg) for pid in ids])
    return ids, matrix
