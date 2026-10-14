from __future__ import annotations

from pathlib import Path

import pytest

from amz_harness.config import ConfigError, all_site_codes, load_config, site_label, write_example

VALID = """
[harness]
profiles_dir = "profiles"
runs_dir = "runs"
global_concurrency = 2
require_proxy = true

[site_switcher]
mode = "navigate"

[accounts.a1]
email = "a1@example.com"
home_site = "US"
sites = ["US", "DE", "US"]
proxy = "http://user:${TEST_PROXY_PASS}@gate.example.com:8000"
proxy_label = "residential-us-01"
totp_seed = "${TEST_TOTP_SEED}"
min_seconds_between_jobs = 15
max_actions_per_hour = 30
notes = "主账号"

[accounts.a2]
email = "a2@example.com"
home_site = "DE"
sites = ["DE"]
proxy = "http://u:p@gate.example.com:8001"
"""


def write_config(tmp_path: Path, content: str, name: str = "harness.toml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROXY_PASS", "s3cret")
    monkeypatch.setenv("TEST_TOTP_SEED", "GEZDGNBVGY3TQOJQ")


def test_loads_accounts_and_dedupes_sites(tmp_path: Path, env: None) -> None:
    cfg = load_config(write_config(tmp_path, VALID))
    assert set(cfg.accounts) == {"a1", "a2"}
    assert cfg.accounts["a1"].sites == ("US", "DE"), "重复站点应去重且保持顺序"
    assert cfg.global_concurrency == 2
    assert cfg.require_proxy is True
    assert cfg.profiles_dir == tmp_path / "profiles"


def test_env_reference_is_expanded(tmp_path: Path, env: None) -> None:
    cfg = load_config(write_config(tmp_path, VALID))
    proxy = cfg.accounts["a1"].proxy
    assert proxy is not None
    assert proxy.password == "s3cret"
    assert proxy.username == "user"
    assert proxy.label == "residential-us-01"
    assert cfg.accounts["a1"].totp_seed == "GEZDGNBVGY3TQOJQ"


def test_hygiene_fields_parsed(tmp_path: Path, env: None) -> None:
    account = load_config(write_config(tmp_path, VALID)).accounts["a1"]
    assert account.min_seconds_between_jobs == 15
    assert account.max_actions_per_hour == 30
    assert account.notes == "主账号"
    assert account.home_site == "US"


def test_missing_env_var_names_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_PROXY_PASS", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, VALID))
    assert "TEST_PROXY_PASS" in str(excinfo.value)
    assert "环境变量" in str(excinfo.value)


def test_unknown_site_rejected(tmp_path: Path) -> None:
    broken = VALID.replace('sites = ["US", "DE", "US"]', 'sites = ["US", "ZZ"]')
    with pytest.raises(ConfigError, match="未知站点"):
        load_config(write_config(tmp_path, broken))


def test_sites_must_be_non_empty_list(tmp_path: Path) -> None:
    broken = VALID.replace('sites = ["US", "DE", "US"]', 'sites = "US"')
    with pytest.raises(ConfigError, match="字符串数组"):
        load_config(write_config(tmp_path, broken))


def test_bad_email_rejected(tmp_path: Path) -> None:
    broken = VALID.replace('email = "a1@example.com"', 'email = "not-an-email"')
    with pytest.raises(ConfigError, match="合法邮箱"):
        load_config(write_config(tmp_path, broken))


def test_unknown_home_site_rejected(tmp_path: Path) -> None:
    broken = VALID.replace('home_site = "US"', 'home_site = "ZZ"', 1)
    with pytest.raises(ConfigError, match="不是已知站点"):
        load_config(write_config(tmp_path, broken))


def test_bad_proxy_url_rejected(tmp_path: Path) -> None:
    broken = VALID.replace(
        'proxy = "http://user:${TEST_PROXY_PASS}@gate.example.com:8000"',
        'proxy = "http://gate.example.com"',
    )
    with pytest.raises(ConfigError, match="解析失败"):
        load_config(write_config(tmp_path, broken))


def test_missing_config_file_gives_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.toml")
    assert "python -m amz_harness init" in str(excinfo.value)


def test_dropdown_mode_requires_selector(tmp_path: Path) -> None:
    broken = VALID.replace('mode = "navigate"', 'mode = "dropdown"')
    with pytest.raises(ConfigError, match="必须给 selector"):
        load_config(write_config(tmp_path, broken))


def test_bad_switcher_mode_rejected(tmp_path: Path) -> None:
    broken = VALID.replace('mode = "navigate"', 'mode = "magic"')
    with pytest.raises(ConfigError, match="navigate 或 dropdown"):
        load_config(write_config(tmp_path, broken))


def test_unknown_account_lists_available(tmp_path: Path, env: None) -> None:
    cfg = load_config(write_config(tmp_path, VALID))
    with pytest.raises(ConfigError) as excinfo:
        cfg.account("nope")
    assert "a1" in str(excinfo.value)


def test_routing_expands_account_site_pairs(tmp_path: Path, env: None) -> None:
    cfg = load_config(write_config(tmp_path, VALID))
    assert cfg.routing() == [("a1", "US"), ("a1", "DE"), ("a2", "DE")]


def test_requires_at_least_one_account(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="没有任何账号"):
        load_config(write_config(tmp_path, "[harness]\nglobal_concurrency = 1\n"))


def test_concurrency_must_be_positive(tmp_path: Path, env: None) -> None:
    broken = VALID.replace("global_concurrency = 2", "global_concurrency = 0")
    with pytest.raises(ConfigError, match="至少为 1"):
        load_config(write_config(tmp_path, broken))


def test_toml_syntax_error_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TOML 解析失败"):
        load_config(write_config(tmp_path, "[harness\nbroken"))


def test_write_example_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "harness.toml"
    written = write_example(target)
    assert written.exists()
    assert "[accounts." in written.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="不覆盖"):
        write_example(target)


def test_example_template_is_valid_and_loadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """示例模板本身必须能加载——否则新人第一步就被劝退。"""
    monkeypatch.setenv("AMZ_TOTP_SHOP_US_MAIN", "GEZDGNBVGY3TQOJQ")
    monkeypatch.setenv("AMZ_PROXY_SHOP_EU_DE", "http://u:p@gate.example.com:8002")
    path = write_example(tmp_path / "harness.toml")
    cfg = load_config(path)
    assert "shop_us_main" in cfg.accounts


def test_site_helpers() -> None:
    assert "US" in all_site_codes()
    assert "sellercentral.amazon.com" in site_label("US")
