"""Tiny hand-built leagues for tests."""
from __future__ import annotations

from fantasyoptimizer.config import Config
from fantasyoptimizer.models import League, Matchup, Player, Team

ELIGIBLE = {
    "QB": [0, 7, 20, 21],
    "RB": [2, 3, 23, 7, 20, 21],
    "WR": [4, 3, 5, 23, 7, 20, 21],
    "TE": [6, 5, 23, 7, 20, 21],
    "LB": [10, 15, 20, 21],
    "D/ST": [16, 20, 21],
}
# QB, RB, WR, FLEX, 2 bench, 1 IR
SLOTS = {0: 1, 2: 1, 4: 1, 23: 1, 20: 2, 21: 1}


def make_player(pid: int, pos: str, rate: float, *, team: int = 0, slot: int | None = None,
                pro: int = 1, injury: str = "ACTIVE", locked: bool = False,
                status: str | None = None) -> Player:
    return Player(
        id=pid, name=f"{pos}{pid}", position=pos, pro_team_id=pro,
        eligible_slots=frozenset(ELIGIBLE[pos]), injury_status=injury,
        status=status or ("ONTEAM" if team else "FREEAGENT"), team_id=team,
        lineup_slot=(slot if slot is not None else 20) if team else None,
        lineup_locked=locked, ros_rate=rate,
    )


def make_league(rosters: dict[int, list[Player]], free_agents: list[Player] = (), *,
                my_team: int = 1, byes: dict[int, int] | None = None,
                records: dict[int, tuple[int, int]] | None = None) -> League:
    """Four-week season: weeks 1-2 regular season, 3-4 playoffs (top 2 teams)."""
    players, teams = {}, {}
    for tid, roster in rosters.items():
        for p in roster:
            p.team_id, p.status = tid, "ONTEAM"
            if p.lineup_slot is None:
                p.lineup_slot = 20
            players[p.id] = p
        w, l = (records or {}).get(tid, (0, 0))
        teams[tid] = Team(id=tid, name=f"Team {tid}", abbrev=f"T{tid}", owners=(f"{{O{tid}}}",),
                          wins=w, losses=l, ties=0, points_for=100.0 * w,
                          roster=[p.id for p in roster], faab_remaining=100)
    for p in free_agents:
        players[p.id] = p
    ids = sorted(teams)
    schedule = []
    for week in (1, 2):
        rot = ids[week - 1:] + ids[:week - 1]
        for i in range(len(rot) // 2):
            schedule.append(Matchup(period=week, home=rot[i], away=rot[-1 - i]))
    pro_games = {}
    for pro, bye in (byes or {}).items():
        pro_games[pro] = {w: (0 if w == bye else 1) for w in range(1, 5)}
    return League(
        id=1, year=2026, name="Test League", current_scoring_period=1, current_matchup_period=1,
        final_scoring_period=4, reg_season_periods=2, matchup_periods={w: [w] for w in range(1, 5)},
        playoff_team_count=2, slot_counts=dict(SLOTS), teams=teams, players=players,
        schedule=schedule, my_team_id=my_team, member_id="{ME}", uses_faab=True, faab_budget=100,
        pro_games=pro_games,
    )


def fast_config() -> Config:
    cfg = Config()
    cfg.sim.n_sims = 1500
    cfg.value.bench_weight = 0.0
    cfg.value.playoff_weight = 1.0
    return cfg
