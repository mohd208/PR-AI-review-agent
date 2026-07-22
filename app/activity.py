"""In-memory activity tracker for the live dashboard.

Two views:
- `current`: what's actively happening right now, keyed by (repo, ref) — e.g. "PR #4" is
  "Thinking...". Cleared when that unit of work finishes.
- `events`: a bounded history feed of meaningful milestones (picked up / fixed / no issue found /
  error), newest first — NOT a dump of every log line or every Claude tool call.

Single-process, in-memory only (not persisted) — a restart just clears the dashboard, which is
fine since it's a live "what's happening now" view, not an audit log.
"""

import time
from collections import deque
from dataclasses import dataclass

MAX_EVENTS = 200


@dataclass
class Event:
    ts: float
    repo: str
    ref: str
    kind: str  # "working" | "success" | "info" | "warning" | "error"
    message: str


_events: deque[Event] = deque(maxlen=MAX_EVENTS)
_current: dict[str, dict] = {}


def _key(repo: str, ref: str) -> str:
    return f"{repo}:{ref}"


def log_event(repo: str, ref: str, kind: str, message: str) -> None:
    _events.appendleft(Event(time.time(), repo, ref, kind, message))


def set_current(repo: str, ref: str, message: str) -> None:
    _current[_key(repo, ref)] = {"repo": repo, "ref": ref, "message": message, "ts": time.time()}


def clear_current(repo: str, ref: str) -> None:
    _current.pop(_key(repo, ref), None)


def snapshot() -> dict:
    return {
        "current": sorted(_current.values(), key=lambda c: c["ts"]),
        "events": [
            {"ts": e.ts, "repo": e.repo, "ref": e.ref, "kind": e.kind, "message": e.message}
            for e in _events
        ],
    }
