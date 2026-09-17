"""Process logs: stderr plus a rotating file under <log.dir>/<component>/<component>.log.
Health lines (!! DEPENDENCY …), uvicorn output and our own warnings all land there."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_configured = False


def setup(cfg, component: str) -> logging.Logger:
    global _configured
    log = logging.getLogger("urlverify")
    if _configured:
        return log
    d = cfg.log.resolved() / component
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(d / f"{component}.log", maxBytes=cfg.log.process_max_bytes, backupCount=cfg.log.process_backups, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(message)s"))
    for name in ("urlverify", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(fh)
        if name == "urlverify":
            lg.addHandler(sh)
        lg.propagate = False
    _configured = True
    return log
