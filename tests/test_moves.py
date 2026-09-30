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


def test_bye_week_filler_is_a_patch_not_a_pickup():
    # Everyone we own is on bye in week 3. A scrub who'd only start that week should
    # wait; a real upgrade should not.
    scrub = make_player(900, "WR", 5, pro=3)
    stud = make_player(901, "WR", 16, pro=3)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [scrub, stud], byes={1: 3})
    cfg = fast_config()
    cfg.waivers.min_gain_points = 1
    ideas = {i.add.id: i for i in find_add_drops(
        league, league.teams[1].roster, Valuer(league, cfg), cfg, State(), NOW)}
    assert ideas[900].patch and ideas[900].start_weeks == [3]
    assert not ideas[901].patch and len(ideas[901].start_weeks) == 4


def test_patch_pickups_are_not_made_today():
    from fantasyoptimizer.engine import Optimizer
    scrub = make_player(900, "WR", 5, pro=3)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [scrub], byes={1: 3})
    cfg = fast_config()
    cfg.dry_run = False
    cfg.waivers.min_gain_points, cfg.waivers.min_title_gain = 1, -100
    cfg.trades.enabled = cfg.lineup.enabled = False
    sent = []

    class Client:
        def submit(self, payload):
            sent.append(payload)
            return {"status": "EXECUTED"}

    result = Optimizer(league, cfg, State(), Client(), NOW).run()
    assert result.add_ideas and result.add_ideas[0].patch
    assert sent == []


def test_players_in_our_pending_offers_are_not_offered_again():
    from fantasyoptimizer.models import PendingTrade
    league = lopsided_league()
    league.teams[3] = league.teams[2]  # a second partner with the same roster needs
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    rosters = {t: list(team.roster) for t, team in league.teams.items()}
    ideas = find_trades(league, rosters, Valuer(league, cfg), cfg, State(), NOW)
    star = ideas[0].give[0]
    league.pending_trades = [PendingTrade(id="x", proposer=1, items=[
        {"playerId": star, "type": "TRADE", "fromTeamId": 1, "toTeamId": 2}])]
    again = find_trades(league, rosters, Valuer(league, cfg), cfg, State(), NOW)
    assert again and all(star not in i.give for i in again)
    assert all(i.partner != 2 for i in again)


def test_players_you_can_get_on_waivers_are_poor_trade_bait():
    # Same lopsided setup, but an RB as good as ours sits on waivers: our RB depth
    # is worth nothing to them, so there's no fair RB-for-WR deal left.
    league = lopsided_league()
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    rosters = {t: list(team.roster) for t, team in league.teams.items()}
    assert find_trades(league, rosters, Valuer(league, cfg), cfg, State(), NOW)
    free_rb = make_player(950, "RB", 16)
    league.players[free_rb.id] = free_rb
    assert not find_trades(league, rosters, Valuer(league, cfg), cfg, State(), NOW)


def test_pitch_sells_the_deal_from_their_side():
    from fantasyoptimizer.engine import Optimizer
    league = lopsided_league()
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    opt = Optimizer(league, cfg, State(), None, NOW)
    idea = find_trades(league, opt.rosters, opt.valuer, cfg, State(), NOW)[0]
    line = opt.pitch(idea)
    received = [league.players[pid].name for pid in idea.give]
    assert line.endswith(".") and "for you" in line
    assert any(name in line for name in received)
    for pid in idea.get:  # never claims one of our new players "over" someone they're losing
        assert f"over {league.players[pid].name}" not in line


def test_long_shots_loosen_what_the_other_team_must_like():
    league = lopsided_league()
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    cfg.trades.min_partner_gain_points = 1000  # nothing helps them enough for the normal list
    rosters = {t: list(team.roster) for t, team in league.teams.items()}
    valuer = Valuer(league, cfg)
    assert not find_trades(league, rosters, valuer, cfg, State(), NOW)
    bold = find_trades(league, rosters, valuer, cfg, State(), NOW, long_shot=True)
    assert bold and all(i.partner_gain >= cfg.trades.long_shot_min_partner_gain_points for i in bold)
    assert [i.my_gain for i in bold] == sorted((i.my_gain for i in bold), reverse=True)


def test_next_week_hole_with_no_backup_suggests_a_free_agent():
    from fantasyoptimizer.engine import Optimizer
    qb = make_player(1, "QB", 20, slot=0, pro=1)     # our only QB, on bye next week
    rest = [make_player(2, "RB", 14, slot=2, pro=2), make_player(3, "WR", 14, slot=4, pro=2),
            make_player(4, "RB", 10, slot=23, pro=2)]
    fa_qb = make_player(900, "QB", 12, pro=3)
    league = make_league({1: [qb] + rest, 2: full_roster(2, 100)}, [fa_qb], byes={1: 2})
    check = Optimizer(league, fast_config(), State(), None, NOW).lineup_check()
    assert not check.swaps
    assert [(h.player, h.reason, h.fill, h.free_agent) for h in check.holes] == [(1, "on bye", None, 900)]


def test_high_scoring_position_does_not_crowd_out_other_pickups():
    # 70 free-agent linebackers outscore everyone, but a WR upgrade still gets found.
    lbs = [make_player(1000 + i, "LB", 18) for i in range(70)]
    wr = make_player(900, "WR", 16)
    mine = full_roster(1, 0)[:5] + [make_player(7, "LB", 19, slot=15)]
    league = make_league({1: mine, 2: full_roster(2, 100)}, lbs + [wr])
    league.slot_counts[15] = 1
    cfg = fast_config()
    ideas = find_add_drops(league, league.teams[1].roster, Valuer(league, cfg), cfg, State(), NOW)
    assert any(i.add.id == 900 for i in ideas)


def test_pitch_names_who_they_actually_start():
    from fantasyoptimizer.engine import Optimizer
    league = lopsided_league()
    # They start their worse RB (RB16) and bench the better one: the pitch should name
    # the player actually in their lineup, not the one the model would start.
    theirs = league.teams[2].roster
    for pid in theirs:
        league.players[pid].lineup_slot = 20
    league.players[16].lineup_slot = 2   # their 4-point RB is in the RB slot
    league.players[15].lineup_slot = 20  # the 5-point RB sits
    cfg = fast_config()
    cfg.trades.min_gain_points = 5
    opt = Optimizer(league, cfg, State(), None, NOW)
    idea = next(i for i in find_trades(league, opt.rosters, opt.valuer, cfg, State(), NOW)
                if any(league.players[pid].position == "RB" for pid in i.give)
                and 16 not in i.get)
    assert "over RB16" in opt.pitch(idea)
