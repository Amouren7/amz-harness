"""状态机是这个项目里最该被穷举测试的地方——它是纯函数，没有借口不测。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from amz_harness.models import Flag, Marker, PageProbe, SessionState
from amz_harness.session import RetryAction, SessionMachine, classify
from amz_harness.sites import get_site

US = get_site("US")
DE = get_site("DE")


def probe(*markers: Marker, url: str = "https://sellercentral.amazon.com/home", **kwargs) -> PageProbe:
    kwargs.setdefault("http_status", 200)
    return PageProbe(url=url, markers=frozenset(markers), **kwargs)


# ---------------------------------------------------------------------------
# 基本判定
# ---------------------------------------------------------------------------


def test_logged_in_via_seller_nav() -> None:
    verdict = classify(probe(Marker.SELLER_NAV), expected_site=US)
    assert verdict.state is SessionState.LOGGED_IN
    assert verdict.ok
    assert not verdict.flags


def test_logged_in_via_switcher_only_is_lower_confidence() -> None:
    only_switcher = classify(probe(Marker.MKT_SWITCHER), expected_site=US)
    both = classify(probe(Marker.SELLER_NAV, Marker.MKT_SWITCHER), expected_site=US)
    assert only_switcher.state is SessionState.LOGGED_IN
    assert only_switcher.confidence < both.confidence


def test_signin_form_means_logged_out() -> None:
    verdict = classify(probe(Marker.SIGNIN_FORM, url="https://sellercentral.amazon.com/ap/signin"))
    assert verdict.state is SessionState.LOGGED_OUT


def test_url_only_fallback_is_low_confidence() -> None:
    verdict = classify(PageProbe(url="https://sellercentral.amazon.com/ap/signin", http_status=200))
    assert verdict.state is SessionState.LOGGED_OUT
    assert verdict.confidence < 0.7, "只凭 URL 推断时必须明说把握不大"


# ---------------------------------------------------------------------------
# 优先级：闸门必须压过"看起来已登录"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (Marker.CAPTCHA, SessionState.NEEDS_CAPTCHA),
        (Marker.OTP_INPUT, SessionState.NEEDS_2FA),
        (Marker.ACCOUNT_LOCKED, SessionState.BLOCKED),
    ],
)
def test_gate_beats_seller_nav(marker: Marker, expected: SessionState) -> None:
    """验证码/2FA 是以浮层出现在已登录页面上的。先判 SELLER_NAV 就会漏掉闸门。"""
    verdict = classify(probe(marker, Marker.SELLER_NAV), expected_site=US)
    assert verdict.state is expected
    assert verdict.needs_human


def test_session_expired_beats_signin_form() -> None:
    verdict = classify(probe(Marker.SESSION_EXPIRED, Marker.SIGNIN_FORM))
    assert verdict.state is SessionState.SESSION_EXPIRED


def test_all_gate_states_have_human_prompt() -> None:
    for marker in (Marker.CAPTCHA, Marker.OTP_INPUT, Marker.ACCOUNT_LOCKED):
        verdict = classify(probe(marker), expected_site=US)
        assert verdict.human_prompt, f"{marker} 没有给出可读的人工提示"
        assert "sellercentral" in verdict.human_prompt


# ---------------------------------------------------------------------------
# 网络 / 未知
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 0])
def test_transient_http_is_network_error(status: int) -> None:
    verdict = classify(PageProbe(url="https://sellercentral.amazon.com/home", http_status=status))
    assert verdict.state is SessionState.NETWORK_ERROR


def test_driver_error_is_network_error() -> None:
    verdict = classify(PageProbe(url="https://sellercentral.amazon.com/home", error="导航失败 TimeoutError"))
    assert verdict.state is SessionState.NETWORK_ERROR
    assert "TimeoutError" in verdict.evidence[0]


def test_no_markers_is_unknown_with_partial_render_flag() -> None:
    verdict = classify(PageProbe(url="https://sellercentral.amazon.com/home", http_status=200))
    assert verdict.state is SessionState.UNKNOWN
    assert Flag.PARTIAL_RENDER in verdict.flags


# ---------------------------------------------------------------------------
# 标记位与状态正交
# ---------------------------------------------------------------------------


def test_health_alert_is_a_flag_not_a_state() -> None:
    """登录成功 + 账号健康告警必须能同时成立，否则会漏报。"""
    verdict = classify(probe(Marker.SELLER_NAV, Marker.HEALTH_ALERT), expected_site=US)
    assert verdict.state is SessionState.LOGGED_IN
    assert verdict.ok
    assert Flag.ACCOUNT_ALERT in verdict.flags


def test_unusual_activity_flag() -> None:
    verdict = classify(probe(Marker.SELLER_NAV, Marker.UNUSUAL_ACTIVITY), expected_site=US)
    assert Flag.UNUSUAL_ACTIVITY in verdict.flags


def test_unexpected_domain_flag() -> None:
    verdict = classify(probe(Marker.SELLER_NAV, url="https://sellercentral.amazon.de/home"), expected_site=US)
    assert verdict.state is SessionState.LOGGED_IN
    assert Flag.UNEXPECTED_DOMAIN in verdict.flags


def test_expected_domain_with_port_is_not_flagged() -> None:
    mock = replace(US, scheme="http", domain="127.0.0.1:8848")
    verdict = classify(probe(Marker.SELLER_NAV, url="http://127.0.0.1:8848/home?mkt=DE"), expected_site=mock)
    assert Flag.UNEXPECTED_DOMAIN not in verdict.flags


# ---------------------------------------------------------------------------
# 重试策略——闸门永不自动重试
# ---------------------------------------------------------------------------


def test_gate_never_auto_retries_no_matter_how_many_times() -> None:
    machine = SessionMachine(account_id="a1")
    for _ in range(10):
        machine.observe(classify(probe(Marker.CAPTCHA), expected_site=US))
        assert machine.decide() is RetryAction.ESCALATE_HUMAN


def test_two_fa_escalates_even_with_totp_configured() -> None:
    """有 TOTP 种子也不改变"重试策略"这一层：闸门就是闸门。"""
    machine = SessionMachine(account_id="a1")
    machine.observe(classify(probe(Marker.OTP_INPUT), expected_site=US))
    assert machine.decide() is RetryAction.ESCALATE_HUMAN


def test_logged_out_triggers_relogin_then_escalates() -> None:
    machine = SessionMachine(account_id="a1", max_relogin_attempts=2)
    machine.observe(classify(probe(Marker.SIGNIN_FORM), expected_site=US))
    assert machine.decide() is RetryAction.RELOGIN

    machine.record_relogin()
    assert machine.decide() is RetryAction.RELOGIN

    machine.record_relogin()
    assert machine.decide() is RetryAction.ESCALATE_HUMAN, "反复重登本身就是风控信号"


def test_network_error_needs_a_streak_before_restarting_env() -> None:
    machine = SessionMachine(account_id="a1")
    machine.observe(classify(PageProbe(url="u", http_status=503)))
    assert machine.decide() is RetryAction.NONE
    machine.observe(classify(PageProbe(url="u", http_status=503)))
    assert machine.decide() is RetryAction.RESTART_ENV


def test_unknown_needs_longer_streak() -> None:
    machine = SessionMachine(account_id="a1", max_unknown_streak=3)
    for _ in range(2):
        machine.observe(classify(PageProbe(url="u", http_status=200)))
        assert machine.decide() is RetryAction.NONE
    machine.observe(classify(PageProbe(url="u", http_status=200)))
    assert machine.decide() is RetryAction.RESTART_ENV


def test_state_change_resets_relogin_budget() -> None:
    machine = SessionMachine(account_id="a1")
    machine.observe(classify(probe(Marker.SIGNIN_FORM), expected_site=US))
    machine.record_relogin()
    machine.record_relogin()
    assert machine.decide() is RetryAction.ESCALATE_HUMAN

    machine.observe(classify(probe(Marker.SELLER_NAV), expected_site=US))
    assert machine.relogin_attempts == 0
    assert machine.decide() is RetryAction.NONE


def test_streak_counts_only_trailing_identical_states() -> None:
    machine = SessionMachine(account_id="a1")
    machine.observe(classify(PageProbe(url="u", http_status=503)))
    machine.observe(classify(probe(Marker.SELLER_NAV), expected_site=US))
    machine.observe(classify(PageProbe(url="u", http_status=503)))
    assert machine.streak() == 1
    assert machine.streak(SessionState.NETWORK_ERROR) == 1
