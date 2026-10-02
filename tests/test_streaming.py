"""Streaming a position (D/ST): pick up next week's matchup instead of holding one all season."""
import datetime as dt

from fantasyoptimizer.engine import Optimizer
from fantasyoptimizer.state import State
from fantasyoptimizer.valuation import Valuer
from fantasyoptimizer.waivers import find_add_drops

from .helpers import fast_config, make_league, make_player
from .test_moves import full_roster

NOW = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)


def dst(pid, rate, wk1, wk2, **kw):
    """A D/ST with ESPN projections for this week and next (weeks 3-4 aren't out yet)."""
    p = make_player(pid, "D/ST", rate, pro=pid, **kw)
    p.period_projections = {1: wk1, 2: wk2}
    return p


def league_with(my_dst, free_agents):
    league = make_league({1: full_roster(1, 0) + [my_dst], 2: full_roster(2, 100)}, free_agents)
    league.slot_counts[16] = 1   # one D/ST slot
    return league


def ideas_for(league, cfg):
    return {i.add.id: i for i in find_add_drops(
        league, league.teams[1].roster, Valuer(league, cfg), cfg, State(), NOW)}


def test_stream_level_is_the_best_free_agent_in_weeks_espn_projected():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0), dst(61, 5.6, 5.9, 3.3)])
    valuer = Valuer(league, fast_config())
    assert valuer.stream_level["D/ST"] == 7.0          # mean of 6.0 (wk 1) and 8.0 (wk 2)
    sid = next(iter(valuer.streamers))
    assert list(valuer.points[valuer.row[sid]]) == [0.0, 0.0, 7.0, 7.0]


def test_better_average_dst_is_no_reason_to_cut_a_player():
    # Same matchups as ours for the two weeks ESPN projected, better on paper after
    # that. Held all season that's a gain; streamed it's nothing.
    bucs = dst(61, 5.6, 5.9, 3.3)
    league = league_with(dst(50, 4.7, 6.4, 3.3), [bucs, dst(60, 5.6, 6.0, 8.0)])
    cfg = fast_config()
    cfg.streaming.positions = []
    held = ideas_for(league, cfg)
    assert held[61].gain > 0 and held[61].drop.position != "D/ST"   # the old behavior
    cfg.streaming.positions = ["D/ST"]
    assert 61 not in ideas_for(league, cfg)


def test_next_weeks_matchup_swaps_dsts_after_this_weeks_game():
    jets = dst(60, 5.6, 6.0, 8.0, status="WAIVERS")
    league = league_with(dst(50, 4.7, 6.4, 3.3), [jets])
    idea = ideas_for(league, fast_config())[60]
    assert idea.drop.id == 50                        # swap defenses, keep the bench
    assert round(idea.gain, 2) == round(8.0 - 3.3 - (6.4 - 6.0), 2)
    assert idea.wait and idea.later                  # ours is better this week: wait


def test_stream_that_also_wins_this_week_is_made_now():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0)])
    idea = ideas_for(league, fast_config())[60]
    assert idea.drop.id == 50 and not idea.wait


def test_elite_dst_on_your_roster_stays():
    # Ours beats the streaming level every week, so a good matchup elsewhere doesn't move it.
    league = league_with(dst(50, 10.0, 10.0, 9.0), [dst(60, 5.6, 6.0, 8.0)])
    assert 60 not in ideas_for(league, fast_config())


def test_dst_better_than_streaming_is_worth_holding_past_next_week():
    # Tough matchups the next two weeks, but better than what streaming gets after that.
    strong = dst(61, 12.0, 5.0, 5.0)
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0), strong])
    cfg = fast_config()
    later = Valuer(league, cfg).points[Valuer(league, cfg).row[61], 2]   # his blended rate
    assert later > 7.0
    idea = ideas_for(league, cfg)[61]
    assert idea.drop.id == 50 and idea.start_weeks == [1, 2, 3, 4]
    assert round(idea.gain, 2) == round((5 - 6.4) + (5 - 3.3) + 2 * (later - 7.0), 2)


class Client:
    def __init__(self):
        self.sent = []

    def submit(self, payload):
        self.sent.append(payload)
        return {"status": "EXECUTED"}


def run(league, cfg):
    cfg.dry_run = False
    cfg.waivers.min_title_gain = -100
    cfg.trades.enabled = cfg.lineup.enabled = False
    client = Client()
    return Optimizer(league, cfg, State(), client, NOW).run(), client


def test_stream_pickup_uses_its_own_lower_bar():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0)])
    result, client = run(league, fast_config())
    assert result.add_ideas[0].gain < 12                       # under the normal bar...
    assert [a.kind for a in result.actions] == ["add"]         # ...but a stream clears its own


def test_next_weeks_stream_is_not_made_before_this_weeks_game():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0)])
    result, client = run(league, fast_config())
    assert result.add_ideas and result.add_ideas[0].wait
    assert client.sent == []


def test_why_line_names_the_streamer_it_replaces():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0), dst(61, 9.0, 7.0, 5.0)])
    result, _ = run(league, fast_config())
    assert result.actions[0].summary.startswith("Add: D/ST61")
    # Streaming level: best free agent was 7.0 in week 1 (this one) and 8.0 in week 2.
    assert "a streamed D/ST (7.5 pts/gm) 2 wks" in result.actions[0].why


def auto_config():
    cfg = fast_config()
    cfg.waivers.max_moves_per_run = 0   # every other pickup stays manual
    cfg.streaming.auto = True
    return cfg


def test_auto_streams_a_defense_for_a_defense_with_pickups_otherwise_off():
    wr = make_player(70, "WR", 30)      # a huge non-D/ST upgrade it must not make
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0), wr])
    result, client = run(league, auto_config())
    assert any(i.add.id == 70 for i in result.add_ideas)              # suggested...
    assert [a.summary.split(",")[0] for a in result.actions] == ["Add: D/ST60 (D/ST)"]  # not WR70
    assert len(client.sent) == 1
    items = client.sent[0]["items"]
    assert {(i["type"], i["playerId"]) for i in items} == {("ADD", 60), ("DROP", 50)}


def test_auto_off_makes_nothing():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0)])
    cfg = auto_config()
    cfg.streaming.auto = False
    result, client = run(league, cfg)
    assert result.add_ideas and client.sent == []


def test_auto_never_holds_two_defenses_or_cuts_someone_else():
    # With an open bench spot the best idea is to keep ours and add theirs: not automatic.
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0)])
    league.slot_counts[20] += 1
    result, client = run(league, auto_config())
    assert result.add_ideas[0].add.id == 60 and result.add_ideas[0].drop is None
    assert client.sent == []
    # No defense on the roster: the only drop would be another position. Also not automatic.
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [dst(60, 5.6, 7.0, 8.0)])
    league.slot_counts[16] = 1
    league.slot_counts[20] -= 1   # roster stays full
    result, client = run(league, auto_config())
    assert result.add_ideas and result.add_ideas[0].drop.position != "D/ST"
    assert client.sent == []


def test_pending_claim_is_not_filed_again():
    jets = dst(60, 5.6, 7.0, 8.0, status="WAIVERS")
    league = league_with(dst(50, 4.7, 6.4, 3.3), [jets, dst(61, 5.6, 6.9, 7.9)])
    state = State()
    state.record_add_drop(60, 50, NOW - dt.timedelta(days=1))   # yesterday's claim
    ideas = {i.add.id: i for i in find_add_drops(
        league, league.teams[1].roster, Valuer(league, fast_config()), fast_config(), state, NOW)}
    assert 60 not in ideas                      # not claimed twice
    assert 61 not in ideas                      # and the Bills aren't dropped in a second claim


def run_move(league, text, lineup=False):
    cfg = fast_config()
    cfg.dry_run = False
    cfg.trades.enabled = False
    cfg.lineup.enabled = lineup
    client = Client()
    return Optimizer(league, cfg, State(), client, NOW, move=text).run(), client


def test_requested_move_adds_and_drops_exactly_that_and_starts_him():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0), make_player(70, "WR", 30)])
    result, client = run_move(league, "add D/ST60, drop D/ST50", lineup=True)
    add, lineup = client.sent
    assert add["type"] == "FREEAGENT"
    assert {(i["type"], i["playerId"]) for i in add["items"]} == {("ADD", 60), ("DROP", 50)}
    assert "you asked for it" in result.actions[0].summary
    # It's his week now, so the lineup step puts him in the D/ST slot (and no WR70 pickup).
    assert lineup["type"] == "ROSTER"
    assert {"playerId": 60, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 16} in lineup["items"]


def test_requested_move_on_waivers_is_a_claim_and_for_works_too():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 6.0, 8.0, status="WAIVERS")])
    result, client = run_move(league, "D/ST60 for D/ST50")
    assert [p["type"] for p in client.sent] == ["WAIVER"]
    assert result.actions[0].summary.startswith("Waiver claim ($") and "D/ST60" in result.actions[0].summary


def test_requested_move_that_cant_be_made_says_why_and_does_nothing_else():
    league = league_with(dst(50, 4.7, 6.4, 3.3), [dst(60, 5.6, 7.0, 8.0)])
    result, client = run_move(league, "add Nobody, drop D/ST50")
    assert "Couldn't find" in result.move_error and client.sent == []
    result, client = run_move(league, "add D/ST60")          # roster is full
    assert "Say who to drop" in result.move_error and client.sent == []
    from fantasyoptimizer import report
    assert "Couldn't make the move you asked for" in report.render(result)
