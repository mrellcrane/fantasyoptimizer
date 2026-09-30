import datetime as dt

import pytest

from fantasyoptimizer import espn, report
from fantasyoptimizer.config import Config, load_config
from fantasyoptimizer.demo import demo_data, demo_league
from fantasyoptimizer.engine import Optimizer
from fantasyoptimizer.models import BENCH_SLOT, IR_SLOT
from fantasyoptimizer.state import State

NOW = dt.datetime(2026, 9, 30, 15, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, fail_on: str | None = None):
        self.sent = []
        self.fail_on = fail_on

    def submit(self, payload):
        self.sent.append(payload)
        if payload["type"] == self.fail_on:
            raise espn.EspnError("409 TRAN_LINEUP_LOCKED", 409)
        return {"status": "EXECUTED" if payload["type"] in ("FREEAGENT", "ROSTER") else "PENDING"}


def config(dry_run=True):
    cfg = Config(dry_run=dry_run)
    cfg.sim.n_sims = 1500
    cfg.trades.pool_size = 6
    return cfg


def no_none(obj):
    if isinstance(obj, dict):
        return all(v is not None and no_none(v) for v in obj.values())
    if isinstance(obj, list):
        return all(no_none(v) for v in obj)
    return True


def test_demo_parses():
    league = demo_league()
    assert league.my_team.name == "Claude's Crushers"
    assert league.roster_limit == 16
    assert league.horizon == list(range(4, 18))
    assert league.playoff_periods == [15, 16, 17]
    assert len(league.pending_trades) == 1
    p = league.players[league.my_team.roster[0]]
    assert p.ros_rate and p.season_projection and 4 in p.period_projections
    pro = p.pro_team_id
    bye = next(w for w in range(1, 19) if league.games(pro, w) == 0)
    assert 5 <= bye <= 14


def test_dry_run_sends_nothing():
    client = FakeClient()
    result = Optimizer(demo_league(), config(), State(), client, NOW).run()
    assert client.sent == []
    assert result.actions and all(not a.executed for a in result.actions)
    text = report.render(result)
    assert "Dry run" in text and "League outlook" in text
    for action in result.actions:
        if action.kind in ("add", "waiver", "trade"):
            assert "would start" in action.why and "pts/gm" in action.why
            assert f"Why: {action.why}" in text
    assert "\u2014" not in text  # no em-dashes in anything we show


def test_live_run_sends_valid_payloads_and_records_state(tmp_path):
    client = FakeClient()
    state = State(path=tmp_path / "state.json")
    league = demo_league()
    result = Optimizer(league, config(dry_run=False), state, client, NOW).run()
    kinds = [p["type"] for p in client.sent]
    assert kinds[0] in ("FREEAGENT", "WAIVER")
    assert "ROSTER" in kinds and "TRADE_PROPOSAL" in kinds
    for payload in client.sent:
        assert no_none(payload)
        assert payload["teamId"] == league.my_team_id
        assert payload["scoringPeriodId"] == league.current_scoring_period
        assert payload["executionType"] == "EXECUTE"
    lineup = next(p for p in client.sent if p["type"] == "ROSTER")
    for item in lineup["items"]:
        player = league.players[item["playerId"]]
        assert not player.lineup_locked
        assert item["fromLineupSlotId"] != item["toLineupSlotId"]
        assert IR_SLOT not in (item["fromLineupSlotId"], item["toLineupSlotId"])
    trade = next(p for p in client.sent if p["type"] == "TRADE_PROPOSAL")
    assert trade["expirationDate"].endswith("Z")
    assert all(a.ok for a in result.actions)
    state.save()
    saved = State.load(tmp_path / "state.json")
    assert saved.proposals and saved.adds


def test_lineup_benches_players_on_bye_or_out():
    league = demo_league()
    cfg = config(dry_run=False)
    cfg.waivers.enabled = cfg.trades.enabled = False
    client = FakeClient()
    Optimizer(league, cfg, State(), client, NOW).run()
    moves = client.sent[0]["items"] if client.sent else []
    after = {p: league.players[p].lineup_slot for p in league.my_team.roster}
    for m in moves:
        after[m["playerId"]] = m["toLineupSlotId"]
    for pid, slot in after.items():
        p = league.players[pid]
        if slot not in (BENCH_SLOT, IR_SLOT) and not p.lineup_locked:
            assert p.period_projections.get(4, 0) > 0, f"{p} starts with no projection"


def test_failed_write_is_reported_not_raised():
    client = FakeClient(fail_on="ROSTER")
    cfg = config(dry_run=False)
    cfg.waivers.enabled = cfg.trades.enabled = False
    result = Optimizer(demo_league(), cfg, State(), client, NOW).run()
    assert result.actions[0].executed and result.actions[0].ok is False
    assert "FAILED" in report.render(result)


def test_trade_deadline_skips_trades():
    league = demo_league()
    league.trade_deadline = NOW - dt.timedelta(days=1)
    client = FakeClient()
    result = Optimizer(league, config(dry_run=False), State(), client, NOW).run()
    assert all(p["type"] != "TRADE_PROPOSAL" for p in client.sent)
    assert any("deadline" in n for n in result.notes)


def test_incoming_offer_is_evaluated():
    result = Optimizer(demo_league(), config(), State(), None, NOW).run()
    assert len(result.incoming) == 1
    offer = result.incoming[0]
    assert offer.partner == 3 and offer.give and offer.get


def test_payload_shapes():
    league = demo_league()
    fa = espn.add_drop_payload(league, 1, 2)
    assert fa["type"] == "FREEAGENT" and "bidAmount" not in fa
    assert fa["items"] == [{"playerId": 1, "type": "ADD", "toTeamId": 1},
                           {"playerId": 2, "type": "DROP", "fromTeamId": 1}]
    claim = espn.add_drop_payload(league, 1, None, waiver=True, bid=7)
    assert claim["type"] == "WAIVER" and claim["bidAmount"] == 7
    league.uses_faab = False
    assert "bidAmount" not in espn.add_drop_payload(league, 1, None, waiver=True, bid=7)
    trade = espn.trade_payload(league, 3, [1], [2, 3], [4], now=NOW)
    assert trade["expirationDate"] == "2026-10-02T15:00:00.000Z"
    assert [i["type"] for i in trade["items"]] == ["TRADE", "TRADE", "TRADE", "DROP"]
    assert espn.lineup_payload(league, [(1, 20, 20), (2, 20, 4)])["items"] == [
        {"playerId": 2, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 4}]


def test_config_file_and_env(tmp_path):
    path = tmp_path / "optimizer.toml"
    path.write_text('[general]\ndry_run = false\n[trades]\nmin_gain_points = 3.5\n'
                    'untouchable = ["Some Guy"]\n[injury]\nquestionable = [0.5]\n')
    cfg = load_config(path, env={"ESPN_LEAGUE_ID": "99", "DRY_RUN": "true", "ESPN_YEAR": "2026"})
    assert cfg.league_id == 99 and cfg.year == 2026 and cfg.dry_run is True
    assert cfg.trades.min_gain_points == 3.5 and cfg.trades.untouchable == ["Some Guy"]
    assert cfg.injury["QUESTIONABLE"] == [0.5] and "OUT" in cfg.injury


def test_team_detected_from_swid_or_error():
    from fantasyoptimizer.models import parse_league
    data, fas, pro = demo_data()
    assert parse_league(data, fas, pro, swid="demo-0000-swid").my_team_id == 1
    assert parse_league(data, fas, pro, team_id=4).my_team_id == 4
    with pytest.raises(ValueError):
        parse_league(data, fas, pro, swid="{NOBODY}")


def test_waiver_claim_does_not_change_todays_lineup():
    league = demo_league()
    for p in league.players.values():
        if p.available:
            p.status = "WAIVERS"
    cfg = config(dry_run=False)
    cfg.trades.enabled = False
    client = FakeClient()
    Optimizer(league, cfg, State(), client, NOW).run()
    claim = next(p for p in client.sent if p["type"] == "WAIVER")
    claimed = next(i["playerId"] for i in claim["items"] if i["type"] == "ADD")
    for payload in (p for p in client.sent if p["type"] == "ROSTER"):
        assert claimed not in {i["playerId"] for i in payload["items"]}
