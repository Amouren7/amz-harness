"""登录编排测试：全部走 FakeDriver，离线、毫秒级。

这里验的是**编排正确性**：什么时候填表、什么时候叫人、什么时候必须停手。
真机联调剩下的唯一变量就只是"选择器对不对"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from amz_harness.driver.base import MFA_CODE, MFA_SUBMIT, SIGNIN_EMAIL, SIGNIN_PASSWORD, SIGNIN_SUBMIT
from amz_harness.driver.fake import FakeDriver, FakePage
from amz_harness.flows import DenyGate, LoginOutcome, ScriptedGate, StaticCredentials, login
from amz_harness.models import Account, Marker, SessionState
from amz_harness.sites import get_site

US = get_site("US")
SIGNIN_URL = US.signin_url
MFA_URL = "https://sellercentral.amazon.com/ap/mfa"
DASH_URL = US.home_url
SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def make_account(**overrides) -> Account:
    base: dict = {
        "account_id": "a1",
        "email": "ops@example.com",
        "home_site": "US",
        "proxy": None,
        "sites": ("US",),
    }
    base.update(overrides)
    return Account(**base)


def happy_driver(**overrides) -> FakeDriver:
    pages = {
        SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.SIGNIN_FORM})),
        MFA_URL: FakePage(url=MFA_URL, markers=frozenset({Marker.OTP_INPUT})),
        DASH_URL: FakePage(url=DASH_URL, markers=frozenset({Marker.SELLER_NAV})),
    }
    transitions = {("click", SIGNIN_SUBMIT): MFA_URL, ("click", MFA_SUBMIT): DASH_URL}
    return FakeDriver(pages=pages, transitions=transitions, **overrides)


async def no_sleep(delay: float) -> None:
    return None


def run(driver: FakeDriver, account: Account, **kwargs) -> LoginOutcome:
    kwargs.setdefault("credentials", StaticCredentials({"a1": "pw"}))
    kwargs.setdefault("gate", ScriptedGate(codes=["123456"]))
    return asyncio.run(login(driver, account, US, sleeper=no_sleep, **kwargs))


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_happy_path_with_human_supplied_code() -> None:
    driver = happy_driver()
    gate = ScriptedGate(codes=["123456"])
    outcome = run(driver, make_account(), gate=gate)

    assert outcome.ok, outcome.render()
    assert outcome.state is SessionState.LOGGED_IN
    assert outcome.code_source == "human"
    assert driver.filled[SIGNIN_EMAIL] == "ops@example.com"
    assert driver.filled[SIGNIN_PASSWORD] == "pw"
    assert driver.filled[MFA_CODE] == "123456"
    assert driver.closed is False  # 编排层不负责关浏览器


def test_steps_describe_the_journey() -> None:
    outcome = run(happy_driver(), make_account())
    joined = " → ".join(outcome.steps)
    assert "打开登录页→logged_out" in joined
    assert "提交后→needs_2fa" in joined
    assert "提交验证码后→logged_in" in joined


def test_already_logged_in_short_circuits() -> None:
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.SELLER_NAV}))}
    )
    outcome = run(driver, make_account())
    assert outcome.ok
    assert driver.actions == [], "已经登录了就不该再点任何东西"


def test_totp_seed_makes_two_factor_fully_automatic() -> None:
    driver = happy_driver()
    gate = ScriptedGate(codes=[])  # 没有人工验证码可用
    outcome = run(driver, make_account(totp_seed=SEED), gate=gate)

    assert outcome.ok
    assert outcome.code_source == "totp"
    assert gate.requests == [], "有 TOTP 种子时不该去打扰人"
    code = driver.filled[MFA_CODE]
    assert len(code) == 6 and code.isdigit()


# ---------------------------------------------------------------------------
# 闸门：绝不自动重试
# ---------------------------------------------------------------------------


def test_gate_denied_means_stop_not_retry() -> None:
    driver = happy_driver()
    gate = ScriptedGate(codes=[])
    outcome = run(driver, make_account(), gate=gate)

    assert outcome.state is SessionState.NEEDS_2FA
    assert outcome.needs_human
    assert outcome.human_message and "验证码" in outcome.human_message
    assert MFA_CODE not in driver.filled, "拿不到验证码就不该去填"
    assert len(gate.requests) == 1, "只问一次，不做重试循环"


def test_captcha_on_signin_escalates_before_touching_the_form() -> None:
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.CAPTCHA}))}
    )
    gate = ScriptedGate(codes=["123456"])
    outcome = run(driver, make_account(), gate=gate)

    assert outcome.state is SessionState.NEEDS_CAPTCHA
    assert outcome.needs_human
    assert [a for a in driver.actions if a[0] == "fill"] == [], "验证码页面绝不能去填表单"
    assert len(gate.requests) == 1


def test_blocked_account_escalates() -> None:
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.ACCOUNT_LOCKED}))}
    )
    outcome = run(driver, make_account())
    assert outcome.state is SessionState.BLOCKED
    assert outcome.human_message and "锁定" in outcome.human_message


def test_screenshot_is_attached_to_human_escalation(tmp_path: Path) -> None:
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.CAPTCHA}))}
    )
    outcome = run(driver, make_account(), screenshot_dir=tmp_path / "shots")
    assert outcome.screenshot is not None
    assert outcome.screenshot.exists()
    assert outcome.screenshot in driver.screenshots


# ---------------------------------------------------------------------------
# 凭据与选择器失效：要给出能照着做的错误
# ---------------------------------------------------------------------------


def test_missing_password_is_reported_with_source() -> None:
    outcome = run(happy_driver(), make_account(), credentials=StaticCredentials({}))
    assert not outcome.ok
    assert outcome.human_message
    assert "密码" in outcome.human_message
    assert "内存字典" in outcome.human_message, "要告诉人'去哪儿取密码'，而不是只说取不到"


def test_missing_email_selector_is_reported_as_calibration_issue() -> None:
    driver = happy_driver(fail_selectors={SIGNIN_EMAIL})
    outcome = run(driver, make_account())
    assert not outcome.ok
    assert outcome.human_message
    assert "校准" in outcome.human_message
    assert SIGNIN_PASSWORD not in driver.filled, "邮箱都填不进去就不该继续填密码"


def test_missing_submit_button_is_reported() -> None:
    driver = happy_driver(fail_selectors={SIGNIN_SUBMIT})
    outcome = run(driver, make_account())
    assert not outcome.ok
    assert outcome.human_message and "提交" in outcome.human_message


def test_unknown_page_is_not_filled_blindly() -> None:
    """页面判不出来时，先别填表单——多半是网络被挡或选择器失效。

    注意：如果 URL 明确落在 /ap/signin，即使一个标记都没命中，也会被判成
    LOGGED_OUT（低置信度），这时填表是合理的；真正要拦的是"完全未知"那种。
    """
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url="https://sellercentral.amazon.com/home")}
    )
    outcome = run(driver, make_account())
    assert not outcome.ok
    assert [a for a in driver.actions if a[0] == "fill"] == []
    assert outcome.human_message and "选择器" in outcome.human_message


def test_deny_gate_collects_messages_without_blocking() -> None:
    driver = FakeDriver(
        pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({Marker.CAPTCHA}))}
    )
    gate = DenyGate()
    outcome = run(driver, make_account(), gate=gate)
    assert outcome.needs_human
    assert len(gate.messages) == 1
    assert "需要人工介入" in gate.messages[0]


@pytest.mark.parametrize("marker", [Marker.CAPTCHA, Marker.OTP_INPUT, Marker.ACCOUNT_LOCKED])
def test_every_gate_produces_a_prompt(marker: Marker) -> None:
    driver = FakeDriver(pages={SIGNIN_URL: FakePage(url=SIGNIN_URL, markers=frozenset({marker}))})
    outcome = run(driver, make_account(totp_seed=None), gate=ScriptedGate(codes=[]))
    assert outcome.needs_human
    assert outcome.human_message
