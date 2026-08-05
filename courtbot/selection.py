from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Selection:
    place_id: str = ""
    place_name: str = ""
    day: str = ""          # YYYY-MM-DD
    time_key: str = ""     # 如 1700
    time_label: str = ""   # 如 17:00-18:00
    venue_id: str = ""
    sport_id: str = ""

    def is_set(self) -> bool:
        return bool(self.place_id and self.day and self.time_key)


class SelectionStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Selection:
        if not self.path.exists():
            return Selection()
        try:
            return Selection(**json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return Selection()

    def save(self, sel: Selection) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(sel.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
