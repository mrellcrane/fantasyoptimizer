"""Settings: secrets come from env vars, tuning knobs from optimizer.toml."""
from __future__ import annotations

import datetime as dt
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ProjectionConfig:
    # How much each signal counts toward a player's per-game rate.
    weight_week: float = 0.30      # ESPN's projection for the current week
    weight_ros: float = 0.35       # ESPN's rest-of-season per-game projection
    weight_season: float = 0.10    # ESPN's preseason projection / season_games
    weight_actual: float = 0.25    # Actual fantasy points per game so far
    actual_full_weight_games: float = 6.0
    season_games: int = 17


@dataclass
class ValueConfig:
    bench_weight: float = 0.10     # Credit for bench depth (bye/injury insurance)
    bench_depth: int = 2
    playoff_weight: float = 1.5    # Fantasy playoff weeks count this much more


@dataclass
class SimConfig:
    n_sims: int = 4000
    sd_fraction: float = 0.20      # Weekly score std dev as a fraction of the mean
    min_sd: float = 15.0
    seed: int = 2026


@dataclass
class LineupConfig:
    enabled: bool = True
    min_gain: float = 0.5          # Projected points a lineup change must add this week
    pregame_minutes: int = 90      # --pregame only acts when a game of yours starts this soon


@dataclass
class WaiverConfig:
    enabled: bool = True
    max_moves_per_run: int = 1
    min_gain_points: float = 12.0  # Weighted rest-of-season points (smaller is noise)
    min_title_gain: float = 0.0    # Percentage points of championship odds
    pool_per_position: int = 8     # Free agents considered at each position
    sim_candidates: int = 10
    never_drop: list[str] = field(default_factory=list)
    # Pickups that only fill a bye/injury hole weeks from now wait until closer to then.
    patch_lookahead_weeks: int = 2
    patch_min_near_gain: float = 1.0
    patch_max_start_share: float = 0.34
    hold_days: int = 7             # Don't drop someone we picked up this recently
    readd_cooldown_days: int = 14  # Don't re-add someone we dropped this recently
    faab_max_fraction: float = 0.30
    faab_gain_for_max_bid: float = 60.0
    faab_min_bid: int = 1


@dataclass
class StreamingConfig:
    # Positions you pick up fresh each week for the matchup instead of holding one
    # player all season. ESPN only projects a week or two out, so beyond that the
    # bot assumes the slot scores what the best free agent is projected to.
    positions: list[str] = field(default_factory=lambda: ["D/ST"])
    min_gain_points: float = 2.0   # A streaming pickup only has to add this many points
    auto: bool = False             # Make streaming pickups even when waivers.max_moves_per_run = 0


@dataclass
class TradeConfig:
    enabled: bool = True
    max_proposals_per_run: int = 1
    team_cooldown_days: int = 7
    repeat_cooldown_days: int = 30
    min_gain_points: float = 10.0
    min_title_gain: float = 0.5
    min_partner_gain_points: float = 5.0  # It has to visibly help their lineup too
    min_fairness: float = 0.90     # What they get / what they give, in points above waivers
    max_overpay: float = 1.6
    min_accept_chance: float = 0.30  # Skip offers they'd probably laugh at
    accept_fairness_weight: float = 6.0
    accept_gain_scale: float = 20.0
    pool_size: int = 10
    max_players_per_side: int = 2
    sim_candidates: int = 30
    untouchable: list[str] = field(default_factory=list)
    not_available: list[str] = field(default_factory=list)  # Their players they won't trade
    do_not_trade_with: list[str] = field(default_factory=list)
    # Streamed week to week, so only ever traded straight for the same position
    # (your D/ST for theirs), never packaged with other players.
    swap_only_positions: list[str] = field(default_factory=lambda: ["D/ST"])
    message: str = ""
    pitch_as_message: bool = False  # Send the one-line pitch as the offer's note
    # "Long shots": better for you, less likely to be accepted. Shown in the report;
    # only sent automatically if propose_long_shots is on.
    long_shots: bool = True
    long_shot_min_fairness: float = 0.60
    long_shot_min_accept_chance: float = 0.05
    long_shot_min_partner_gain_points: float = -30.0
    propose_long_shots: bool = False
    expiration_hours: int = 48


@dataclass
class Config:
    league_id: int = 0
    year: int = 0
    team_id: int | None = None
    espn_s2: str | None = None
    swid: str | None = None
    dry_run: bool = True
    projection: ProjectionConfig = field(default_factory=ProjectionConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    lineup: LineupConfig = field(default_factory=LineupConfig)
    waivers: WaiverConfig = field(default_factory=WaiverConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    trades: TradeConfig = field(default_factory=TradeConfig)
    # Questionable players who do play score less (RB/WR ~8.5-10% less, QBs no drop;
    # 4for4 injury study). Multiplier on their projection when they play.
    questionable_performance: dict[str, float] = field(default_factory=lambda: {
        "RB": 0.91, "WR": 0.91, "TE": 0.91,
    })
    # Expected return week for specific injured players, from the news (overrides the
    # generic injury curve): {"A.J. Brown": 7}
    expected_return: dict[str, int] = field(default_factory=dict)
    # injury status -> availability for this week, next week, ... (1.0 after the list ends)
    injury: dict[str, list[float]] = field(default_factory=lambda: {
        "OUT": [0.0, 0.75],
        "DOUBTFUL": [0.3, 0.85],
        "QUESTIONABLE": [0.74],
        "INJURY_RESERVE": [0.0, 0.0, 0.0, 0.0, 0.5],
        "SUSPENSION": [0.0],
    })


def _apply(obj, values: dict, section: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            log.warning("Unknown setting [%s] %s, ignoring", section, key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, key)
        else:
            setattr(obj, key, value)


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def default_season(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year if today.month >= 3 else today.year - 1


def load_config(path: str | Path | None = "optimizer.toml", env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    if path and Path(path).exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        injury = data.pop("injury", None)
        general = data.pop("general", {})
        _apply(cfg, general, "general")
        _apply(cfg, data, "root")
        if injury:
            cfg.injury.update({k.upper(): list(v) for k, v in injury.items()})

    if env.get("ESPN_LEAGUE_ID"):
        cfg.league_id = int(env["ESPN_LEAGUE_ID"])
    if env.get("ESPN_YEAR"):
        cfg.year = int(env["ESPN_YEAR"])
    if env.get("ESPN_TEAM_ID"):
        cfg.team_id = int(env["ESPN_TEAM_ID"])
    cfg.espn_s2 = env.get("ESPN_S2") or cfg.espn_s2
    cfg.swid = env.get("ESPN_SWID") or cfg.swid
    if env.get("DRY_RUN") is not None:
        cfg.dry_run = _truthy(env["DRY_RUN"])
    if not cfg.year:
        cfg.year = default_season()
    return cfg
