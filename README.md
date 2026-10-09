# spendguard

**Agent spend containment at the agent→LLM API boundary.** A local reverse
proxy + CLI that sits between your agents and LLM providers, attributes every
call, enforces budget caps, and trips circuit breakers on runaway loops.

Why: a B2B team's customer agent retried a failing tool 31,000× overnight and
burned $4,700 against a $400/mo plan. Global rate limits don't contain a
single agent; per-customer attribution + containment is the missing layer
(motivating incident: reddit.com/r/AI_Agents/comments/1wykdzx, sev 8.0, 61
comments).

## What it does

1. **Spend ledger** — every token/USD cost attributed per agent / customer /
   task / credential, *including failed and retried calls* (those are real
   money).
2. **Loop circuit breaker** — the same endpoint + error-class failing more
   than N times trips the breaker: the agent is paused (423), a hook fires,
   later identical calls are refused without touching the provider.
3. **Budget caps** — soft (alert once per window) and hard (refuse 402 before
   the upstream is called) caps per agent / customer / day in
   `spendguard.yml`.
4. **Scoped, expiring credentials** — `sg_...` keys minted per agent/customer/
   task; a finished task leaves nothing reusable. Raw provider keys are
   refused when `require_credential` is on.
5. **Report** — per-agent/per-customer spend, retry/fail cost share, refusal
   reasons, with exit codes for harness wiring.

## Quick start

```bash
pip install -e .            # stdlib-only runtime; pytest for dev
spendguard serve --config spendguard.yml
```

Point your agent's `base_url` at the proxy: `http://localhost:8093/openai/v1`
(it forwards to the real provider, swapping your `sg_` credential for the real
key, which never reaches the agent).

Example `spendguard.yml` — see `examples/spendguard.yml`.

## Refusal contract

| Shape | Field | Value |
|---|---|---|
| OpenAI error body | `error.code` | bare reason: `budget`, `credential`, `breaker` |
| Anthropic error body | `error.type` | `spendguard_<reason>` |
| HTTP header | `X-Spendguard-Refused` | bare reason |

Status codes: 401 credential, 402 budget, 423 paused/breaker — all non-retryable.

## Layout

```
src/spendguard/
  proxy.py     reverse proxy (metering, refusal, streaming relay)
  guard.py     policy engine: budgets, breaker, credentials
  store.py     sqlite ledger (WAL)
  pricing.py   model price table + unknown-model deterrent
  usage.py     usage parsing/estimation (incl. SSE streams)
  toolscan.py  request tool-result inspection (error classification)
  report.py    report subcommand
  miniyaml.py  minimal YAML reader (stdlib-only config loading)
  config.py    config schema/validation
```

Tests: `python -m pytest` (131 tests, stdlib fakes — no network).

## Status

v0.1.0. Apache-2.0. Zero telemetry. Part of the bugyal agent-ops family
(spendrouter routes spend across models/tiers; spendguard contains a running
agent's spend — complementary layers).