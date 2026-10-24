import datetime
import json
import os
import sys
from pathlib import Path


def state_dir() -> Path:
    env = os.environ.get("NEXUS_STATE_DIR")
    if env:
        p = Path(env)
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        if xdg:
            p = Path(xdg) / "nexus"
        else:
            p = Path.home() / ".local" / "state" / "nexus"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_event(event: dict) -> None:
    try:
        p = state_dir() / "events.jsonl"
        event["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        print("nexus: log error", file=sys.stderr)
