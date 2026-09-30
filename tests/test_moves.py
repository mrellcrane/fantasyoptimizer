import datetime as dt

from fantasyoptimizer.state import State
from fantasyoptimizer.trades import find_trades
from fantasyoptimizer.valuation import Valuer
from fantasyoptimizer.waivers import find_add_drops

from .helpers import fast_config, make_league, make_player

NOW = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)


def full_roster(team, base):
    """QB, RB, WR, TE-ish flex, two bench: 6 players = roster limit."""
    return [make_player(base + 1, "QB", 18), make_player(base + 2, "RB", 14),
            make_player(base + 3, "WR", 14), make_player(base + 4, "RB", 10),
            make_player(base + 5, "WR", 6), make_player(base + 6, "RB", 4)]


def test_finds_obvious_pickup_and_drops_worst_player():
    fa = make_player(900, "WR", 16)
    junk = make_player(901, "RB", 1)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [fa, junk])
    cfg = fast_config()
    ideas = find_add_drops(league, league.teams[1].roster, Valuer(league, cfg), cfg, State(), NOW)
    assert ideas[0].add.id == 900
    assert ideas[0].drop.id == 6        # the 4-point RB, not a starter
    assert all(i.add.id != 901 for i in ideas)  # junk never beats anyone


def test_never_drop_and_hold_period_respected():
    fa = make_player(900, "WR", 16)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [fa])
    cfg = fast_config()
    cfg.waivers.never_drop = ["RB6"]
    state = State(adds={"5": (NOW - dt.timedelta(days=2)).isoformat()})
    ideas = find_add_drops(league, league.teams[1].roster, Valuer(league, cfg), cfg, state, NOW)
    assert ideas[0].drop.id not in (5, 6)


def test_waiver_players_get_faab_bid():
    fa = make_player(900, "WR", 16, status="WAIVERS")
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [fa])
    cfg = fast_config()
    idea = find_add_drops(league, league.teams[1].roster, Valuer(league, cfg), cfg, State(), NOW)[0]
    assert idea.waiver and 1 <= idea.bid <= 30


def lopsided_league():
    # We're stacked at RB and thin at WR; they're the opposite. Both should want a swap.
    mine = [make_player(1, "QB", 15), make_player(2, "RB", 16), make_player(3, "RB", 15),
            make_player(4, "RB", 14), make_player(5, "WR", 5), make_player(6, "WR", 4)]
    theirs = [make_player(11, "QB", 15), make_player(12, "WR", 16), make_player(13, "WR", 15),
              make_player(14, "WR", 14), make_player(15, "RB", 5), make_player(16, "RB", 4)]
    return make_league({1: mine, 2: theirs})


def test_finds_win_win_trade():
    league = lopsided_league()
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    rosters = {t: list(team.roster) for t, team in league.teams.items()}
    ideas = find_trades(league, rosters, Valuer(league, cfg), cfg, State(), NOW)
    assert ideas
    best = ideas[0]
    assert best.partner == 2 and best.my_gain > 0 and best.partner_gain >= 0
    gives = {league.players[p].position for p in best.give}
    gets = {league.players[p].position for p in best.get}
    assert "RB" in gives and gets == {"WR"}
    assert best.accept_chance >= cfg.trades.min_accept_chance


def test_trade_respects_untouchable_cooldown_and_pending():
    league = lopsided_league()
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    rosters = {t: list(team.roster) for t, team in league.teams.items()}
    valuer = Valuer(league, cfg)

    cfg.trades.untouchable = ["RB2", "RB3", "RB4"]
    assert not find_trades(league, rosters, valuer, cfg, State(), NOW)
    cfg.trades.untouchable = []

    state = State()
    state.record_proposal(2, (2,), (12,), NOW - dt.timedelta(days=1))
    assert not find_trades(league, rosters, valuer, cfg, state, NOW)

    cfg.trades.do_not_trade_with = ["Team 2"]
    assert not find_trades(league, rosters, valuer, cfg, State(), NOW)


def test_idp_slot_gets_filled_by_pickup():
    # Leagues with a DP slot: an empty DP slot makes a linebacker a real pickup.
    fa_lb = make_player(900, "LB", 12)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [fa_lb])
    league.slot_counts[15] = 1
    league.slot_counts[20] = 1   # keep the roster limit the same
    cfg = fast_config()
    valuer = Valuer(league, cfg)
    assert 15 in valuer.slots
    ideas = find_add_drops(league, league.teams[1].roster, valuer, cfg, State(), NOW)
    assert ideas[0].add.id == 900 and ideas[0].gain == 12 * 4
