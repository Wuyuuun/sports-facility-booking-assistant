from __future__ import annotations

import json
import subprocess


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    def notify(self, title: str, message: str) -> None:
        if self.cfg.app_notification:
            script = (
                "display notification "
                + json.dumps(message)
                + " with title "
                + json.dumps(title)
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, check=False)
        if self.cfg.sound:
            subprocess.Popen(["say", message])
