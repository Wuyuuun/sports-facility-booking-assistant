from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Session:
    cookies: list = field(default_factory=list)
    venue_token: str = ""
    api_key: str = ""
    booking_uri: str = ""
    euid: str = ""
    fetched_at: float = 0.0

    def is_valid(self, ttl: float = 1800.0) -> bool:
        return (
            bool(self.venue_token)
            and bool(self.api_key)
            and (time.time() - self.fetched_at) < ttl
        )


class SessionStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Session:
        if not self.path.exists():
            return Session()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Session(**data)
        except Exception:
            return Session()

    def save(self, s: Session) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
