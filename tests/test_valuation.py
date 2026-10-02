import random

import numpy as np
import pytest

from fantasyoptimizer.config import Config
from fantasyoptimizer.projections import availability, per_game_rate
from fantasyoptimizer.valuation import Valuer

from .helpers import fast_config, make_league, make_player


def brute_force(valuer, roster, column):
    """Try every way to fill each slot with an eligible player or leave it empty."""
    def best(slot_i, used):
        if slot_i == len(valuer.slots):
            return 0.0
        slot = valuer.slots[slot_i]
        top = best(slot_i + 1, used)
        for pid in roster:
            if pid not in used and slot in valuer.league.players[pid].eligible_slots:
                pts = max(0.0, valuer.points[valuer.row[pid], column])
                top = max(top, pts + best(slot_i + 1, used | {pid}))
        return top
    return best(0, frozenset())


def test_solver_matches_brute_force():
    rng = random.Random(1)
    for trial in range(25):
        roster = [make_player(10 * trial + i, rng.choice(["QB", "RB", "WR", "TE"]), rng.uniform(0, 25))
                  for i in range(6)]
        league = make_league({1: roster, 2: []})
        valuer = Valuer(league, fast_config())
        ids = [p.id for p in roster]
        total, assignment = valuer.solve(ids, column=0)
        assert abs(total - brute_force(valuer, ids, 0)) < 1e-6
        # Every assigned player is eligible for their slot, and no slot is used twice.
        assert len(set(assignment.values())) == len(assignment)
        for pid, j in assignment.items():
            assert valuer.slots[j] in league.players[pid].eligible_slots


def test_bye_week_depth_has_value():
    # Two WRs on different byes: the backup matters exactly on the starter's bye.
    wr_a = make_player(1, "WR", 20, pro=1)
    wr_b = make_player(2, "WR", 10, pro=2)
    league = make_league({1: [wr_a, wr_b], 2: []}, byes={1: 2, 2: 3})
    valuer = Valuer(league, fast_config())
    weekly = valuer.weekly_points([1, 2])
    # WR slot + FLEX slot: both start except on byes.
    assert list(np.round(weekly, 6)) == [30, 10, 20, 30]
    assert valuer.value([1, 2]) > valuer.value([1])


def test_rate_blends_signals_and_handles_missing():
    cfg = Config()
    p = make_player(1, "RB", 12.0)
    league = make_league({1: [p], 2: []})
    assert per_game_rate(p, league, cfg) == pytest.approx(12.0)
    p.period_projections[1] = 18.0
    blended = per_game_rate(p, league, cfg)
    assert 12.0 < blended < 18.0
    p.ros_rate, p.period_projections = None, {}
    assert per_game_rate(p, league, cfg) == 0.0


def test_injury_availability_curve():
    cfg = Config()
    p = make_player(1, "RB", 10, injury="INJURY_RESERVE")
    assert [availability(p, k, cfg) for k in range(6)] == [0, 0, 0, 0, 0.5, 1.0]
    p.injury_status = "ACTIVE"
    assert availability(p, 0, cfg) == 1.0


def test_playoff_weeks_weighted_up():
    cfg = fast_config()
    cfg.value.playoff_weight = 2.0
    p = make_player(1, "QB", 10)
    league = make_league({1: [p], 2: []})
    # Weeks 1-2 count once, playoff weeks 3-4 count double.
    assert Valuer(league, cfg).value([1]) == 10 + 10 + 20 + 20


def test_espn_weekly_projections_are_used_as_is_and_estimates_get_injury_discount():
    from fantasyoptimizer.projections import expected_points
    cfg = Config()
    p = make_player(1, "TE", 10, injury="DOUBTFUL")
    p.period_projections = {1: 0.0, 2: 11.0}   # ESPN: out this week, back next week
    league = make_league({1: [p], 2: []})
    pts = expected_points(p, league, cfg)
    assert pts[0] == 0.0 and pts[1] == 11.0   # ESPN's numbers, untouched
    del p.period_projections[2]                # no ESPN number: the bot estimates and discounts
    assert expected_points(p, league, cfg)[1] == pytest.approx(10 * 0.85)


def test_questionable_players_this_week_carry_their_risk():
    from fantasyoptimizer.projections import expected_points
    cfg = Config()
    p = make_player(1, "LB", 19, injury="QUESTIONABLE")
    p.period_projections = {1: 19.3, 2: 19.2}
    league = make_league({1: [p], 2: []})
    pts = expected_points(p, league, cfg)
    assert pts[0] == pytest.approx(19.3 * 0.74)  # ~74% of Questionable players play
    assert pts[1] == 19.2                        # next week: ESPN's number as is
    wr = make_player(2, "WR", 15, injury="QUESTIONABLE")
    wr.period_projections = {1: 15.0}
    assert expected_points(wr, league, cfg)[0] == pytest.approx(15.0 * 0.74 * 0.91)  # and score less
    qb = make_player(3, "QB", 20, injury="QUESTIONABLE")
    qb.period_projections = {1: 20.0}
    assert expected_points(qb, league, cfg)[0] == pytest.approx(20.0 * 0.74)  # QBs don't drop off


def test_questionable_player_is_active_once_inactives_are_out():
    import datetime as dt
    from fantasyoptimizer.projections import expected_points
    cfg = Config()
    p = make_player(1, "WR", 15, injury="QUESTIONABLE", pro=1)
    p.period_projections = {1: 15.0}
    league = make_league({1: [p], 2: []})
    kickoff = dt.datetime(2026, 10, 4, 17, tzinfo=dt.timezone.utc)
    league.kickoffs = {1: {1: kickoff}}
    league.loaded_at = kickoff - dt.timedelta(days=2)     # midweek: might sit
    assert expected_points(p, league, cfg)[0] == pytest.approx(15.0 * 0.74 * 0.91)
    league.loaded_at = kickoff - dt.timedelta(minutes=60)  # inactives out, still listed Q: active
    assert expected_points(p, league, cfg)[0] == pytest.approx(15.0 * 0.91)
