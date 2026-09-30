"""Memory between daily runs, so the bot doesn't spam or churn."""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def trade_key(partner: int, give, get) -> str:
    return f"{partner}:{','.join(map(str, sorted(give)))}>{','.join(map(str, sorted(get)))}"


@dataclass
class State:
    proposals: list[dict] = field(default_factory=list)   # {key, partner, date}
    adds: dict[str, str] = field(default_factory=dict)    # player id -> ISO date
    drops: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            log.warning("State file %s is unreadable; starting fresh", path)
            data = {}
        return cls(proposals=data.get("proposals", []), adds=data.get("adds", {}),
                   drops=data.get("drops", {}), path=path)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("path")
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    @staticmethod
    def _within(iso: str | None, days: int, now: dt.datetime) -> bool:
        return bool(iso) and now - dt.datetime.fromisoformat(iso) < dt.timedelta(days=days)

    def proposed_recently(self, partner: int, days: int, now: dt.datetime) -> bool:
        return any(p["partner"] == partner and self._within(p["date"], days, now) for p in self.proposals)

    def proposed_before(self, key: str, days: int, now: dt.datetime) -> bool:
        return any(p["key"] == key and self._within(p["date"], days, now) for p in self.proposals)

    def added_recently(self, pid: int, days: int, now: dt.datetime) -> bool:
        return self._within(self.adds.get(str(pid)), days, now)

    def dropped_recently(self, pid: int, days: int, now: dt.datetime) -> bool:
        return self._within(self.drops.get(str(pid)), days, now)

    def record_proposal(self, partner: int, give, get, now: dt.datetime) -> None:
        self.proposals.append({"key": trade_key(partner, give, get), "partner": partner,
                               "date": now.isoformat()})
        cutoff = now - dt.timedelta(days=120)
        self.proposals = [p for p in self.proposals if dt.datetime.fromisoformat(p["date"]) > cutoff]

    def record_add_drop(self, add: int, drop: int | None, now: dt.datetime) -> None:
        self.adds[str(add)] = now.isoformat()
        if drop is not None:
            self.drops[str(drop)] = now.isoformat()
