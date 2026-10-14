from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from amz_harness.models import Account, Proxy
from amz_harness.profiles import (
    accept_language,
    build_environment,
    materialize,
    resolve_profile_dir,
)
from amz_harness.sites import get_site


def make_account(**overrides) -> Account:
    base = {
        "account_id": "shop_us_main",
        "email": "ops@example.com",
        "home_site": "US",
        "proxy": Proxy.parse("http://user:pass@gate.example.com:8000", label="us-01"),
        "sites": ("US", "DE"),
    }
    base.update(overrides)
    return Account(**base)


# ---------------------------------------------------------------------------
# 指纹锚定：这是 R1
# ---------------------------------------------------------------------------


def test_fingerprint_follows_home_site_not_target_site(tmp_path: Path) -> None:
    """同一账号从 US 切到 DE 时，locale/timezone 不许跟着变。"""
    account = make_account(home_site="US")
    spec, _ = build_environment(account, root=tmp_path)

    assert spec.locale == get_site("US").locale
    assert spec.timezone == get_site("US").timezone

    # 换一个 home_site，指纹才应该换
    other, _ = build_environment(make_account(home_site="DE"), root=tmp_path)
    assert other.locale == get_site("DE").locale
    assert other.timezone == get_site("DE").timezone
    assert other.locale != spec.locale


def test_viewport_is_stable_across_calls(tmp_path: Path) -> None:
    """必须用 hashlib 派生。用内置 hash() 会因进程随机盐导致每次启动都变。"""
    account = make_account()
    first, _ = build_environment(account, root=tmp_path)
    second, _ = build_environment(account, root=tmp_path)
    assert first.viewport == second.viewport
    assert first.viewport in ((1920, 1080), (1536, 864), (1600, 900), (1366, 768))


def test_different_accounts_get_different_profile_dirs(tmp_path: Path) -> None:
    one, _ = build_environment(make_account(account_id="a1"), root=tmp_path)
    two, _ = build_environment(make_account(account_id="a2"), root=tmp_path)
    assert one.profile_dir != two.profile_dir


def test_accept_language() -> None:
    assert accept_language("de-DE") == "de-DE,de;q=0.9,en;q=0.8"
    assert accept_language("en-US") == "en-US,en;q=0.9"


# ---------------------------------------------------------------------------
# 代理：这是 R2
# ---------------------------------------------------------------------------


def test_missing_proxy_blocks_startup(tmp_path: Path) -> None:
    spec, issues = build_environment(make_account(proxy=None), root=tmp_path)
    assert spec.profile_dir  # 规格照样构造出来
    assert [i.code for i in issues] == ["missing_proxy"]
    assert issues[0].level == "error"
    assert "头号风控诱因" in issues[0].message


def test_missing_proxy_allowed_when_explicitly_opted_out(tmp_path: Path) -> None:
    _, issues = build_environment(make_account(proxy=None), root=tmp_path, require_proxy=False)
    assert issues == ()


def test_playwright_proxy_payload() -> None:
    spec, _ = build_environment(make_account(), root=Path("."))
    payload = spec.playwright_proxy()
    assert payload == {
        "server": "http://gate.example.com:8000",
        "username": "user",
        "password": "pass",
    }


def test_proxy_repr_never_leaks_password() -> None:
    proxy = Proxy.parse("http://user:supersecret@gate.example.com:8000")
    assert "supersecret" not in repr(proxy)
    assert "supersecret" not in proxy.display


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def test_profile_dir_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="逃出根目录"):
        resolve_profile_dir(tmp_path, "../outside")


def test_account_id_rejects_path_traversal() -> None:
    for bad in ("../evil", "a/b", "", "-leading", "a" * 65, "a b"):
        with pytest.raises(ValueError, match="不合法"):
            make_account(account_id=bad).validated()


def test_account_requires_at_least_one_site() -> None:
    with pytest.raises(ValueError, match="未配置任何站点"):
        make_account(sites=()).validated()


# ---------------------------------------------------------------------------
# materialize 与指纹漂移
# ---------------------------------------------------------------------------


def test_materialize_creates_profile_and_manifest(tmp_path: Path) -> None:
    spec, _ = build_environment(make_account(), root=tmp_path)
    report = materialize(spec)

    assert report.created is True
    assert report.ok
    manifest = json.loads((spec.profile_dir / "environment.json").read_text(encoding="utf-8"))
    assert manifest["account_id"] == "shop_us_main"
    assert manifest["fingerprint"]["locale"] == "en-US"
    assert manifest["proxy"]["server"] == "http://gate.example.com:8000"
    # 清单里绝不能有代理账密
    assert "password" not in json.dumps(manifest)


def test_second_materialize_reuses_directory(tmp_path: Path) -> None:
    spec, _ = build_environment(make_account(), root=tmp_path)
    materialize(spec)
    again = materialize(spec)
    assert again.created is False
    assert again.drift == ()


def test_fingerprint_drift_is_detected(tmp_path: Path) -> None:
    first, _ = build_environment(make_account(home_site="US"), root=tmp_path)
    materialize(first)
    changed, _ = build_environment(make_account(home_site="DE"), root=tmp_path)
    report = materialize(changed)
    codes = {issue.code for issue in report.drift}
    assert "fingerprint_drift" in codes
    assert all(issue.level == "warn" for issue in report.drift)


def test_proxy_drift_is_detected(tmp_path: Path) -> None:
    first, _ = build_environment(make_account(), root=tmp_path)
    materialize(first)
    changed, _ = build_environment(
        make_account(proxy=Proxy.parse("http://user:pass@other.example.com:9000")), root=tmp_path
    )
    codes = {issue.code for issue in materialize(changed).drift}
    assert "proxy_drift" in codes


def test_explicit_user_agent_is_kept_but_default_is_none(tmp_path: Path) -> None:
    default_spec, _ = build_environment(make_account(), root=tmp_path)
    assert default_spec.user_agent is None, "默认不要覆盖 UA——真实浏览器的 UA 本身就是最一致的那个"

    pinned, _ = build_environment(make_account(user_agent="Mozilla/5.0 (custom)"), root=tmp_path)
    assert pinned.user_agent == "Mozilla/5.0 (custom)"


def test_spec_to_dict_is_json_serializable() -> None:
    spec, _ = build_environment(make_account(), root=Path("."))
    from amz_harness.profiles import spec_to_dict

    payload = json.loads(json.dumps(spec_to_dict(spec)))
    assert payload["proxy"] == "http://user@gate.example.com:8000 [us-01]"
    assert payload["viewport"] == list(spec.viewport)


def test_replace_keeps_frozen_spec_intact() -> None:
    spec, _ = build_environment(make_account(), root=Path("."))
    tweaked = replace(spec, headless=True)
    assert tweaked.headless is True
    assert spec.headless is False
