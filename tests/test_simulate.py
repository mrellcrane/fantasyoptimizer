from fantasyoptimizer.config import SimConfig
from fantasyoptimizer.simulate import SeasonSimulator, bracket_order

from .helpers import make_league, make_player


def four_team_league(records=None):
    rosters = {t: [make_player(t * 10, "QB", 10 + t)] for t in range(1, 5)}
    return make_league(rosters, records=records)


def test_bracket_order():
    assert bracket_order(2) == [1, 2]
    assert bracket_order(4) == [1, 4, 2, 3]
    assert bracket_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]


def test_odds_are_probabilities():
    league = four_team_league()
    sim = SeasonSimulator(league, SimConfig(n_sims=3000))
    means = {t: {mp: 100.0 + 5 * t for mp in range(1, 5)} for t in league.teams}
    res = sim.run(means)
    assert abs(sum(res.title_odds.values()) - 1.0) < 1e-9
    assert abs(sum(res.playoff_odds.values()) - league.playoff_team_count) < 1e-9
    # Stronger teams do better.
    assert res.title_odds[4] > res.title_odds[3] > res.title_odds[1]


def test_common_random_numbers_make_comparisons_exact():
    league = four_team_league()
    sim = SeasonSimulator(league, SimConfig(n_sims=2000))
    means = {t: {mp: 100.0 for mp in range(1, 5)} for t in league.teams}
    a, b = sim.run(means), sim.run(means)
    assert a.title_odds == b.title_odds
    better = {**means, 1: {mp: 110.0 for mp in range(1, 5)}}
    assert sim.run(better).title_odds[1] > a.title_odds[1]


def test_existing_record_counts():
    league = four_team_league(records={1: (8, 0), 2: (0, 8), 3: (0, 8), 4: (0, 8)})
    sim = SeasonSimulator(league, SimConfig(n_sims=2000))
    means = {t: {mp: 100.0 for mp in range(1, 5)} for t in league.teams}
    assert sim.run(means).playoff_odds[1] == 1.0


def test_mid_playoffs_uses_current_bracket():
    league = four_team_league()
    league.current_matchup_period = league.current_scoring_period = 4
    league.schedule = [m for m in league.schedule if m.period < 3]
    from fantasyoptimizer.models import Matchup
    league.schedule.append(Matchup(period=4, home=2, away=3, playoff_tier="WINNERS_BRACKET"))
    sim = SeasonSimulator(league, SimConfig(n_sims=1000))
    means = {t: {mp: 100.0 for mp in range(1, 5)} for t in league.teams}
    res = sim.run(means)
    assert res.title_odds[1] == res.title_odds[4] == 0.0
    assert abs(res.title_odds[2] + res.title_odds[3] - 1.0) < 1e-9
