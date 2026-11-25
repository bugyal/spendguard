# spendguard

**Spend containment, not spend optimization.** A runaway agent loop is not
a routing problem — it is an incident. A B2B team's customer agent retried
a failing tool 31,000× overnight and burned **$4,700 against a $400/mo
plan**; no global rate limit contains a single agent. `spendguard` is a
local reverse proxy + CLI that sits between your agents and LLM providers,
attributes every call (including failed and retried ones — they are real
money), enforces per-agent/customer budget caps, and trips a circuit
breaker on runaway loops: the agent is paused before the invoice arrives.

The wedge vs routing tools: **spendrouter decides where spend goes;
spendguard decides whether spend is allowed to keep happening at all.**
You want the router when costs are high but sane. You want the guard at
3 a.m. when one agent is in a retry loop and the meter is running. They
compose: route to the cheap tier, and contain when anything loops.

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

## Worked example — real output

Everything below is verbatim from a live run of the shipped CLI (credential
token redacted; it is shown exactly once by the tool and never again):

```bash
$ spendguard init config.yml
wrote config.yml

$ spendguard -c config.yml creds mint --agent support-bot \
    --task "ticket-4821" --max-usd 5.00 --ttl 3600
spendguard: minted cred_cdb0f11b6fe9 for agent 'support-bot'
(expires 2026-11-25 05:14 UTC); the token is shown once
sg_EnJPu3eWTDw6kAp5JLp9jpELFFGroQnPRF8_HKGdIRc
```

The credential is scoped (agent + task), capped (`--max-usd 5.00`) and
expiring (`--ttl 3600`) — a finished task leaves nothing reusable. Point
the agent's `base_url` at the proxy and every call is attributed to
`support-bot` / `ticket-4821` in the ledger.

```bash
$ spendguard -c config.yml status
spendguard status · ledger .spendguard/ledger.sqlite3

paused
  (nothing)

active credentials: 1
  cred_cdb0f11b6fe9  agent=support-bot task=ticket-4821  expires 2026-11-25 05:14 UTC

budgets (current period)
  OK    customer:acme:month      customer=acme   $0.00 (soft $300.00 / hard $400.00)
  OK    global:*:day             *               $0.00 (hard $200.00)

status: OK (exit 0)
```

When the retry loop from the motivating incident starts — the same endpoint
failing in the same class past `max_repeats` — the breaker pauses the agent
(423, non-retryable), `status` and `report` show it, and later identical
calls are refused without touching the provider. `report` splits spend by
customer/agent and counts **retries and failed calls as spend** — they are
real money; that is how a $4,700 overnight loop becomes visible at $50
instead of at invoice time.

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