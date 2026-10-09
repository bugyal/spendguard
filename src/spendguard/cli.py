"""Command line: init | serve | report | check | status | pause | resume | creds | run."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .config import Config, ConfigError, load_config
from .guard import Attribution, Credential, Guard
from .miniyaml import YAMLError
from .proxy import HEALTH_PATH, ProxyServer
from .report import (
    EXIT_ERROR,
    EXIT_HARD,
    EXIT_OK,
    EXIT_PAUSED,
    EXIT_SOFT,
    EXIT_USAGE,
    GROUPS,
    STATUS_NAMES,
    ReportOptions,
    build_report,
    containment_status,
    render,
)

__all__ = ["main"]

TEMPLATE_PATH = Path(__file__).with_name("spendguard.example.yml")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class UsageError(Exception):
    pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except UsageError as exc:
        print(f"spendguard: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ConfigError, YAMLError) as exc:
        print(f"spendguard: config error: {exc}", file=sys.stderr)
        return EXIT_ERROR


# -- helpers -----------------------------------------------------------------


def parse_duration(text: str) -> float:
    """'90s', '15m', '2h', '7d', '1w', or plain seconds."""
    match = _DURATION.match(text or "")
    if not match:
        raise UsageError(f"invalid duration {text!r} (examples: 90s, 15m, 2h, 7d)")
    return float(match.group(1)) * _UNITS[match.group(2)]


def parse_time(text: Optional[str], tz: str, now: float) -> Optional[float]:
    """An ISO date/time (in the configured zone unless it says otherwise), or a duration ago."""
    if text is None:
        return None
    if _DURATION.match(text):
        return now - parse_duration(text)
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise UsageError(f"invalid time {text!r} (use 2026-10-01, 2026-10-01T09:30, or a duration like 24h)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc) if tz == "utc" else dt.astimezone()
    return dt.timestamp()


def _usd(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


def _when(ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _guard(args: argparse.Namespace) -> Guard:
    return Guard(load_config(getattr(args, "config", None)))


def _scope(args: argparse.Namespace) -> Tuple[str, str]:
    if getattr(args, "all", False):
        return "global", "*"
    for scope in ("agent", "customer", "task"):
        value = getattr(args, scope, None)
        if value:
            return scope, value
    raise UsageError("give one of --agent, --customer, --task or --all")


def _print_table(rows: List[List[str]], headers: List[str]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for row in [headers] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


# -- commands ----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"spendguard: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {path}")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    config = load_config(getattr(args, "config", None))
    host, port = config.proxy.host, config.proxy.port
    if args.listen:
        host_part, _, port_part = args.listen.rpartition(":")
        if not port_part.isdigit():
            raise UsageError(f"--listen expects host:port, got {args.listen!r}")
        host, port = host_part.strip("[]") or host, int(port_part)
    guard = Guard(config)
    server = ProxyServer(guard, host, port, quiet=args.quiet)
    print(f"spendguard {__version__} proxy listening on {server.url}", file=sys.stderr)
    print(f"  ledger: {config.db_path}", file=sys.stderr)
    for name, up in config.upstreams.items():
        if up.api_key_env and os.environ.get(up.api_key_env):
            key = f"credentials use ${up.api_key_env}"
        elif up.api_key_env:
            key = f"${up.api_key_env} unset: sg_ credentials get 502, passthrough keys still work"
        else:
            key = "passthrough keys only"
        print(f"  {server.url}/{name} -> {up.base_url}  [{up.format}; {key}]", file=sys.stderr)

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
        guard.close()
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    guard = _guard(args)
    try:
        by = tuple(b.strip() for b in args.by.split(",") if b.strip())
        unknown = [b for b in by if b not in GROUPS and b != "call"]
        if unknown or not by or ("call" in by and len(by) > 1):
            raise UsageError(f"--by takes 'call' or a comma list of: {', '.join(GROUPS)}")
        now = guard.clock()
        tz = guard.config.timezone
        opts = ReportOptions(
            period=args.period,
            since=parse_time(args.since, tz, now),
            until=parse_time(args.until, tz, now),
            by=by,
            agent=args.agent,
            customer=args.customer,
            task=args.task,
            run=args.run,
            limit=args.limit,
        )
        report = build_report(guard, opts)
        print(render(report, args.format))
        return EXIT_OK if args.exit_zero else report["status"]
    finally:
        guard.close()


def cmd_check(args: argparse.Namespace) -> int:
    if not (args.agent or args.customer or args.task):
        raise UsageError("give at least one of --agent, --customer, --task")
    guard = _guard(args)
    try:
        attr = Attribution(agent=args.agent or "", task=args.task or "", customer=args.customer or "")
        pause = guard.active_pause(attr)
        states = guard.budget_states(attr)
        status = containment_status(states, [pause] if pause else [])
        if not args.quiet:
            if pause is not None:
                target = "everything" if pause.scope == "global" else f"{pause.scope}={pause.value}"
                print(f"PAUSED  {target} — {pause.reason}")
            for s in states:
                if s.level != "ok":
                    limit = s.rule.hard_usd if s.level == "hard" else s.rule.soft_usd
                    print(f"{s.level.upper():<6}  {s.label} spent {_usd(s.spend)} this {s.rule.period} (cap {_usd(limit)}, {s.rule.name})")
            if status == EXIT_OK:
                print("OK")
        return status
    finally:
        guard.close()


def cmd_status(args: argparse.Namespace) -> int:
    guard = _guard(args)
    try:
        pauses = guard.pauses()
        creds = guard.credentials()
        states = guard.budget_overview()
        print(f"spendguard status · ledger {guard.config.db_path}")
        print("\npaused")
        if not pauses:
            print("  (nothing)")
        for p in pauses:
            target = "everything" if p.scope == "global" else f"{p.scope}={p.value}"
            until = f"until {_when(p.until)}" if p.until else "until resumed"
            print(f"  {target}  [{p.kind}] since {_when(p.paused_at)}, {until} — {p.reason}")
        print(f"\nactive credentials: {len(creds)}")
        for c in creds:
            scope = " ".join(f"{k}={v}" for k, v in (("agent", c.agent), ("task", c.task), ("customer", c.customer)) if v)
            print(f"  {c.id}  {scope}  expires {_when(c.expires_at)}")
        if states:
            print("\nbudgets (current period)")
            for s in states:
                caps = " / ".join(f"{n} {_usd(v)}" for n, v in (("soft", s.rule.soft_usd), ("hard", s.rule.hard_usd)) if v is not None)
                print(f"  {s.level.upper():<4}  {s.rule.name:<24} {s.label:<24} {_usd(s.spend)} ({caps})")
        status = containment_status(states, pauses)
        print(f"\nstatus: {STATUS_NAMES[status]} (exit {status})")
        return status
    finally:
        guard.close()


def cmd_pause(args: argparse.Namespace) -> int:
    scope, value = _scope(args)
    guard = _guard(args)
    try:
        until = guard.clock() + parse_duration(args.duration) if args.duration else None
        guard.pause(scope, value, reason=args.reason, until=until)
        target = "everything" if scope == "global" else f"{scope} {value!r}"
        print(f"paused {target}" + (f" until {_when(until)}" if until else " until resumed"))
        return EXIT_OK
    finally:
        guard.close()


def cmd_resume(args: argparse.Namespace) -> int:
    scope, value = _scope(args)
    guard = _guard(args)
    try:
        target = "everything" if scope == "global" else f"{scope} {value!r}"
        if guard.resume(scope, value):
            print(f"resumed {target}")
            return EXIT_OK
        print(f"spendguard: {target} was not paused", file=sys.stderr)
        return EXIT_ERROR
    finally:
        guard.close()


def _mint(guard: Guard, args: argparse.Namespace, note: str = "") -> Tuple[str, Credential]:
    try:
        return guard.mint_credential(
            agent=args.agent,
            task=args.task or "",
            customer=args.customer or "",
            run=args.run or "",
            ttl_seconds=parse_duration(args.ttl) if args.ttl else None,
            max_usd=args.max_usd,
            upstreams=args.upstream or (),
            note=note or (args.note or ""),
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def cmd_creds_mint(args: argparse.Namespace) -> int:
    guard = _guard(args)
    try:
        token, cred = _mint(guard, args)
        # The token goes to stdout alone, so TOKEN=$(spendguard creds mint ...) works.
        print(token)
        expiry = f"expires {_when(cred.expires_at)}" if cred.expires_at else "no expiry"
        print(f"spendguard: minted {cred.id} for agent {cred.agent!r} ({expiry}); the token is shown once", file=sys.stderr)
        return EXIT_OK
    finally:
        guard.close()


def cmd_creds_list(args: argparse.Namespace) -> int:
    guard = _guard(args)
    try:
        now = guard.clock()
        rows = []
        for c in guard.credentials(include_inactive=args.all):
            calls, spent = guard.credential_stats(c.id)
            state = "revoked" if c.revoked_at else ("expired" if not c.active(now) else "active")
            rows.append(
                [c.id, c.agent, c.task or "-", c.customer or "-", _when(c.expires_at), _usd(c.max_usd), _usd(spent), str(calls), state]
            )
        if not rows:
            print("(no credentials)" if args.all else "(no active credentials; --all shows expired and revoked ones)")
        else:
            _print_table(rows, ["id", "agent", "task", "customer", "expires", "max", "spent", "calls", "status"])
        return EXIT_OK
    finally:
        guard.close()


def cmd_creds_revoke(args: argparse.Namespace) -> int:
    if not (args.ids or args.agent or args.task):
        raise UsageError("give credential ids, --agent or --task")
    guard = _guard(args)
    try:
        revoked = guard.revoke_credentials(ids=args.ids, agent=args.agent, task=args.task)
        for cred_id in revoked:
            print(f"revoked {cred_id}")
        if not revoked:
            print("spendguard: no active credentials matched", file=sys.stderr)
            return EXIT_ERROR
        return EXIT_OK
    finally:
        guard.close()


def cmd_creds_gc(args: argparse.Namespace) -> int:
    guard = _guard(args)
    try:
        removed = guard.gc_credentials(parse_duration(args.older_than))
        print(f"removed {removed} expired or revoked credential(s)")
        return EXIT_OK
    finally:
        guard.close()


def _proxy_alive(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via HTTP_PROXY
    try:
        with opener.open(url + HEALTH_PATH, timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def child_env(config: Config, proxy_url: str, token: str, cred: Credential, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for a guarded child: proxy base URLs, and the credential in place of every provider key."""
    env = dict(os.environ if base is None else base)
    for name, up in config.upstreams.items():
        url = f"{proxy_url}/{name}"
        if up.api_key_env:
            env[up.api_key_env] = token  # the real key never reaches the agent
        env[f"SPENDGUARD_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}_BASE_URL"] = url
        if name == "openai":
            env["OPENAI_BASE_URL"] = url + "/v1"
        elif name == "anthropic":
            env["ANTHROPIC_BASE_URL"] = url
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    hosts = [h for h in no_proxy.split(",") if h]
    for host in ("127.0.0.1", "localhost"):
        if host not in hosts:
            hosts.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(hosts)
    env.update(
        SPENDGUARD_PROXY=proxy_url,
        SPENDGUARD_KEY=token,
        SPENDGUARD_CREDENTIAL_ID=cred.id,
        SPENDGUARD_AGENT=cred.agent,
        SPENDGUARD_TASK=cred.task,
        SPENDGUARD_CUSTOMER=cred.customer,
        SPENDGUARD_RUN=cred.run or cred.id,
    )
    return env


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.cmd)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise UsageError("nothing to run: spendguard run --agent NAME [options] -- COMMAND [ARGS...]")
    guard = _guard(args)
    proxy_url = (args.proxy or guard.config.proxy_url).rstrip("/")
    try:
        if not args.no_check and not _proxy_alive(proxy_url):
            print(f"spendguard: no proxy answering at {proxy_url} — start one with `spendguard serve` (or pass --proxy)", file=sys.stderr)
            guard.close()
            return EXIT_ERROR
        token, cred = _mint(guard, args, note=("spendguard run: " + " ".join(command))[:200])
    except BaseException:
        guard.close()
        raise
    print(
        f"spendguard: run {cred.run or cred.id} · agent={cred.agent} · credential {cred.id} (expires {_when(cred.expires_at)})",
        file=sys.stderr,
    )
    code = 127
    try:
        try:
            proc = subprocess.Popen(command, env=child_env(guard.config, proxy_url, token, cred))
        except OSError as exc:
            print(f"spendguard: cannot run {command[0]!r}: {exc}", file=sys.stderr)
        else:

            def forward(signum: int, frame: Any) -> None:
                proc.send_signal(signum)

            handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                code = proc.wait()
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    finally:
        guard.revoke_credentials(ids=[cred.id])  # the finished task leaves nothing reusable
        calls, spent = guard.credential_stats(cred.id)
        print(f"spendguard: run finished (exit {code}) · {calls} call(s) · {_usd(spent)} · {cred.id} revoked", file=sys.stderr)
        guard.close()
    return code if code >= 0 else 128 - code


# -- argument parsing ----------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="path to spendguard.yml (default: $SPENDGUARD_CONFIG, then ./spendguard.yml)"
    )
    parser = argparse.ArgumentParser(
        prog="spendguard",
        parents=[common],
        description="Spend containment for running agents: per-agent ledger, loop circuit breaker, budget caps.",
        epilog="exit codes: 0 ok · 1 error · 2 usage · 3 soft cap exceeded · 4 hard cap reached · 5 paused",
    )
    parser.add_argument("--version", action="version", version=f"spendguard {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("init", parents=[common], help="write a commented spendguard.yml")
    p.add_argument("path", nargs="?", default="spendguard.yml")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", parents=[common], help="run the proxy daemon (foreground)")
    p.add_argument("--listen", help="host:port (default: proxy.listen from the config)")
    p.add_argument("--quiet", action="store_true", help="no per-call log lines")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("report", parents=[common], help="spend rollup; exit code reflects containment state")
    p.add_argument("--period", choices=("hour", "day", "week", "month", "total"), default="day", help="current calendar period (default: day)")
    p.add_argument("--since", help="start: ISO date/time, or a duration ago (24h, 7d); overrides --period")
    p.add_argument("--until", help="end: ISO date/time, or a duration ago")
    p.add_argument("--by", default="customer,agent", help=f"group by a comma list of {', '.join(GROUPS)}; or 'call' for per-call rows")
    p.add_argument("--agent")
    p.add_argument("--customer")
    p.add_argument("--task")
    p.add_argument("--run")
    p.add_argument("--limit", type=int, default=50, help="max rows (default: 50)")
    p.add_argument("--format", choices=("text", "md", "json"), default="text")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0 (just print the report)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("check", parents=[common], help="gate a run: exit 0 ok, 3 soft, 4 hard cap, 5 paused")
    p.add_argument("--agent")
    p.add_argument("--customer")
    p.add_argument("--task")
    p.add_argument("--quiet", action="store_true", help="exit code only")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", parents=[common], help="paused scopes, active credentials, budget state")
    p.set_defaults(func=cmd_status)

    for name, helptext in (("pause", "pause an agent, customer, task or everything"), ("resume", "lift a pause")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--agent")
        target.add_argument("--customer")
        target.add_argument("--task")
        target.add_argument("--all", action="store_true", help="every call through spendguard")
        if name == "pause":
            p.add_argument("--reason", default="paused by operator")
            p.add_argument("--for", dest="duration", help="auto-resume after this long (e.g. 30m)")
            p.set_defaults(func=cmd_pause)
        else:
            p.set_defaults(func=cmd_resume)

    creds = sub.add_parser("creds", parents=[common], help="scoped, expiring spend credentials")
    csub = creds.add_subparsers(dest="creds_command", metavar="ACTION")

    def creds_help(args: argparse.Namespace) -> int:
        creds.print_help(sys.stderr)
        return EXIT_USAGE

    creds.set_defaults(func=creds_help)

    def scope_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", required=True)
        p.add_argument("--task")
        p.add_argument("--customer")
        p.add_argument("--run", help="run id for per-run rollups (default: the credential id)")
        p.add_argument("--max-usd", type=float, help="the credential stops working after this much spend")
        p.add_argument("--upstream", action="append", help="limit to this upstream (repeatable)")

    p = csub.add_parser("mint", parents=[common], help="mint a credential; prints the token once")
    scope_args(p)
    p.add_argument("--ttl", help="expire after this long (e.g. 2h); omit for a standing per-agent subkey")
    p.add_argument("--note")
    p.set_defaults(func=cmd_creds_mint)

    p = csub.add_parser("list", parents=[common], help="list credentials")
    p.add_argument("--all", action="store_true", help="include expired and revoked")
    p.set_defaults(func=cmd_creds_list)

    p = csub.add_parser("revoke", parents=[common], help="revoke by id, or all of an agent's / task's")
    p.add_argument("ids", nargs="*")
    p.add_argument("--agent")
    p.add_argument("--task")
    p.set_defaults(func=cmd_creds_revoke)

    p = csub.add_parser("gc", parents=[common], help="delete expired and revoked credentials")
    p.add_argument("--older-than", default="0s", help="only those dead for longer than this (default: 0s)")
    p.set_defaults(func=cmd_creds_gc)

    p = sub.add_parser(
        "run",
        parents=[common],
        help="run a command with a credential minted for it and revoked when it exits",
        description="Mint an expiring credential, run COMMAND with provider base URLs pointed at the proxy and "
        "the credential in place of every provider key, then revoke the credential. Exits with COMMAND's code.",
    )
    scope_args(p)
    p.add_argument("--ttl", default="12h", help="backstop expiry if spendguard itself dies (default: 12h)")
    p.add_argument("--proxy", help="proxy URL (default: from proxy.listen)")
    p.add_argument("--no-check", action="store_true", help="do not check that the proxy is up first")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARGS...]")
    p.set_defaults(func=cmd_run)
    return parser
