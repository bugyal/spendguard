from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from fakes import FakeClock, FakeUpstream
from spendguard.config import parse_config
from spendguard.guard import Guard
from spendguard.proxy import ProxyServer

# Prices that make test arithmetic checkable by hand: 1M input tokens = $1.
TEST_PRICING = {"test-model": {"input": 1.0, "output": 2.0}}


@pytest.fixture
def make_guard(tmp_path: Any) -> Iterator[Callable[..., Guard]]:
    guards: List[Guard] = []

    def factory(extra: Optional[Dict[str, Any]] = None, clock: Optional[FakeClock] = None) -> Guard:
        data: Dict[str, Any] = {"state_dir": str(tmp_path / f"state{len(guards)}"), "pricing": dict(TEST_PRICING)}
        data.update(extra or {})
        guard = Guard(parse_config(data), clock=clock or FakeClock())
        guards.append(guard)
        return guard

    yield factory
    for guard in guards:
        guard.close()


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    fake = FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture
def make_proxy(tmp_path: Any, upstream: FakeUpstream, monkeypatch: Any) -> Iterator[Callable[..., SimpleNamespace]]:
    """A running proxy in front of FakeUpstream; returns (url, guard, upstream)."""
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")
    started: List[SimpleNamespace] = []

    def factory(extra: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
        data: Dict[str, Any] = {
            "state_dir": str(tmp_path / f"proxy{len(started)}"),
            "upstreams": {
                "openai": {"base_url": upstream.url},
                "anthropic": {"base_url": upstream.url},
            },
        }
        data.update(extra or {})
        guard = Guard(parse_config(data))
        server = ProxyServer(guard, "127.0.0.1", 0, quiet=True)
        server.start_background()
        env = SimpleNamespace(url=server.url, guard=guard, server=server, upstream=upstream)
        started.append(env)
        return env

    yield factory
    for env in started:
        env.server.stop()
        env.guard.close()
