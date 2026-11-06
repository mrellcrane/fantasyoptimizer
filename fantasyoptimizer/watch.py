"""Free agent watch: ping when a pickup worth making shows up.

Managers drop players all day. The full run looks for pickups once a day; the watch
runs every 15 minutes, finds the same pickups with the same rules, and sends an alert
the first time each one qualifies. It never makes moves itself.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import waivers
from .config import Config
from .engine import RunResult

log = logging.getLogger(__name__)


@dataclass
class WatchMemory:
    """Pickups already alerted on: player id -> {"date", "status"}."""
    alerted: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "WatchMemory":
        path = Path(path)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            log.warning("Watch memory %s is unreadable; starting fresh", path)
            data = {}
        return cls(alerted=data.get("alerted", {}), path=path)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"alerted": self.alerted}, indent=2, sort_keys=True))

    def is_new(self, move: waivers.AddDrop, cooldown_days: int, now: dt.datetime) -> bool:
        seen = self.alerted.get(str(move.add.id))
        if not seen:
            return True
        if now - dt.datetime.fromisoformat(seen["date"]) >= dt.timedelta(days=cooldown_days):
            return True
        # He cleared waivers: now it's first come, first served, so say so again.
        return seen.get("status") == "WAIVERS" and move.add.status != "WAIVERS"

    def record(self, moves: list[waivers.AddDrop], now: dt.datetime) -> None:
        for m in moves:
            self.alerted[str(m.add.id)] = {"date": now.isoformat(), "status": m.add.status,
                                           "name": m.add.name}
        cutoff = now - dt.timedelta(days=30)
        self.alerted = {k: v for k, v in self.alerted.items()
                        if dt.datetime.fromisoformat(v["date"]) > cutoff}


def worth_it(result: RunResult, cfg: Config, skip_streams: bool = False) -> list[waivers.AddDrop]:
    """Pickups that clear the same bar the full run uses to make a move.

    skip_streams: leave out D/ST-style streams, because the daily run makes those
    itself (streaming.auto).
    """
    out = []
    for i in result.add_ideas:
        if i.later or i.title_gain is None:
            continue
        if skip_streams and waivers.streamed(i.add, cfg):
            continue
        if i.gain >= waivers.min_gain(i, cfg) and i.title_gain >= cfg.waivers.min_title_gain:
            out.append(i)
    return out


def new_alerts(result: RunResult, cfg: Config, memory: WatchMemory, now: dt.datetime,
               skip_streams: bool = False) -> list[waivers.AddDrop]:
    good = [m for m in worth_it(result, cfg, skip_streams)
            if memory.is_new(m, cfg.watch.cooldown_days, now)]
    return good[:cfg.watch.max_alerts]


def alert_text(result: RunResult, moves: list[waivers.AddDrop]) -> str:
    league = result.league
    lines = [f"Pickup alert for {league.my_team.name}:"]
    for m in moves:
        team = league.pro_team_abbrevs.get(m.add.pro_team_id, "")
        who = f"{m.add.name} ({m.add.position}{', ' + team if team else ''})"
        drop = f", drop {m.drop}" if m.drop else ""
        if m.waiver:
            how = f"On waivers: put in a claim{f' (bid ${m.bid})' if league.uses_faab else ''}"
        else:
            how = "Free agent: add now, first come first served"
        lines.append(f"- Add {who}{drop}: +{m.gain:.1f} season pts, "
                     f"{m.title_gain:+.1f}% title odds. {how}.")
    return "\n".join(lines)
