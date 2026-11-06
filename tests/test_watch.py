import datetime as dt

from fantasyoptimizer import cli, watch
from fantasyoptimizer.engine import Optimizer
from fantasyoptimizer.state import State

from .helpers import fast_config, make_league, make_player
from .test_moves import full_roster

NOW = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)


def watch_config():
    cfg = fast_config()
    cfg.waivers.max_moves_per_run = 0     # what --watch sets: alerts only
    cfg.streaming.auto = False
    cfg.trades.enabled = False
    cfg.lineup.enabled = False
    return cfg


def run(fa_status="FREEAGENT"):
    fa = make_player(900, "WR", 16, status=fa_status)
    league = make_league({1: full_roster(1, 0), 2: full_roster(2, 100)}, [fa])
    cfg = watch_config()
    return Optimizer(league, cfg, State(), None, NOW).run(), cfg


def test_alerts_on_a_pickup_worth_making_and_makes_no_moves():
    result, cfg = run()
    assert not result.actions
    alerts = watch.new_alerts(result, cfg, watch.WatchMemory(), NOW)
    assert [m.add.id for m in alerts] == [900]
    text = watch.alert_text(result, alerts)
    assert "WR900" in text and "drop RB6" in text and "Free agent" in text


def test_pings_once_then_again_after_cooldown():
    result, cfg = run()
    memory = watch.WatchMemory()
    memory.record(watch.new_alerts(result, cfg, memory, NOW), NOW)
    assert watch.new_alerts(result, cfg, memory, NOW + dt.timedelta(hours=1)) == []
    later = NOW + dt.timedelta(days=cfg.watch.cooldown_days)
    assert [m.add.id for m in watch.new_alerts(result, cfg, memory, later)] == [900]


def test_pings_again_when_he_clears_waivers():
    claimed, cfg = run(fa_status="WAIVERS")
    memory = watch.WatchMemory()
    alerts = watch.new_alerts(claimed, cfg, memory, NOW)
    assert "On waivers" in watch.alert_text(claimed, alerts)
    memory.record(alerts, NOW)
    cleared, _ = run(fa_status="FREEAGENT")
    assert [m.add.id for m in watch.new_alerts(cleared, cfg, memory, NOW)] == [900]


def test_small_upgrades_are_not_worth_a_ping():
    result, cfg = run()
    cfg.waivers.min_gain_points = 10_000
    assert watch.new_alerts(result, cfg, watch.WatchMemory(), NOW) == []


def test_skips_streams_the_daily_run_makes():
    result, cfg = run()
    cfg.streaming.positions = ["WR"]   # as if you streamed WRs like a defense
    assert [m.add.id for m in watch.worth_it(result, cfg)] == [900]
    assert watch.worth_it(result, cfg, skip_streams=True) == []


def test_memory_round_trip(tmp_path):
    result, cfg = run()
    memory = watch.WatchMemory.load(tmp_path / "watch.json")
    memory.record(watch.new_alerts(result, cfg, memory, NOW), NOW)
    memory.save()
    again = watch.WatchMemory.load(tmp_path / "watch.json")
    assert watch.new_alerts(result, cfg, again, NOW) == []


def test_cli_watch_pings_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "notify", lambda text, url: sent.append(text))
    monkeypatch.setenv("NOTIFY_URL", "https://ntfy.sh/test")
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    args = ["--demo", "--watch", "--state", str(tmp_path / "s.json"),
            "--watch-state", str(tmp_path / "w.json"),
            "--report", str(tmp_path / "r.md"), "--snapshot", str(tmp_path / "snap.json")]
    assert cli.main(args) == 0
    assert len(sent) == 1 and sent[0].startswith("Pickup alert")
    assert "D/ST" not in sent[0]   # optimizer.toml streams D/ST automatically
    assert "report_kind=watch" in out.read_text() and "watch_changed=true" in out.read_text()
    (tmp_path / "snap.json").unlink()
    assert cli.main(args) == 0     # nothing new: quiet, no report
    assert len(sent) == 1 and not (tmp_path / "snap.json").exists()
