"""Events: every containment action is written down and can wake someone up.

Each event goes to three places: the ``events`` table (for reports), an
append-only ``events.jsonl`` in the state dir (for tailing or log shippers),
and any hooks configured for its kind. Hooks run off the request path, on
background threads: a command (event JSON on stdin) or an HTTP POST to a URL
*you* configured. Nothing is sent anywhere else — spendguard has no
telemetry.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TextIO

from . import __version__
from .config import Config, Hook
from .store import Store

__all__ = ["EventSink"]


class EventSink:
    def __init__(self, config: Config, store: Store, clock: Callable[[], float] = time.time, *, errors: Optional[TextIO] = None):
        self.hooks: List[Hook] = list(config.hooks)
        self.path = config.events_path
        self.store = store
        self.clock = clock
        self.errors = errors
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []

    def emit(self, kind: str, text: str, **data: Any) -> Dict[str, Any]:
        now = self.clock()
        event: Dict[str, Any] = {
            "ts": round(now, 3),
            "time": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": kind,
            "text": text,
        }
        event.update({k: v for k, v in data.items() if v is not None})
        line = json.dumps(event, sort_keys=True)
        self.store.execute(
            "INSERT INTO events(ts, kind, agent, data) VALUES (?, ?, ?, ?)",
            (now, kind, str(data.get("agent") or ""), line),
        )
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            for hook in self.hooks:
                if hook.wants(kind):
                    thread = threading.Thread(target=self._run_hook, args=(hook, event), daemon=True)
                    thread.start()
                    self._threads.append(thread)
            self._threads = [t for t in self._threads if t.is_alive()]
        return event

    def wait(self, timeout: float = 15.0) -> None:
        """Block until in-flight hooks finish (CLI commands call this before exiting)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _run_hook(self, hook: Hook, event: Dict[str, Any]) -> None:
        payload = json.dumps(event).encode("utf-8")
        try:
            if hook.command:
                env = dict(os.environ)
                env.update(
                    SPENDGUARD_EVENT=str(event["kind"]),
                    SPENDGUARD_TEXT=str(event["text"]),
                    SPENDGUARD_AGENT=str(event.get("agent") or ""),
                )
                subprocess.run(
                    list(hook.command),
                    input=payload,
                    env=env,
                    timeout=hook.timeout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if hook.url:
                request = urllib.request.Request(
                    hook.url,
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/json", "User-Agent": f"spendguard/{__version__}"},
                )
                urllib.request.urlopen(request, timeout=hook.timeout).close()
        except Exception as exc:  # a broken hook must never take the proxy down
            print(f"spendguard: hook for {event['kind']} failed: {exc}", file=self.errors or sys.stderr)
