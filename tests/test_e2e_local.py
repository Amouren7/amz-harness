"""端到端：真开 Chromium，打本地 mock 卖家后台。

这是唯一一个"真开浏览器"的测试。它证明的是编排链路能真跑：
环境启动 → 登录 → 2FA → 站点切换校验 → 只读采集 → 闸门判定。

它**不**证明、也无法证明的事：真实亚马逊后台的选择器是否正确。
那必须对着真机校准——这正是 probe 命令存在的理由。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from amz_harness.driver.playwright_driver import PlaywrightDriver
from amz_harness.flows import ScriptedGate, StaticCredentials, login
from amz_harness.mockserver import mock_seller_central
from amz_harness.models import Account, SessionState
from amz_harness.profiles import build_environment, materialize
from amz_harness.readers import CollectRequest, DeltaTracker, collect
from amz_harness.session import classify
from amz_harness.sites import get_site
from amz_harness.switch import switch_to


def _chromium_available() -> bool:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False

    async def check() -> bool:
        pw = await async_playwright().start()
        try:
            return Path(pw.chromium.executable_path).exists()
        finally:
            await pw.stop()

    try:
        return asyncio.run(check())
    except Exception:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _chromium_available(),
        reason="需要 playwright + chromium（python -m playwright install chromium）",
    ),
]


async def _full_chain(workdir: Path) -> None:
    with mock_seller_central() as base_url:
        netloc = urlsplit(base_url).netloc
        us = replace(get_site("US"), scheme="http", domain=netloc)
        de = replace(get_site("DE"), scheme="http", domain=netloc)

        account = Account(
            account_id="e2e",
            email="e2e@example.com",
            home_site="US",
            proxy=None,
            sites=("US", "DE"),
            headless=True,
        )

        # --- 1. 环境启动 ---
        spec, issues = build_environment(account, root=workdir / "profiles", require_proxy=False)
        assert [i for i in issues if i.level == "error"] == []
        assert spec.locale == "en-US"
        report = materialize(spec)
        assert report.ok
        assert (spec.profile_dir / "environment.json").exists()

        driver = PlaywrightDriver(spec, default_timeout_ms=20_000)
        await driver.start()
        try:
            # --- 2. 登录（密码 + 2FA） ---
            gate = ScriptedGate(codes=["123456"])
            outcome = await login(
                driver,
                account,
                us,
                credentials=StaticCredentials({"e2e": "e2e-password"}),
                gate=gate,
                screenshot_dir=workdir / "shots",
            )
            assert outcome.ok, outcome.render()
            assert outcome.state is SessionState.LOGGED_IN
            assert outcome.code_source == "human"
            assert len(gate.requests) == 1

            # --- 3. 站点切换 + 校验 ---
            switched = await switch_to(driver, de, from_site="US")
            assert switched.ok, switched.render()

            # --- 4. 只读采集：订单 ---
            orders = await collect(driver, CollectRequest("e2e", de, "orders", de.orders_url))
            assert orders.state is SessionState.LOGGED_IN
            assert orders.row_count == 3, orders.warnings
            assert orders.mapped_columns["order_id"] == "订单编号"
            assert orders.rows[0]["order_id"] == "113-1234567-8901234"
            assert orders.rows[0]["item_total"] == "29.99"

            # --- 5. 增量：同一批数据第二次不该再报 ---
            tracker = DeltaTracker()
            assert len(tracker.new_rows("DE", "orders", orders.rows)) == 3
            assert tracker.new_rows("DE", "orders", orders.rows) == []

            # --- 6. 只读采集：库存 ---
            inventory = await collect(
                driver, CollectRequest("e2e", de, "inventory", de.inventory_url)
            )
            assert inventory.state is SessionState.LOGGED_IN
            assert inventory.row_count == 3
            assert inventory.rows[2]["available"] == "0"

            # --- 7. 闸门：真浏览器上也要判成"转人工" ---
            probe = await driver.goto(f"{base_url}/ap/captcha")
            verdict = classify(probe, expected_site=de)
            assert verdict.state is SessionState.NEEDS_CAPTCHA
            assert verdict.needs_human
            assert verdict.human_prompt
        finally:
            await driver.close()


def test_full_chain_against_local_mock(tmp_path: Path) -> None:
    asyncio.run(_full_chain(tmp_path))


async def _persistent_profile_keeps_cookies(workdir: Path) -> None:
    """持久化 profile 的意义：关掉再开，登录态还在。"""
    with mock_seller_central() as base_url:
        netloc = urlsplit(base_url).netloc
        us = replace(get_site("US"), scheme="http", domain=netloc)
        account = Account(
            account_id="persist",
            email="p@example.com",
            home_site="US",
            proxy=None,
            sites=("US",),
            headless=True,
        )
        spec, _ = build_environment(account, root=workdir / "profiles", require_proxy=False)
        materialize(spec)

        driver = PlaywrightDriver(spec, default_timeout_ms=20_000)
        await driver.start()
        try:
            outcome = await login(
                driver,
                account,
                us,
                credentials=StaticCredentials({"persist": "pw"}),
                gate=ScriptedGate(codes=["123456"]),
            )
            assert outcome.ok
            # 自己写一个 cookie，验证它落到 profile 目录里而不是内存里。
            # 必须带 expires：不带的话是"会话 cookie"，浏览器**设计上**就不跨重启保留，
            # 那是浏览器行为、不是 profile 没生效。真实亚马逊的登录态和
            # "信任此设备"标记都是带有效期的持久 cookie，所以这里要照样模拟。
            await driver.goto(f"{base_url}/home")
            await driver.context.add_cookies(
                [
                    {
                        "name": "harness_probe",
                        "value": "kept",
                        "url": base_url,
                        "expires": time.time() + 86_400,
                    }
                ]
            )
        finally:
            await driver.close()

        second = PlaywrightDriver(spec, default_timeout_ms=20_000)
        await second.start()
        try:
            cookies = await second.context.cookies(base_url)
            assert any(c["name"] == "harness_probe" for c in cookies), (
                "profile 目录没有持久化 cookie —— 环境隔离就没意义了"
            )
        finally:
            await second.close()


def test_profile_persists_cookies_across_restarts(tmp_path: Path) -> None:
    asyncio.run(_persistent_profile_keeps_cookies(tmp_path))
