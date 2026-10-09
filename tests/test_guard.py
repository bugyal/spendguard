import json
import sys

import pytest

from fakes import FakeClock
from spendguard.guard import Attribution, SpendBlocked
from spendguard.toolscan import ToolResult
from spendguard.usage import Usage

HALF_DOLLAR = Usage(input_tokens=500_000, found=True)  # test-model: $1 per 1M input tokens
ACME_BOT = Attribution(agent="support-bot", customer="acme", task="T-1")


def events(guard, kind=None):
    rows = guard.store.query("SELECT kind, data FROM events ORDER BY id")
    return [json.loads(r["data"]) for r in rows if kind is None or r["kind"] == kind]


def spend(guard, attr=ACME_BOT, usage=HALF_DOLLAR, **kw):
    return guard.record_call(attr, upstream="openai", model="test-model", usage=usage, **kw)


# -- ledger ------------------------------------------------------------------


def test_record_call_prices_and_attributes(make_guard):
    guard = make_guard()
    cost = spend(guard)
    assert cost == pytest.approx(0.5)
    row = guard.store.one("SELECT * FROM calls")
    assert (row["agent"], row["customer"], row["task"], row["outcome"], row["priced"]) == ("support-bot", "acme", "T-1", "ok", 1)
    assert row["cost_usd"] == pytest.approx(0.5)


def test_failed_and_refused_calls_are_ledgered_too(make_guard):
    guard = make_guard({"breaker": {"api_error_repeats": 0}})
    guard.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=500, error_class="http_500")
    guard.pause("agent", "support-bot")
    assert not guard.admit(ACME_BOT, upstream="openai").allowed
    outcomes = [r["outcome"] for r in guard.store.query("SELECT outcome FROM calls ORDER BY id")]
    assert outcomes == ["error", "refused"]


def test_identical_resend_is_marked_as_retry(make_guard):
    clock = FakeClock()
    guard = make_guard(clock=clock)
    assert not guard.admit(ACME_BOT, body_hash="abc").retry
    spend(guard, body_hash="abc")
    clock.advance(5)
    assert guard.admit(ACME_BOT, body_hash="abc").retry
    assert guard.admit(ACME_BOT, body_hash="abc", retry_hint=True).retry
    clock.advance(301)  # outside retry_window_seconds
    assert not guard.admit(ACME_BOT, body_hash="abc").retry


# -- budgets -----------------------------------------------------------------


def test_soft_cap_alerts_once_per_window(make_guard):
    clock = FakeClock()
    guard = make_guard({"budgets": [{"scope": "customer", "match": "*", "soft_usd": 1, "hard_usd": 5}]}, clock=clock)
    for _ in range(4):
        assert guard.admit(ACME_BOT).allowed
        spend(guard)
    assert len(events(guard, "soft_cap_exceeded")) == 1
    (alert,) = events(guard, "soft_cap_exceeded")
    assert alert["value"] == "acme" and alert["limit_usd"] == 1
    clock.advance(86400)  # a new day re-arms the alert
    spend(guard)
    spend(guard)
    assert len(events(guard, "soft_cap_exceeded")) == 2


def test_hard_cap_refuses_and_rolls_over_next_day(make_guard):
    clock = FakeClock()
    guard = make_guard({"budgets": [{"scope": "customer", "match": "*", "hard_usd": 1}]}, clock=clock)
    spend(guard)
    assert guard.admit(ACME_BOT).allowed
    spend(guard)
    decision = guard.admit(ACME_BOT)
    assert (decision.allowed, decision.reason, decision.status) == (False, "budget", 402)
    assert "hard cap" in decision.message
    assert guard.admit(Attribution(agent="support-bot", customer="globex")).allowed  # per customer
    assert len(events(guard, "hard_cap_exceeded")) == 1
    clock.advance(12 * 3600 + 1)  # past midnight UTC
    assert guard.admit(ACME_BOT).allowed


def test_hard_cap_with_pause_action_needs_a_human(make_guard):
    clock = FakeClock()
    guard = make_guard(
        {"budgets": [{"scope": "customer", "match": "acme", "period": "month", "hard_usd": 1, "action": "pause"}]}, clock=clock
    )
    spend(guard)
    spend(guard)  # crossing the cap pauses the customer straight away
    decision = guard.admit(Attribution(agent="other-bot", customer="acme"))
    assert (decision.allowed, decision.reason, decision.status) == (False, "paused", 423)
    assert "resume --customer acme" in decision.message
    clock.advance(40 * 86400)  # a new month does not lift a pause
    assert not guard.admit(ACME_BOT).allowed
    assert guard.resume("customer", "acme")
    assert guard.admit(ACME_BOT).allowed
    assert [e["kind"] for e in events(guard) if e["kind"] in ("hard_cap_exceeded", "paused", "resumed")] == [
        "hard_cap_exceeded",
        "paused",
        "resumed",
    ]


def test_global_cap_counts_everyone(make_guard):
    guard = make_guard({"budgets": [{"scope": "global", "hard_usd": 1}]})
    spend(guard, Attribution(agent="a"))
    spend(guard, Attribution(agent="b"))
    assert guard.admit(Attribution(agent="c")).reason == "budget"


def test_rules_for_one_value_ignore_others(make_guard):
    guard = make_guard({"budgets": [{"scope": "agent", "match": "big-spender", "hard_usd": 0.5}]})
    spend(guard)
    assert guard.admit(ACME_BOT).allowed
    spend(guard, Attribution(agent="big-spender"))
    assert guard.admit(Attribution(agent="big-spender")).reason == "budget"


# -- circuit breaker -----------------------------------------------------------


def tool_error(i, cls="TimeoutError", tool="crm_lookup"):
    return [ToolResult(tool, f"call_{i}", cls)]


def test_tool_loop_trips_after_max_repeats(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 3}}, clock=clock)
    allowed = []
    for i in range(6):
        clock.advance(1)
        allowed.append(guard.admit(ACME_BOT, tool_results=tool_error(i)).allowed)
    assert allowed == [True, True, True, False, False, False]
    first_refusal = guard.store.one("SELECT error_class, tool_error FROM calls WHERE outcome = 'refused' ORDER BY id")
    assert (first_refusal["error_class"], first_refusal["tool_error"]) == ("breaker", "crm_lookup:TimeoutError")
    (trip,) = events(guard, "breaker_tripped")
    assert trip["signal"] == "tool_loop" and trip["tool"] == "crm_lookup" and trip["count"] == 4
    assert guard.admit(ACME_BOT).reason == "paused"  # the agent stays paused, whatever it sends
    assert guard.admit(Attribution(agent="other-bot")).allowed  # other agents are untouched


def test_success_resets_the_streak(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 3}}, clock=clock)
    for i in range(3):
        clock.advance(1)
        assert guard.admit(ACME_BOT, tool_results=tool_error(i)).allowed
    clock.advance(1)
    assert guard.admit(ACME_BOT, tool_results=[ToolResult("crm_lookup", "ok_1", "")]).allowed
    for i in range(3, 6):
        clock.advance(1)
        assert guard.admit(ACME_BOT, tool_results=tool_error(i)).allowed


def test_error_classes_and_tools_count_separately(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 2}}, clock=clock)
    for i in range(2):
        clock.advance(1)
        assert guard.admit(ACME_BOT, tool_results=tool_error(i, "TimeoutError")).allowed
        assert guard.admit(ACME_BOT, tool_results=tool_error(i + 100, "http_503")).allowed
        assert guard.admit(ACME_BOT, tool_results=tool_error(i + 200, tool="search")).allowed


def test_resent_tool_results_are_not_double_counted(make_guard):
    guard = make_guard({"breaker": {"max_repeats": 2, "max_identical_requests": 0}})
    for _ in range(10):  # an SDK retrying the same request carries the same tool_use ids
        assert guard.admit(ACME_BOT, tool_results=tool_error(1)).allowed


def test_failures_outside_the_window_are_forgotten(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 2, "window_seconds": 600}}, clock=clock)
    for i in range(2):
        assert guard.admit(ACME_BOT, tool_results=tool_error(i)).allowed
    clock.advance(601)
    assert guard.admit(ACME_BOT, tool_results=tool_error(9)).allowed


def test_resume_restarts_breaker_counts(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 1}}, clock=clock)
    for i in range(2):
        clock.advance(1)
        guard.admit(ACME_BOT, tool_results=tool_error(i))
    assert guard.admit(ACME_BOT).reason == "paused"
    clock.advance(1)
    assert guard.resume("agent", "support-bot")
    clock.advance(1)
    assert guard.admit(ACME_BOT, tool_results=tool_error(5)).allowed  # not re-tripped by old failures


def test_cooldown_auto_resumes(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_repeats": 1, "cooldown_seconds": 60}}, clock=clock)
    for i in range(2):
        guard.admit(ACME_BOT, tool_results=tool_error(i))
    assert "Auto-resumes" in guard.admit(ACME_BOT).message
    clock.advance(61)
    assert guard.admit(ACME_BOT).allowed
    (resumed,) = events(guard, "resumed")
    assert resumed["by"] == "cooldown"


def test_identical_request_loop_trips(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"max_identical_requests": 3}}, clock=clock)
    for _ in range(3):
        clock.advance(1)
        decision = guard.admit(ACME_BOT, body_hash="same")
        assert decision.allowed
        spend(guard, body_hash="same", retry=decision.retry)
    decision = guard.admit(ACME_BOT, body_hash="same")
    assert decision.reason == "breaker"
    assert events(guard, "breaker_tripped")[0]["signal"] == "identical_requests"


def test_upstream_error_streak_trips(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"api_error_repeats": 2}}, clock=clock)
    for _ in range(3):
        clock.advance(1)
        guard.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=500, error_class="http_500")
    decision = guard.admit(ACME_BOT, upstream="openai")
    assert decision.reason == "paused" and "http_500" in decision.message
    assert events(guard, "breaker_tripped")[0]["signal"] == "api_errors"


def test_upstream_errors_interleaved_with_success_do_not_trip(make_guard):
    clock = FakeClock()
    guard = make_guard({"breaker": {"api_error_repeats": 2}}, clock=clock)
    for _ in range(5):
        clock.advance(1)
        guard.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=429, error_class="http_429")
        clock.advance(1)
        spend(guard)
    assert guard.admit(ACME_BOT, upstream="openai").allowed


def test_breaker_can_be_disabled(make_guard):
    guard = make_guard({"breaker": {"enabled": False, "max_repeats": 1}})
    for i in range(5):
        assert guard.admit(ACME_BOT, tool_results=tool_error(i)).allowed


# -- pauses ------------------------------------------------------------------


def test_manual_pause_scopes(make_guard):
    guard = make_guard()
    guard.pause("task", "T-1", reason="investigating")
    assert guard.admit(ACME_BOT).reason == "paused"
    assert guard.admit(Attribution(agent="support-bot", task="T-2")).allowed
    guard.pause("global", "ignored")
    assert "spendguard resume --all" in guard.admit(Attribution(agent="x")).message
    assert guard.resume("global", "*") and guard.resume("task", "T-1")
    assert not guard.resume("task", "T-1")
    assert guard.admit(ACME_BOT).allowed


# -- credentials -------------------------------------------------------------


def test_credentials_are_hashed_scoped_and_revocable(make_guard):
    guard = make_guard()
    token, cred = guard.mint_credential(agent="support-bot", customer="acme", upstreams=["anthropic"], ttl_seconds=3600)
    assert token.startswith("sg_") and len(token) > 40
    dump = "\n".join(str(tuple(r)) for r in guard.store.query("SELECT * FROM credentials"))
    assert token not in dump  # only the hash is stored
    found, refusal = guard.verify_credential(token)
    assert refusal is None and found.id == cred.id and found.upstreams == ("anthropic",)
    attr = Attribution(agent="support-bot", customer="acme", credential_id=cred.id)
    assert guard.admit(attr, upstream="openai", credential=found).reason == "credential_scope"
    assert guard.admit(attr, upstream="anthropic", credential=found).allowed
    assert guard.revoke_credentials(ids=[cred.id]) == [cred.id]
    assert "revoked" in guard.verify_credential(token)[1].message
    assert guard.verify_credential("sg_made_up")[1].reason == "credential"


def test_credentials_expire(make_guard):
    clock = FakeClock()
    guard = make_guard(clock=clock)
    token, _ = guard.mint_credential(agent="a", ttl_seconds=60)
    assert guard.verify_credential(token)[1] is None
    clock.advance(61)
    refusal = guard.verify_credential(token)[1]
    assert refusal.status == 401 and "expired" in refusal.message
    assert guard.credentials() == [] and len(guard.credentials(include_inactive=True)) == 1
    assert guard.gc_credentials(older_than_seconds=3600) == 0
    assert guard.gc_credentials() == 1


def test_credential_spend_cap(make_guard):
    guard = make_guard()
    token, cred = guard.mint_credential(agent="support-bot", max_usd=1.0)
    attr = Attribution(agent="support-bot", credential_id=cred.id)
    spend(guard, attr)
    assert guard.admit(attr, credential=cred).allowed
    spend(guard, attr)
    decision = guard.admit(attr, credential=cred)
    assert (decision.reason, decision.status) == ("credential_budget", 402)
    assert guard.credential_stats(cred.id) == (3, pytest.approx(1.0))  # two calls + the refusal


def test_revoke_by_task(make_guard):
    guard = make_guard()
    guard.mint_credential(agent="a", task="T-9")
    guard.mint_credential(agent="b", task="T-9")
    guard.mint_credential(agent="c", task="T-1")
    assert len(guard.revoke_credentials(task="T-9")) == 2
    assert [c.agent for c in guard.credentials()] == ["c"]


# -- in-process API ------------------------------------------------------------


def test_call_context_records_sdk_response(make_guard):
    guard = make_guard()
    response = {
        "type": "message",
        "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0},
    }
    with guard.call(agent="support-bot", customer="acme") as call:
        call.record_response(response)
    assert call.cost_usd == pytest.approx(2.0 + 1.0)
    row = guard.store.one("SELECT model, customer, upstream FROM calls")
    assert tuple(row) == ("claude-sonnet-5-5", "acme", "sdk")


def test_call_context_refuses_and_records_failures(make_guard):
    guard = make_guard()

    class RateLimitError(Exception):
        status_code = 429

    with pytest.raises(RateLimitError):
        with guard.call(agent="bot"):
            raise RateLimitError("slow down")
    row = guard.store.one("SELECT outcome, http_status, error_class FROM calls")
    assert tuple(row) == ("error", 429, "http_429")
    guard.pause("agent", "bot")
    with pytest.raises(SpendBlocked) as info:
        with guard.call(agent="bot"):
            pytest.fail("a refused call must not run")
    assert info.value.decision.reason == "paused"


def test_call_context_scans_request_for_tool_loops(make_guard):
    guard = make_guard({"breaker": {"max_repeats": 2}})
    from fakes import anthropic_tool_turn

    for i in range(2):
        with guard.call(agent="bot", request=anthropic_tool_turn(i, "TimeoutError: 30s")) as call:
            call.set_usage(input_tokens=10, output_tokens=1)
    with pytest.raises(SpendBlocked) as info:
        with guard.call(agent="bot", request=anthropic_tool_turn(3, "TimeoutError: 31s")):
            pass
    assert info.value.decision.reason == "breaker"


def test_tool_result_api_trips_breaker(make_guard):
    guard = make_guard({"breaker": {"max_repeats": 2}})
    guard.tool_result(agent="bot", tool="search", error=TimeoutError("read timed out"))
    guard.tool_result(agent="bot", tool="search", error="TimeoutError: read timed out (2)")
    with pytest.raises(SpendBlocked) as info:
        guard.tool_result(agent="bot", tool="search", error=TimeoutError("read timed out"))
    assert "search" in str(info.value) and info.value.decision.status == 423
    with pytest.raises(SpendBlocked):
        guard.tool_result(agent="bot", tool="other")  # paused agents are refused everything


def test_hooks_receive_events(make_guard, tmp_path):
    out = tmp_path / "hook.jsonl"
    script = f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')"
    guard = make_guard({"hooks": [{"events": ["paused"], "command": [sys.executable, "-c", script]}]})
    guard.pause("agent", "bot", reason="testing hooks")
    guard.events.wait()
    (event,) = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert event["kind"] == "paused" and event["reason"] == "testing hooks"
    log = guard.config.events_path.read_text().splitlines()
    assert json.loads(log[-1])["kind"] == "paused"
