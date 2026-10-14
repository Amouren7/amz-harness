"""登录编排 + 人工兜底通道。

核心主张：**"需要人"不是异常分支，是正常分支。**

2FA / 图形验证码 / 账号锁定，这三件事在这门生意里是常态，不是意外。
把它们写成 `except: retry 3 times`，就是在系统性地把账号往火坑里推——
对着验证码重试，等于告诉风控"我是脚本"。

安全设计：**密码不进配置文件**。Account 里根本没有 password 字段，
密码只能从环境变量/凭据库经 CredentialProvider 取。
配置文件是要进 git 的，密码进了 git 就是事故。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .driver.base import MFA_CODE, MFA_SUBMIT, SIGNIN_EMAIL, SIGNIN_PASSWORD, SIGNIN_SUBMIT, DriverLike
from .models import Account, SessionState
from .session import Verdict, classify
from .sites import Site
from .totp import mask as mask_seed
from .totp import seconds_remaining, totp

# 验证码有效期只剩这么少秒时，先等下一轮再填。
# 否则码在提交完成前就过期了——白跑一次，还多留一次登录尝试记录。
_MIN_CODE_SECONDS = 3.0


class CredentialProvider(Protocol):
    def password_for(self, account_id: str) -> str | None: ...

    def describe(self, account_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class EnvCredentials:
    """从环境变量读密码：AMZ_PASSWORD_<ACCOUNT_ID 大写>。"""

    prefix: str = "AMZ_PASSWORD_"

    def _key(self, account_id: str) -> str:
        return self.prefix + re.sub(r"[^A-Za-z0-9]", "_", account_id).upper()

    def password_for(self, account_id: str) -> str | None:
        return os.environ.get(self._key(account_id)) or None

    def describe(self, account_id: str) -> str:
        return f"环境变量 {self._key(account_id)}"


@dataclass(frozen=True, slots=True)
class StaticCredentials:
    """测试/单机调试用。"""

    passwords: dict[str, str] = field(default_factory=dict)

    def password_for(self, account_id: str) -> str | None:
        return self.passwords.get(account_id)

    def describe(self, account_id: str) -> str:
        return f"内存字典（{account_id}）"


@dataclass(frozen=True, slots=True)
class HumanRequest:
    account_id: str
    site_code: str
    state: SessionState
    reason: str
    screenshot: Path | None = None

    def render(self) -> str:
        shot = f"\n  截图：{self.screenshot}" if self.screenshot else ""
        return (
            f"[需要人工介入] 账号={self.account_id} 站点={self.site_code} "
            f"状态={self.state.value}\n  原因：{self.reason}{shot}"
        )


class HumanGate(Protocol):
    async def notify(self, request: HumanRequest) -> None: ...

    async def provide_code(self, request: HumanRequest) -> str | None: ...


@dataclass
class DenyGate:
    """无人值守时的默认策略：不猜、不重试、如实上报。"""

    messages: list[str] = field(default_factory=list)

    async def notify(self, request: HumanRequest) -> None:
        self.messages.append(request.render())

    async def provide_code(self, request: HumanRequest) -> str | None:
        self.messages.append(request.render())
        return None


@dataclass
class ScriptedGate:
    """测试用：预置验证码序列。"""

    codes: list[str] = field(default_factory=list)
    requests: list[HumanRequest] = field(default_factory=list)

    async def notify(self, request: HumanRequest) -> None:
        self.requests.append(request)

    async def provide_code(self, request: HumanRequest) -> str | None:
        self.requests.append(request)
        return self.codes.pop(0) if self.codes else None


@dataclass
class ConsoleGate:
    """有人值守的终端：打印提示，等输入。

    接飞书/Telegram 机器人时，实现同样两个方法即可，编排层不用改。
    """

    async def notify(self, request: HumanRequest) -> None:
        print(request.render(), flush=True)

    async def provide_code(self, request: HumanRequest) -> str | None:
        print(request.render(), flush=True)
        try:
            return (await asyncio.to_thread(input, "验证码 > ")).strip() or None
        except (EOFError, KeyboardInterrupt):
            return None


@dataclass
class LoginOutcome:
    account_id: str
    site_code: str
    state: SessionState
    verdict: Verdict
    steps: list[str] = field(default_factory=list)
    code_source: str | None = None
    screenshot: Path | None = None
    human_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is SessionState.LOGGED_IN

    @property
    def needs_human(self) -> bool:
        return self.state.is_gate or bool(self.human_message)

    def render(self) -> str:
        return f"登录 {self.account_id}/{self.site_code} -> {self.state.value}；" + " → ".join(self.steps)


async def login(
    driver: DriverLike,
    account: Account,
    site: Site,
    *,
    credentials: CredentialProvider | None = None,
    gate: HumanGate | None = None,
    screenshot_dir: Path | None = None,
    log=None,
    timeout_ms: int = 30_000,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LoginOutcome:
    """走一遍登录。返回"事实"，不抛异常——调用方按 state 决定下一步。"""
    credentials = credentials or EnvCredentials()
    gate = gate or DenyGate()
    steps: list[str] = []

    probe = await driver.goto(site.signin_url, timeout_ms=timeout_ms)
    verdict = classify(probe, expected_site=site)
    steps.append(f"打开登录页→{verdict.state.value}")
    if log is not None:
        log.event("login_page", state=verdict.state.value, url=probe.url, evidence=verdict.evidence)

    if verdict.ok:
        return LoginOutcome(account.account_id, site.code, verdict.state, verdict, steps)

    if verdict.state is SessionState.NEEDS_2FA:
        return await _handle_mfa(
            driver, account, site, gate, steps, screenshot_dir, log, timeout_ms, sleeper
        )

    if verdict.state.is_gate:
        return await _escalate(driver, account, site, verdict, steps, screenshot_dir, gate, log)

    if verdict.state not in (SessionState.LOGGED_OUT, SessionState.SESSION_EXPIRED):
        # 登录页都读不出来就别硬填表单——多半是选择器失效或网络被挡。
        return await _escalate(
            driver,
            account,
            site,
            verdict,
            steps,
            screenshot_dir,
            gate,
            log,
            reason=f"登录页状态异常（{verdict.state.value}）。先别填表单，检查网络/选择器。",
        )

    password = credentials.password_for(account.account_id)
    if not password:
        return await _escalate(
            driver,
            account,
            site,
            verdict,
            steps,
            screenshot_dir,
            gate,
            log,
            reason=f"取不到密码（{credentials.describe(account.account_id)}）。密码不允许写进配置文件。",
        )

    if not await driver.fill(SIGNIN_EMAIL, account.email):
        return await _escalate(
            driver,
            account,
            site,
            verdict,
            steps,
            screenshot_dir,
            gate,
            log,
            reason="找不到邮箱输入框。登录页选择器需要重新校准（见 driver/base.py 的 MARKER_SELECTORS）。",
        )
    steps.append("已填邮箱")
    await driver.fill(SIGNIN_PASSWORD, password)
    steps.append("已填密码")

    if not await driver.click(SIGNIN_SUBMIT):
        return await _escalate(
            driver,
            account,
            site,
            verdict,
            steps,
            screenshot_dir,
            gate,
            log,
            reason="找不到登录提交按钮。需要校准提交选择器。",
        )

    probe = await driver.probe()
    verdict = classify(probe, expected_site=site)
    steps.append(f"提交后→{verdict.state.value}")
    if log is not None:
        log.event("login_submit", state=verdict.state.value, url=probe.url)

    if verdict.state is SessionState.NEEDS_2FA:
        return await _handle_mfa(
            driver, account, site, gate, steps, screenshot_dir, log, timeout_ms, sleeper
        )
    if verdict.ok:
        return LoginOutcome(account.account_id, site.code, verdict.state, verdict, steps)
    if verdict.state.is_gate:
        return await _escalate(driver, account, site, verdict, steps, screenshot_dir, gate, log)

    return await _escalate(
        driver,
        account,
        site,
        verdict,
        steps,
        screenshot_dir,
        gate,
        log,
        reason=f"提交后未进入后台（{verdict.state.value}）。可能密码错、或触发了额外校验。",
    )


async def _handle_mfa(
    driver: DriverLike,
    account: Account,
    site: Site,
    gate: HumanGate,
    steps: list[str],
    screenshot_dir: Path | None,
    log,
    timeout_ms: int,
    sleeper: Callable[[float], Awaitable[None]],
) -> LoginOutcome:
    """处理两步验证：有 TOTP 种子就全自动，否则走人工通道。"""
    code: str | None = None
    source: str | None = None

    if account.totp_seed:
        remaining = seconds_remaining()
        if remaining < _MIN_CODE_SECONDS:
            steps.append(f"验证码仅剩 {remaining:.1f}s，等下一轮")
            await sleeper(remaining + 0.5)
        code = totp(account.totp_seed)
        source = "totp"
        steps.append("用 TOTP 种子生成验证码")
        if log is not None:
            log.event("mfa_totp", seed=mask_seed(account.totp_seed))

    if code is None:
        shot = await _snapshot(driver, screenshot_dir, f"mfa-{account.account_id}-{site.code}")
        request = HumanRequest(
            account_id=account.account_id,
            site_code=site.code,
            state=SessionState.NEEDS_2FA,
            reason="账号未配 TOTP 种子，需要人工提供验证码（短信/邮箱验证码）。",
            screenshot=shot,
        )
        # 只调 provide_code，不额外调 notify：否则终端/机器人会把同一条提示推两遍。
        code = await gate.provide_code(request)
        source = "human" if code else None
        if code is None:
            steps.append("人工未提供验证码")
            verdict = classify(await driver.probe(), expected_site=site)
            return LoginOutcome(
                account.account_id,
                site.code,
                SessionState.NEEDS_2FA,
                verdict,
                steps,
                screenshot=shot,
                human_message=request.render(),
            )
        steps.append("已取得人工验证码")

    if not await driver.fill(MFA_CODE, code):
        probe = await driver.probe()
        verdict = classify(probe, expected_site=site)
        return await _escalate(
            driver,
            account,
            site,
            verdict,
            steps,
            screenshot_dir,
            gate,
            log,
            reason="找不到 OTP 输入框。两步验证页选择器需要校准。",
        )
    await driver.click(MFA_SUBMIT)

    probe = await driver.probe()
    verdict = classify(probe, expected_site=site)
    steps.append(f"提交验证码后→{verdict.state.value}")
    if log is not None:
        log.event("mfa_submit", state=verdict.state.value, source=source)

    if verdict.ok:
        return LoginOutcome(account.account_id, site.code, verdict.state, verdict, steps, code_source=source)
    if verdict.state.is_gate:
        return await _escalate(driver, account, site, verdict, steps, screenshot_dir, gate, log, code_source=source)
    return await _escalate(
        driver,
        account,
        site,
        verdict,
        steps,
        screenshot_dir,
        gate,
        log,
        reason=f"验证码提交后仍未进入后台（{verdict.state.value}）。验证码可能已过期或不被接受。",
        code_source=source,
    )


async def _snapshot(driver: DriverLike, screenshot_dir: Path | None, name: str) -> Path | None:
    if screenshot_dir is None:
        return None
    try:
        return await driver.screenshot(screenshot_dir / f"{name}.png")
    except Exception:
        return None


async def _escalate(
    driver: DriverLike,
    account: Account,
    site: Site,
    verdict: Verdict,
    steps: list[str],
    screenshot_dir: Path | None,
    gate: HumanGate,
    log,
    *,
    reason: str | None = None,
    code_source: str | None = None,
) -> LoginOutcome:
    """上报人工。**绝不重试**——闸门状态下重试是纯负收益。"""
    shot = await _snapshot(driver, screenshot_dir, f"gate-{account.account_id}-{site.code}-{verdict.state.value}")
    text = reason or verdict.human_prompt or f"需要人工介入：{verdict.state.value}"
    request = HumanRequest(account.account_id, site.code, verdict.state, text, shot)
    await gate.notify(request)
    steps.append(f"上报人工：{verdict.state.value}")
    if log is not None:
        log.event("escalate_human", state=verdict.state.value, reason=text, screenshot=str(shot) if shot else None)
    return LoginOutcome(
        account.account_id,
        site.code,
        verdict.state,
        verdict,
        steps,
        code_source=code_source,
        screenshot=shot,
        human_message=request.render(),
    )
