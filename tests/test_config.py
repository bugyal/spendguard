from pathlib import Path

import pytest

from spendguard.config import ConfigError, find_config, load_config, parse_config


def test_defaults_without_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDGUARD_CONFIG", raising=False)
    config = load_config()
    assert config.path is None
    assert config.state_dir == Path.home() / ".spendguard"
    assert set(config.upstreams) == {"openai", "anthropic"}
    assert config.upstreams["anthropic"].format == "anthropic"
    assert config.upstreams["anthropic"].auth == "x-api-key"
    assert config.upstreams["openai"].auth == "bearer"
    assert config.budgets == []
    assert config.breaker.max_repeats == 10
    assert config.proxy_url == "http://127.0.0.1:8787"


def test_state_dir_is_relative_to_the_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "conf" / "spendguard.yml"
    cfg.parent.mkdir()
    cfg.write_text("state_dir: data\nproxy:\n  listen: 0.0.0.0:9999\n")
    monkeypatch.setenv("SPENDGUARD_CONFIG", str(cfg))
    config = load_config()
    assert config.path == cfg.resolve()
    assert config.state_dir == cfg.parent.resolve() / "data"
    assert config.db_path.name == "ledger.sqlite3"
    assert config.proxy.port == 9999
    assert config.proxy_url == "http://127.0.0.1:9999"  # clients never dial 0.0.0.0


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        find_config(str(tmp_path / "nope.yml"))


def test_json_config_is_accepted(tmp_path):
    cfg = tmp_path / "spendguard.json"
    cfg.write_text('{"budgets": [{"scope": "agent", "match": "bot", "hard_usd": 3}]}')
    config = load_config(str(cfg))
    assert config.budgets[0].name == "agent:bot:day"


def test_upstreams_merge_add_and_remove():
    config = parse_config(
        {
            "upstreams": {
                "openai": None,
                "anthropic": {"base_url": "http://localhost:9000/"},
                "openrouter": {"base_url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY"},
            }
        }
    )
    assert "openai" not in config.upstreams
    assert config.upstreams["anthropic"].base_url == "http://localhost:9000"
    assert config.upstreams["anthropic"].api_key_env == "ANTHROPIC_API_KEY"  # merged with the default
    assert config.upstreams["openrouter"].format == "openai"


def test_budget_rules():
    config = parse_config(
        {
            "budgets": [
                {"scope": "customer", "match": "*", "soft_usd": 5, "hard_usd": 15},
                {"scope": "global", "period": "month", "hard_usd": 400, "action": "pause"},
            ]
        }
    )
    each, everything = config.budgets
    assert (each.name, each.period, each.action) == ("customer:*:day", "day", "refuse")
    assert each.applies_to("acme") and not each.applies_to("")
    assert everything.match == "*" and everything.applies_to("*")


@pytest.mark.parametrize(
    "data, message",
    [
        ({"budget": []}, "unknown key"),  # the typo that would silently disable every cap
        ({"budgets": [{"scope": "customer", "match": "a", "hard": 5}]}, "unknown key"),
        ({"budgets": [{"scope": "team", "match": "a", "hard_usd": 5}]}, "must be one of"),
        ({"budgets": [{"scope": "customer", "hard_usd": 5}]}, "match is required"),
        ({"budgets": [{"scope": "customer", "match": "a"}]}, "soft_usd, hard_usd"),
        ({"budgets": [{"scope": "customer", "match": "a", "soft_usd": 9, "hard_usd": 5}]}, "above hard_usd"),
        ({"budgets": [{"scope": "customer", "match": "a", "hard_usd": -1}]}, ">= 0"),
        ({"budgets": [{"scope": "customer", "match": "a", "hard_usd": "lots"}]}, "expected a number"),
        ({"budgets": [{"scope": "agent", "match": "a", "hard_usd": 1}, {"scope": "agent", "match": "a", "hard_usd": 2}]}, "duplicate rule"),
        ({"breaker": {"max_repeats": 0}}, ">= 1"),
        ({"breaker": {"error_patterns": ["("]}}, "invalid regex"),
        ({"proxy": {"listen": "localhost"}}, "host:port"),
        ({"upstreams": {"x": {"base_url": "ftp://x"}}}, r"http\(s\) URL"),
        ({"upstreams": {"x": {"format": "openai"}}}, "base_url is required"),
        ({"hooks": [{"events": ["nope"], "command": "true"}]}, "unknown event"),
        ({"hooks": [{"events": ["paused"]}]}, "command, url"),
        ({"pricing": {"m": {"input": 1}}}, "output"),
        ({"timezone": "PST"}, "must be one of"),
    ],
)
def test_validation_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_hook_command_string_is_split_without_a_shell():
    config = parse_config({"hooks": [{"events": "paused", "command": "notify --channel 'ops room'"}]})
    assert config.hooks[0].command == ("notify", "--channel", "ops room")
    assert config.hooks[0].wants("paused") and not config.hooks[0].wants("resumed")


def test_pricing_overrides_and_default():
    config = parse_config({"pricing": {"gpt-4o": {"input": 1, "output": 1}, "default": {"input": 99, "output": 99}}})
    assert config.pricing.lookup("gpt-4o")[0].input == 1
    assert config.pricing.lookup("mystery")[0].input == 99
