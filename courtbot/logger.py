from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(state_dir: Path) -> logging.Logger:
    state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("courtbot")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    fh = logging.FileHandler(state_dir / "bot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
