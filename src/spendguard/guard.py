"""The containment engine: admission checks, the spend ledger, pauses, credentials.

Both front ends share it — the HTTP proxy and the in-process API
(:meth:`Guard.call` / :meth:`Guard.tool_result`) — so a cap, a pause or a
breaker trip means the same thing however an agent reaches the model.

Admission order for every call:

1. credential scope and credential spend cap
2. pauses (agent, customer, task, or everything)
3. circuit breaker — tool-failure loops and identical-request loops
4. budget caps — hard refuses (or pauses), soft alerts once per window

After the call: record cost, trip the breaker on a run of identical upstream
errors, and fire any cap the call just crossed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import BudgetRule, Config, load_config
from .events import EventSink
from .store import Store
from .toolscan import ToolResult, compile_patterns, error_class, scan_request
from .usage import Usage, detect_format, parse_response, to_dict

__all__ = [
    "Attribution",
    "BudgetState",
    "CallHandle",
    "Credential",
    "Decision",
    "Guard",
    "Pause",
    "SpendBlocked",
    "UNATTRIBUTED",
    "hash_body",
    "period_start",
]

UNATTRIBUTED = "unattributed"
_SCOPE_COLUMNS = {"agent": "agent", "customer": "customer", "task": "task", "run": "run", "credential": "credential_id"}


@dataclass(frozen=True)
class Attribution:
    agent: str = UNATTRIBUTED
    task: str = ""
    customer: str = ""
    run: str = ""
    credential_id: str = ""

    def value(self, scope: str) -> str:
        return {"agent": self.agent, "customer": self.customer, "task": self.task}.get(scope, "")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""  # refusal code: paused | breaker | budget | credential | credential_scope | credential_budget
    message: str = ""
    status: int = 200  # HTTP status the proxy answers a refusal with
    retry: bool = False  # the call resends a recent identical request


ALLOW = Decision(True)


class SpendBlocked(RuntimeError):
    """Raised by the in-process API when spendguard refuses a call."""

    def __init__(self, decision: Decision):
        super().__init__(decision.message)
        self.decision = decision


@dataclass(frozen=True)
class Credential:
    id: str
    agent: str
    task: str
    customer: str
    run: str
    created_at: float
    expires_at: Optional[float]
    revoked_at: Optional[float]
    max_usd: Optional[float]
    upstreams: Tuple[str, ...]
    note: str

    @classmethod
    def from_row(cls, row: Any) -> "Credential":
        return cls(
            id=row["id"],
            agent=row["agent"],
            task=row["task"],
            customer=row["customer"],
            run=row["run"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            max_usd=row["max_usd"],
            upstreams=tuple(u for u in row["upstreams"].split(",") if u),
            note=row["note"],
        )

    def active(self, now: float) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


@dataclass(frozen=True)
class Pause:
    scope: str
    value: str
    kind: str
    reason: str
    paused_at: float
    until: Optional[float]


@dataclass(frozen=True)
class BudgetState:
    rule: BudgetRule
    value: str
    window_start: float
    spend: float

    @property
    def level(self) -> str:
        if self.rule.hard_usd is not None and self.spend >= self.rule.hard_usd:
            return "hard"
        if self.rule.soft_usd is not None and self.spend >= self.rule.soft_usd:
            return "soft"
        return "ok"

    @property
    def label(self) -> str:
        return "*" if self.rule.scope == "global" else f"{self.rule.scope}={self.value}"


def hash_body(path: str, body: bytes) -> str:
    return hashlib.sha256(path.encode("utf-8") + b"\n" + body).hexdigest()[:32]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def period_start(period: str, now: float, tz: str = "utc") -> float:
    """Start of the calendar period containing ``now`` (UTC or local time)."""
    if period == "total":
        return 0.0
    dt = datetime.fromtimestamp(now) if tz == "local" else datetime.fromtimestamp(now, timezone.utc)
    if period == "hour":
        dt = dt.replace(minute=0, second=0, microsecond=0)
    elif period == "day":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        dt = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period {period!r}")
    return dt.timestamp()


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


class CallHandle:
    """Yielded by :meth:`Guard.call`; tell it what the call cost."""

    def __init__(self, model: str = ""):
        self.model = model
        self.usage = Usage()
        self.cost_usd = 0.0

    def record_response(self, response: Any) -> None:
        """Read usage from an OpenAI/Anthropic SDK response object or a decoded JSON dict."""
        data = to_dict(response)
        usage, model = parse_response(detect_format(data), data)
        self.usage = usage
        self.model = model or self.model

    def set_usage(self, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        self.usage = Usage(int(input_tokens), int(output_tokens), int(cache_read_tokens), int(cache_write_tokens), found=True)


class Guard:
    def __init__(self, config: Config, *, clock: Callable[[], float] = time.time, store: Optional[Store] = None):
        self.config = config
        self.clock = clock
        self.store = store or Store(config.db_path)
        self.events = EventSink(config, self.store, clock)
        self.patterns = compile_patterns(config.breaker.error_patterns)

    @classmethod
    def load(cls, path: Optional[str] = None, **kwargs: Any) -> "Guard":
        return cls(load_config(path), **kwargs)

    def close(self) -> None:
        self.events.wait()
        self.store.close()

    def __enter__(self) -> "Guard":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- admission -----------------------------------------------------------

    def admit(
        self,
        attr: Attribution,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        tool_results: Sequence[ToolResult] = (),
        body_hash: str = "",
        retry_hint: bool = False,
        credential: Optional[Credential] = None,
    ) -> Decision:
        """Decide whether a call may go upstream. Refusals are written to the ledger."""
        now = self.clock()
        with self.store.transaction():
            decision = self._admit(attr, now, upstream, tool_results, body_hash, credential)
            retry = retry_hint or self._is_resend(attr.agent, body_hash, now)
            if not decision.allowed:
                self._insert_call(
                    attr,
                    now,
                    upstream=upstream,
                    endpoint=endpoint,
                    model=model,
                    outcome="refused",
                    error_class=decision.reason,
                    retry=retry,
                    tool_error=_tool_error_label(tool_results),
                    body_hash=body_hash,
                )
        return Decision(decision.allowed, decision.reason, decision.message, decision.status, retry)

    def record_refusal(self, attr: Attribution, decision: Decision, *, upstream: str = "", endpoint: str = "", model: str = "") -> None:
        """Ledger a call refused before admission (bad credential, credential required)."""
        self._insert_call(attr, self.clock(), upstream=upstream, endpoint=endpoint, model=model, outcome="refused", error_class=decision.reason)

    def _admit(
        self,
        attr: Attribution,
        now: float,
        upstream: str,
        tool_results: Sequence[ToolResult],
        body_hash: str,
        credential: Optional[Credential],
    ) -> Decision:
        if credential is not None:
            if credential.upstreams and upstream not in credential.upstreams:
                return Decision(
                    False,
                    "credential_scope",
                    f"spendguard: credential {credential.id} is not valid for upstream {upstream!r} "
                    f"(allowed: {', '.join(credential.upstreams)})",
                    403,
                )
            if credential.max_usd is not None:
                spent = self._spend("credential", credential.id, 0.0)
                if spent >= credential.max_usd:
                    return Decision(
                        False,
                        "credential_budget",
                        f"spendguard: credential {credential.id} has spent {_usd(spent)} of its {_usd(credential.max_usd)} limit",
                        402,
                    )
        pause = self.active_pause(attr, now)
        if pause is not None:
            return Decision(False, "paused", _pause_message(pause), 423)
        if self.config.breaker.enabled:
            trip = self._breaker_admission(attr, now, tool_results, body_hash)
            if trip is not None:
                return trip
        for state in self.budget_states(attr, now):
            if state.level == "hard":
                return self._enforce_hard_cap(state, attr, now)
            if state.level == "soft":
                self._soft_alert(state, now)
        return ALLOW

    def _is_resend(self, agent: str, body_hash: str, now: float) -> bool:
        if not body_hash:
            return False
        since = now - self.config.breaker.retry_window_seconds
        row = self.store.one(
            "SELECT 1 FROM calls WHERE agent = ? AND body_hash = ? AND ts > ? AND outcome != 'refused' LIMIT 1",
            (agent, body_hash, since),
        )
        return row is not None

    # -- circuit breaker -----------------------------------------------------

    def _breaker_floor(self, agent: str, now: float) -> float:
        reset = self.store.scalar("SELECT ts FROM resets WHERE agent = ?", (agent,)) or 0.0
        return max(now - self.config.breaker.window_seconds, reset)

    def _breaker_admission(
        self, attr: Attribution, now: float, tool_results: Sequence[ToolResult], body_hash: str
    ) -> Optional[Decision]:
        breaker = self.config.breaker
        floor = self._breaker_floor(attr.agent, now)
        for result in tool_results:
            self.store.execute(
                "INSERT OR IGNORE INTO tool_events(ts, agent, tool, error_class, call_ref) VALUES (?, ?, ?, ?, ?)",
                (now, attr.agent, result.tool, result.error_class, result.call_id),
            )
        seen = set()
        for result in tool_results:
            key = (result.tool, result.error_class)
            if not result.is_error or key in seen:
                continue
            seen.add(key)
            last_ok = self.store.scalar(
                "SELECT MAX(ts) FROM tool_events WHERE agent = ? AND tool = ? AND error_class = ''",
                (attr.agent, result.tool),
            )
            # Failures since the tool last succeeded; a success resets the streak.
            count = self.store.scalar(
                "SELECT COUNT(*) FROM tool_events WHERE agent = ? AND tool = ? AND error_class = ? AND ts > ?",
                (attr.agent, result.tool, result.error_class, max(floor, last_ok or 0.0)),
            )
            if count > breaker.max_repeats:
                return self._trip(
                    attr,
                    now,
                    signal="tool_loop",
                    detail=f"tool {result.tool!r} failed {count}x in a row with {result.error_class}",
                    tool=result.tool,
                    error_class=result.error_class,
                    count=count,
                    threshold=breaker.max_repeats,
                )
        if body_hash and breaker.max_identical_requests > 0:
            sent = self.store.scalar(
                "SELECT COUNT(*) FROM calls WHERE agent = ? AND body_hash = ? AND ts > ? AND outcome != 'refused'",
                (attr.agent, body_hash, floor),
            )
            if sent >= breaker.max_identical_requests:
                return self._trip(
                    attr,
                    now,
                    signal="identical_requests",
                    detail=f"the same request was sent {sent + 1}x",
                    tool="request",
                    error_class="identical_request",
                    count=sent + 1,
                    threshold=breaker.max_identical_requests,
                )
        return None

    def _check_api_errors(self, attr: Attribution, upstream: str, error_cls: str, now: float) -> None:
        threshold = self.config.breaker.api_error_repeats
        if not threshold or not self.config.breaker.enabled:
            return
        floor = self._breaker_floor(attr.agent, now)
        last_ok = self.store.scalar(
            "SELECT MAX(ts) FROM calls WHERE agent = ? AND upstream = ? AND outcome = 'ok'", (attr.agent, upstream)
        )
        count = self.store.scalar(
            "SELECT COUNT(*) FROM calls WHERE agent = ? AND upstream = ? AND outcome = 'error' AND error_class = ? AND ts > ?",
            (attr.agent, upstream, error_cls, max(floor, last_ok or 0.0)),
        )
        if count > threshold and self.active_pause(attr, now) is None:
            self._trip(
                attr,
                now,
                signal="api_errors",
                detail=f"upstream {upstream!r} failed {count}x in a row with {error_cls}",
                tool=f"api:{upstream}",
                error_class=error_cls,
                count=count,
                threshold=threshold,
            )

    def _trip(self, attr: Attribution, now: float, *, signal: str, detail: str, **data: Any) -> Decision:
        cooldown = self.config.breaker.cooldown_seconds
        until = now + cooldown if cooldown > 0 else None
        reason = f"circuit breaker ({signal}): {detail}"
        self._set_pause("agent", attr.agent, kind="breaker", reason=reason, now=now, until=until)
        self.events.emit(
            "breaker_tripped",
            f"spendguard: agent {attr.agent!r} paused — {detail}",
            agent=attr.agent,
            task=attr.task,
            customer=attr.customer,
            signal=signal,
            **data,
        )
        pause = Pause("agent", attr.agent, "breaker", reason, now, until)
        return Decision(False, "breaker", _pause_message(pause), 423)

    def tool_result(
        self,
        *,
        agent: str,
        tool: str,
        error: Any = None,
        call_id: str = "",
        task: str = "",
        customer: str = "",
    ) -> None:
        """Report a tool execution from your own harness (no proxy needed).

        ``error`` is None for success, else an exception or message. Raises
        :class:`SpendBlocked` when this failure trips the circuit breaker.
        """
        if error is None:
            cls = ""
        elif isinstance(error, BaseException):
            cls = error_class(f"{type(error).__name__}: {error}")
        else:
            cls = error_class(str(error)) or "error"
        attr = Attribution(agent=agent, task=task, customer=customer)
        now = self.clock()
        decision: Optional[Decision] = None
        with self.store.transaction():
            pause = self.active_pause(attr, now)
            if pause is not None:
                decision = Decision(False, "paused", _pause_message(pause), 423)
            elif self.config.breaker.enabled:
                decision = self._breaker_admission(attr, now, [ToolResult(tool, call_id, cls)], "")
        if decision is not None:
            raise SpendBlocked(decision)

    # -- pauses --------------------------------------------------------------

    def active_pause(self, attr: Attribution, now: Optional[float] = None) -> Optional[Pause]:
        now = self.clock() if now is None else now
        for scope, value in (("global", "*"), ("agent", attr.agent), ("customer", attr.customer), ("task", attr.task)):
            if not value:
                continue
            row = self.store.one("SELECT * FROM pauses WHERE scope = ? AND value = ?", (scope, value))
            if row is None:
                continue
            if row["until"] is not None and row["until"] <= now:
                self.resume(scope, value, by="cooldown")
                continue
            return Pause(row["scope"], row["value"], row["kind"], row["reason"], row["paused_at"], row["until"])
        return None

    def pauses(self) -> List[Pause]:
        now = self.clock()
        out = []
        for row in self.store.query("SELECT * FROM pauses ORDER BY paused_at"):
            if row["until"] is not None and row["until"] <= now:
                self.resume(row["scope"], row["value"], by="cooldown")
                continue
            out.append(Pause(row["scope"], row["value"], row["kind"], row["reason"], row["paused_at"], row["until"]))
        return out

    def pause(self, scope: str, value: str, *, reason: str = "paused by operator", until: Optional[float] = None) -> None:
        if scope == "global":
            value = "*"
        self._set_pause(scope, value, kind="manual", reason=reason, now=self.clock(), until=until)

    def _set_pause(self, scope: str, value: str, *, kind: str, reason: str, now: float, until: Optional[float]) -> None:
        if scope not in ("agent", "customer", "task", "global"):
            raise ValueError(f"cannot pause scope {scope!r}")
        with self.store.transaction():
            self.store.execute(
                "INSERT OR REPLACE INTO pauses(scope, value, kind, reason, paused_at, until) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, value, kind, reason, now, until),
            )
            what = "everything" if scope == "global" else f"{scope} {value!r}"
            self.events.emit(
                "paused",
                f"spendguard: paused {what} — {reason}",
                scope=scope,
                value=value,
                pause_kind=kind,
                reason=reason,
                until=until,
                agent=value if scope == "agent" else None,
            )

    def resume(self, scope: str, value: str, *, by: str = "operator") -> bool:
        if scope == "global":
            value = "*"
        now = self.clock()
        with self.store.transaction():
            gone = self.store.execute("DELETE FROM pauses WHERE scope = ? AND value = ?", (scope, value)).rowcount
            if not gone:
                return False
            if scope == "agent":  # breaker counts restart from here, or the next failure would re-trip at once
                self.store.execute("INSERT OR REPLACE INTO resets(agent, ts) VALUES (?, ?)", (value, now))
            what = "everything" if scope == "global" else f"{scope} {value!r}"
            self.events.emit(
                "resumed",
                f"spendguard: resumed {what} ({by})",
                scope=scope,
                value=value,
                by=by,
                agent=value if scope == "agent" else None,
            )
        return True

    # -- budgets -------------------------------------------------------------

    def _spend(self, scope: str, value: str, since: float) -> float:
        if scope == "global":
            total = self.store.scalar("SELECT SUM(cost_usd) FROM calls WHERE ts >= ?", (since,))
        else:
            column = _SCOPE_COLUMNS[scope]
            total = self.store.scalar(f"SELECT SUM(cost_usd) FROM calls WHERE {column} = ? AND ts >= ?", (value, since))
        return float(total or 0.0)

    def budget_states(self, attr: Attribution, now: Optional[float] = None) -> List[BudgetState]:
        """Every budget rule that applies to this attribution, with current-period spend."""
        now = self.clock() if now is None else now
        states = []
        for rule in self.config.budgets:
            value = "*" if rule.scope == "global" else attr.value(rule.scope)
            if not rule.applies_to(value):
                continue
            start = period_start(rule.period, now, self.config.timezone)
            states.append(BudgetState(rule, value, start, self._spend(rule.scope, value, start)))
        return states

    def budget_overview(self, now: Optional[float] = None) -> List[BudgetState]:
        """Every rule × every value it currently covers (for reports)."""
        now = self.clock() if now is None else now
        states = []
        for rule in self.config.budgets:
            start = period_start(rule.period, now, self.config.timezone)
            if rule.scope == "global":
                values = ["*"]
            elif rule.match != "*":
                values = [rule.match]
            else:
                column = _SCOPE_COLUMNS[rule.scope]
                values = [
                    r[0]
                    for r in self.store.query(
                        f"SELECT DISTINCT {column} FROM calls WHERE ts >= ? AND {column} != '' ORDER BY 1", (start,)
                    )
                ]
            states.extend(BudgetState(rule, v, start, self._spend(rule.scope, v, start)) for v in values)
        return states

    def _alert_once(self, level: str, state: BudgetState, now: float) -> bool:
        key = f"{level}|{state.rule.name}|{state.value}|{int(state.window_start)}"
        cursor = self.store.execute("INSERT OR IGNORE INTO alerts(key, ts) VALUES (?, ?)", (key, now))
        return cursor.rowcount == 1

    def _soft_alert(self, state: BudgetState, now: float) -> None:
        rule = state.rule
        if rule.soft_usd is None or not self._alert_once("soft", state, now):
            return
        self.events.emit(
            "soft_cap_exceeded",
            f"spendguard: {state.label} spent {_usd(state.spend)} this {rule.period}, over the soft cap of {_usd(rule.soft_usd)} ({rule.name})",
            rule=rule.name,
            scope=rule.scope,
            value=state.value,
            period=rule.period,
            spend_usd=round(state.spend, 6),
            limit_usd=rule.soft_usd,
            agent=state.value if rule.scope == "agent" else None,
        )

    def _enforce_hard_cap(self, state: BudgetState, attr: Attribution, now: float) -> Decision:
        rule = state.rule
        assert rule.hard_usd is not None
        if self._alert_once("hard", state, now):
            self.events.emit(
                "hard_cap_exceeded",
                f"spendguard: {state.label} reached the hard cap of {_usd(rule.hard_usd)} this {rule.period} "
                f"({_usd(state.spend)} spent) — calls are {'paused' if rule.action == 'pause' else 'refused'} ({rule.name})",
                rule=rule.name,
                scope=rule.scope,
                value=state.value,
                period=rule.period,
                spend_usd=round(state.spend, 6),
                limit_usd=rule.hard_usd,
                action=rule.action,
                agent=state.value if rule.scope == "agent" else attr.agent,
            )
        if rule.action == "pause":
            scope, value = (rule.scope, state.value) if rule.scope != "global" else ("global", "*")
            reason = f"hard cap {rule.name} reached ({_usd(state.spend)} >= {_usd(rule.hard_usd)})"
            if self.store.one("SELECT 1 FROM pauses WHERE scope = ? AND value = ?", (scope, value)) is None:
                self._set_pause(scope, value, kind="budget", reason=reason, now=now, until=None)
            return Decision(False, "paused", _pause_message(Pause(scope, value, "budget", reason, now, None)), 423)
        return Decision(
            False,
            "budget",
            f"spendguard: {state.label} has spent {_usd(state.spend)} this {rule.period}; hard cap is "
            f"{_usd(rule.hard_usd)} ({rule.name}). Calls resume next {rule.period} or when the cap is raised.",
            402,
        )

    # -- recording -----------------------------------------------------------

    def record_call(
        self,
        attr: Attribution,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        outcome: str = "ok",
        http_status: Optional[int] = None,
        error_class: str = "",
        usage: Optional[Usage] = None,
        estimated: bool = False,
        latency_ms: int = 0,
        body_hash: str = "",
        retry: bool = False,
        tool_error: str = "",
    ) -> float:
        """Write a completed (or failed) call to the ledger; returns its cost in USD."""
        now = self.clock()
        usage = usage or Usage()
        cost, priced = self.config.pricing.cost(model, usage)
        with self.store.transaction():
            self._insert_call(
                attr,
                now,
                upstream=upstream,
                endpoint=endpoint,
                model=model,
                outcome=outcome,
                http_status=http_status,
                error_class=error_class,
                usage=usage,
                cost=cost,
                priced=priced,
                estimated=estimated,
                latency_ms=latency_ms,
                body_hash=body_hash,
                retry=retry,
                tool_error=tool_error,
            )
            if outcome == "error":
                self._check_api_errors(attr, upstream, error_class, now)
            if cost > 0:
                for state in self.budget_states(attr, now):
                    if state.level == "hard":
                        self._enforce_hard_cap(state, attr, now)
                    elif state.level == "soft":
                        self._soft_alert(state, now)
        return cost

    def _insert_call(
        self,
        attr: Attribution,
        now: float,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        outcome: str,
        http_status: Optional[int] = None,
        error_class: str = "",
        usage: Optional[Usage] = None,
        cost: float = 0.0,
        priced: bool = True,
        estimated: bool = False,
        latency_ms: int = 0,
        body_hash: str = "",
        retry: bool = False,
        tool_error: str = "",
    ) -> None:
        u = usage or Usage()
        self.store.execute(
            """INSERT INTO calls(ts, agent, task, customer, run, credential_id, upstream, endpoint, model,
                   outcome, http_status, error_class, retry, tool_error, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, priced, estimated, latency_ms, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                attr.agent,
                attr.task,
                attr.customer,
                attr.run,
                attr.credential_id,
                upstream,
                endpoint,
                model,
                outcome,
                http_status,
                error_class,
                int(retry),
                tool_error,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                cost,
                int(priced),
                int(estimated),
                int(latency_ms),
                body_hash,
            ),
        )

    # -- in-process API ------------------------------------------------------

    @contextmanager
    def call(
        self,
        *,
        agent: str,
        task: str = "",
        customer: str = "",
        run: str = "",
        model: str = "",
        request: Any = None,
        upstream: str = "sdk",
    ) -> Iterator[CallHandle]:
        """Guard one model call made by your own code.

        >>> with guard.call(agent="support-bot", customer="acme", model="claude-opus-5-5") as call:
        ...     response = client.messages.create(...)
        ...     call.record_response(response)

        Raises :class:`SpendBlocked` *before* the call when it is refused. Pass
        the request payload as ``request=`` to get tool-loop detection on it.
        """
        attr = Attribution(agent=agent, task=task, customer=customer, run=run)
        tool_results: List[ToolResult] = []
        body_hash = ""
        if request is not None:
            payload = to_dict(request)
            tool_results = scan_request(payload, self.patterns)
            body_hash = hash_body(upstream, json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
        decision = self.admit(attr, upstream=upstream, model=model, tool_results=tool_results, body_hash=body_hash)
        if not decision.allowed:
            raise SpendBlocked(decision)
        handle = CallHandle(model)
        started = time.monotonic()
        common = dict(upstream=upstream, body_hash=body_hash, retry=decision.retry, tool_error=_tool_error_label(tool_results))
        try:
            yield handle
        except BaseException as exc:
            status = getattr(exc, "status_code", None)
            handle.cost_usd = self.record_call(
                attr,
                model=handle.model,
                outcome="error",
                http_status=status if isinstance(status, int) else None,
                error_class=f"http_{status}" if isinstance(status, int) else type(exc).__name__,
                usage=handle.usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                **common,  # type: ignore[arg-type]
            )
            raise
        handle.cost_usd = self.record_call(
            attr,
            model=handle.model,
            outcome="ok",
            usage=handle.usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            **common,  # type: ignore[arg-type]
        )

    # -- credentials ---------------------------------------------------------

    def mint_credential(
        self,
        *,
        agent: str,
        task: str = "",
        customer: str = "",
        run: str = "",
        ttl_seconds: Optional[float] = None,
        max_usd: Optional[float] = None,
        upstreams: Sequence[str] = (),
        note: str = "",
    ) -> Tuple[str, Credential]:
        """Create a scoped credential. The token is returned once and never stored."""
        if not agent:
            raise ValueError("a credential needs an agent")
        unknown = [u for u in upstreams if u not in self.config.upstreams]
        if unknown:
            raise ValueError(f"unknown upstream(s): {', '.join(unknown)}")
        now = self.clock()
        token = "sg_" + secrets.token_urlsafe(32)
        cred = Credential(
            id="cred_" + secrets.token_hex(6),
            agent=agent,
            task=task,
            customer=customer,
            run=run,
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
            revoked_at=None,
            max_usd=max_usd,
            upstreams=tuple(upstreams),
            note=note,
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO credentials(id, token_hash, agent, task, customer, run, created_at, expires_at,
                       revoked_at, max_usd, upstreams, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (cred.id, _token_hash(token), agent, task, customer, run, now, cred.expires_at, max_usd, ",".join(upstreams), note),
            )
            self.events.emit(
                "credential_minted",
                f"spendguard: minted {cred.id} for agent {agent!r}",
                id=cred.id,
                agent=agent,
                task=task,
                customer=customer,
                expires_at=cred.expires_at,
                max_usd=max_usd,
            )
        return token, cred

    def verify_credential(self, token: str) -> Tuple[Optional[Credential], Optional[Decision]]:
        row = self.store.one("SELECT * FROM credentials WHERE token_hash = ?", (_token_hash(token),))
        if row is None:
            return None, Decision(False, "credential", "spendguard: unknown credential", 401)
        cred = Credential.from_row(row)
        if cred.revoked_at is not None:
            return cred, Decision(False, "credential", f"spendguard: credential {cred.id} was revoked", 401)
        if cred.expires_at is not None and cred.expires_at <= self.clock():
            when = datetime.fromtimestamp(cred.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return cred, Decision(False, "credential", f"spendguard: credential {cred.id} expired at {when}", 401)
        return cred, None

    def credentials(self, *, include_inactive: bool = False) -> List[Credential]:
        now = self.clock()
        creds = [Credential.from_row(r) for r in self.store.query("SELECT * FROM credentials ORDER BY created_at")]
        return creds if include_inactive else [c for c in creds if c.active(now)]

    def credential_stats(self, cred_id: str) -> Tuple[int, float]:
        """(calls, USD) recorded against a credential."""
        row = self.store.one("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM calls WHERE credential_id = ?", (cred_id,))
        return (int(row[0]), float(row[1])) if row is not None else (0, 0.0)

    def revoke_credentials(
        self, *, ids: Sequence[str] = (), agent: Optional[str] = None, task: Optional[str] = None
    ) -> List[str]:
        """Revoke by id, or every active credential of an agent or task. Returns revoked ids."""
        now = self.clock()
        clauses: List[str] = []
        params: List[Any] = []
        if ids:
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if task:
            clauses.append("task = ?")
            params.append(task)
        if not clauses:
            return []
        where = " OR ".join(clauses)
        with self.store.transaction():
            rows = self.store.query(f"SELECT id, agent FROM credentials WHERE revoked_at IS NULL AND ({where})", params)
            for row in rows:
                self.store.execute("UPDATE credentials SET revoked_at = ? WHERE id = ?", (now, row["id"]))
                self.events.emit(
                    "credential_revoked", f"spendguard: revoked {row['id']}", id=row["id"], agent=row["agent"]
                )
        return [r["id"] for r in rows]

    def gc_credentials(self, older_than_seconds: float = 0.0) -> int:
        """Delete credentials that expired or were revoked more than ``older_than_seconds`` ago."""
        cutoff = self.clock() - older_than_seconds
        return self.store.execute(
            "DELETE FROM credentials WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) "
            "OR (expires_at IS NOT NULL AND expires_at <= ?)",
            (cutoff, cutoff),
        ).rowcount


def _tool_error_label(results: Sequence[ToolResult]) -> str:
    for result in results:
        if result.is_error:
            return f"{result.tool}:{result.error_class}"[:200]
    return ""


def _pause_message(pause: Pause) -> str:
    if pause.scope == "global":
        what, flag = "all agents are", "--all"
    else:
        what, flag = f"{pause.scope} {pause.value!r} is", f"--{pause.scope} {pause.value}"
    tail = ""
    if pause.until is not None:
        tail = f" Auto-resumes at {datetime.fromtimestamp(pause.until, timezone.utc).strftime('%H:%M:%S UTC')}."
    return f"spendguard: {what} paused — {pause.reason}.{tail} Resume with: spendguard resume {flag}"
