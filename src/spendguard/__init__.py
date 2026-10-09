"""spendguard — spend containment for running agents.

A ledger that attributes every model call to (agent, task, customer), a
circuit breaker that pauses an agent stuck in a failure loop, hard and soft
budget caps, and scoped, expiring credentials. Runs as a local proxy in
front of the provider API, or in-process via :class:`Guard`.
"""

__version__ = "0.1.0"

from .config import Config, ConfigError, load_config  # noqa: E402
from .guard import Attribution, Decision, Guard, SpendBlocked  # noqa: E402

__all__ = [
    "Attribution",
    "Config",
    "ConfigError",
    "Decision",
    "Guard",
    "SpendBlocked",
    "__version__",
    "load_config",
]
