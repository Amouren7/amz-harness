"""站点切换 + 切换后校验。"""

from __future__ import annotations

import asyncio

import pytest

from amz_harness.driver.fake import FakeDriver, FakePage
from amz_harness.models import Marker, SessionState
from amz_harness.sites import get_site
from amz_harness.switch import RotationPlan, switch_to

US = get_site("US")
DE = get_site("DE")
JP = get_site("JP")


def logged_in(url: str) -> FakePage:
    return FakePage(url=url, markers=frozenset({Marker.SELLER_NAV, Marker.MKT_SWITCHER}))


def test_navigate_mode_switches_and_verifies() -> None:
    driver = FakeDriver(pages={DE.home_url: logged_in(DE.home_url)})
    outcome = asyncio.run(switch_to(driver, DE, from_site="US"))
    assert outcome.ok
    assert outcome.verdict.state is SessionState.LOGGED_IN
    assert driver.visits == [DE.home_url]


def test_landing_on_wrong_domain_fails_verification() -> None:
    """只检查"页面加载成功"是不够的——必须确认落在目标域名上。"""
    driver = FakeDriver(pages={DE.home_url: logged_in(US.home_url)})
    outcome = asyncio.run(switch_to(driver, DE, from_site="US"))
    assert not outcome.ok
    assert "非目标域名" in outcome.note
    assert "sellercentral.amazon.de" in outcome.note


def test_switching_into_a_gate_stops_immediately() -> None:
    gate_page = FakePage(
        url=DE.home_url,
        markers=frozenset({Marker.SELLER_NAV, Marker.CAPTCHA}),
    )
    driver = FakeDriver(pages={DE.home_url: gate_page})
    outcome = asyncio.run(switch_to(driver, DE, from_site="US"))
    assert not outcome.ok
    assert outcome.needs_human
    assert "闸门" in outcome.note


def test_session_expired_during_switch_asks_for_relogin() -> None:
    driver = FakeDriver(
        pages={DE.home_url: FakePage(url=DE.home_url, markers=frozenset({Marker.SESSION_EXPIRED}))}
    )
    outcome = asyncio.run(switch_to(driver, DE, from_site="US"))
    assert not outcome.ok
    assert outcome.requires_relogin
    assert "重新登录" in outcome.note


def test_dropdown_mode_requires_selector() -> None:
    driver = FakeDriver(pages={})
    with pytest.raises(ValueError, match="selector"):
        asyncio.run(switch_to(driver, DE, mode="dropdown"))


def test_unknown_mode_rejected() -> None:
    driver = FakeDriver(pages={})
    with pytest.raises(ValueError, match="未知切换模式"):
        asyncio.run(switch_to(driver, DE, mode="teleport"))


def test_unopenable_switcher_is_reported_as_calibration() -> None:
    driver = FakeDriver(pages={}, fail_selectors={"#sw"})
    outcome = asyncio.run(switch_to(driver, DE, mode="dropdown", selector="#sw"))
    assert not outcome.ok
    assert "校准" in outcome.note


def test_dropdown_mode_clicks_through_to_target_site() -> None:
    pages = {US.home_url: logged_in(US.home_url), DE.home_url: logged_in(DE.home_url)}
    transitions = {("click", "#sw"): US.home_url, ("click_text", "DE"): DE.home_url}
    driver = FakeDriver(pages=pages, transitions=transitions)

    outcome = asyncio.run(switch_to(driver, DE, from_site="US", mode="dropdown", selector="#sw"))
    assert outcome.ok
    assert ("click", "#sw") in driver.actions
    assert ("click_text", "DE") in driver.actions


def test_switch_outcome_renders() -> None:
    driver = FakeDriver(pages={JP.home_url: logged_in(JP.home_url)})
    outcome = asyncio.run(switch_to(driver, JP, from_site="DE"))
    assert "DE→JP" in outcome.render()
    assert "OK" in outcome.render()


# ---------------------------------------------------------------------------
# 轮转计划
# ---------------------------------------------------------------------------


def test_rotation_plan_cycles_in_order() -> None:
    plan = RotationPlan(("US", "DE", "JP"))
    assert [plan.next_site() for _ in range(4)] == ["US", "DE", "JP", "US"]


def test_rotation_plan_remembers_position() -> None:
    """每次都从头开始，会让排在前面的站点被打得更频繁，长期累积成风控画像。"""
    plan = RotationPlan(("US", "DE", "JP"))
    plan.next_site()
    plan.next_site()
    assert plan.peek() == "JP"
    assert plan.rounds == 0
    plan.next_site()
    assert plan.rounds == 1


def test_rotation_plan_with_single_site() -> None:
    plan = RotationPlan(("US",))
    assert [plan.next_site() for _ in range(3)] == ["US", "US", "US"]
