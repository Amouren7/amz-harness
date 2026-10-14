"""会话健康状态机。

这是整个骨架里最该被认真写、也最容易被写坏的一块。

三个刻意的设计决定：

1. **判定是纯函数。** `classify(probe)` 只吃一个页面快照，不碰浏览器、不碰网络。
   所以它能被离线穷举测试——而真实站点联调是没法在 CI 里跑的。

2. **硬闸门永不自动重试。** 验证码 / 2FA / 账号锁定，重试只会让情况变坏。
   盲目重试是把"一次验证"升级成"账号被挂起"的经典路径。

3. **"登录态"和"账号风险提示"分开建模。** 登录成功 + 账号健康告警
   （listing 被下架、绩效通知）必须能同时成立，否则会漏报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Flag, Marker, PageProbe, SessionState
from .sites import Site

# 这些 HTTP 码在亚马逊后台语境的含义是"对方暂时不想理你"，不是"页面坏了"。
# 当成网络错误处理：退避重开环境，而不是硬刷。
_TRANSIENT_HTTP = frozenset({0, 408, 429, 500, 502, 503, 504})

_GATE_PROMPT = {
    SessionState.NEEDS_2FA: "需要两步验证码（OTP）。请把收到的验证码交给调度器后继续。",
    SessionState.NEEDS_CAPTCHA: "出现图形验证码。必须人工在浏览器里通过，禁止自动重试。",
    SessionState.BLOCKED: "账号疑似被锁定/受限。停止自动操作，先人工确认账号状态。",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    state: SessionState
    confidence: float
    evidence: tuple[str, ...]
    flags: frozenset[Flag] = frozenset()
    expected_host: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is SessionState.LOGGED_IN

    @property
    def needs_human(self) -> bool:
        return self.state.is_gate

    @property
    def human_prompt(self) -> str | None:
        prompt = _GATE_PROMPT.get(self.state)
        if prompt and self.expected_host:
            return f"[{self.expected_host}] {prompt}"
        return prompt

    @property
    def summary(self) -> str:
        extra = ""
        if self.flags:
            extra = " 标记=" + ",".join(sorted(f.value for f in self.flags))
        return f"{self.state.value}(conf={self.confidence:.2f}){extra} :: {' | '.join(self.evidence)}"


def classify(probe: PageProbe, *, expected_site: Site | None = None) -> Verdict:
    """把一个页面快照判成一个状态。

    判定顺序 = 优先级。硬闸门排在登录标记前面：验证码/2FA 是以浮层形式出现
    在已登录页面之上的，如果先判 SELLER_NAV 就会把闸门漏掉。
    """
    evidence: list[str] = []
    flags: set[Flag] = set()
    expected_host = expected_site.domain if expected_site else None

    if probe.error:
        return Verdict(
            SessionState.NETWORK_ERROR, 0.9, (f"驱动报错: {probe.error}",), frozenset(flags), expected_host
        )
    if probe.http_status is not None and probe.http_status in _TRANSIENT_HTTP:
        return Verdict(
            SessionState.NETWORK_ERROR,
            0.9,
            (f"HTTP {probe.http_status}",),
            frozenset(flags),
            expected_host,
        )

    # ---- 正交标记：先收集，等状态定下来再挂上去 ----
    if Marker.HEALTH_ALERT in probe.markers:
        flags.add(Flag.ACCOUNT_ALERT)
    if Marker.UNUSUAL_ACTIVITY in probe.markers:
        flags.add(Flag.UNUSUAL_ACTIVITY)
    if expected_site:
        actual = probe.netloc or probe.host
        if actual and actual != expected_site.domain:
            flags.add(Flag.UNEXPECTED_DOMAIN)

    # ---- 硬闸门（优先级最高） ----
    if Marker.ACCOUNT_LOCKED in probe.markers:
        return Verdict(SessionState.BLOCKED, 0.95, ("命中 account_locked 标记",), frozenset(flags), expected_host)
    if Marker.CAPTCHA in probe.markers:
        return Verdict(
            SessionState.NEEDS_CAPTCHA, 0.95, ("命中验证码标记",), frozenset(flags), expected_host
        )
    if Marker.OTP_INPUT in probe.markers:
        return Verdict(SessionState.NEEDS_2FA, 0.95, ("命中 OTP 输入框",), frozenset(flags), expected_host)

    if Marker.SESSION_EXPIRED in probe.markers:
        return Verdict(
            SessionState.SESSION_EXPIRED, 0.9, ("命中会话过期标记",), frozenset(flags), expected_host
        )

    # ---- 登录态 ----
    if Marker.SIGNIN_FORM in probe.markers:
        return Verdict(SessionState.LOGGED_OUT, 0.95, ("命中登录表单",), frozenset(flags), expected_host)

    if probe.has(Marker.SELLER_NAV) or probe.has(Marker.MKT_SWITCHER):
        hits = [m.value for m in (Marker.SELLER_NAV, Marker.MKT_SWITCHER) if m in probe.markers]
        conf = 0.9 if probe.has(Marker.SELLER_NAV) else 0.75
        return Verdict(SessionState.LOGGED_IN, conf, (f"命中 {'/'.join(hits)}",), frozenset(flags), expected_host)

    # ---- 退化到 URL 推断（置信度明确压低，别假装确定） ----
    path = probe.url.split("?", 1)[0]
    if "/ap/signin" in path or "/ap/register" in path:
        return Verdict(
            SessionState.LOGGED_OUT, 0.6, ("URL 落在 /ap/signin",), frozenset(flags), expected_host
        )

    flags.add(Flag.PARTIAL_RENDER)
    return Verdict(
        SessionState.UNKNOWN,
        0.3,
        (f"未命中任何标记（url={probe.url or '空'}）",),
        frozenset(flags),
        expected_host,
    )


class RetryAction(str, Enum):
    NONE = "none"  # 当前步原样重试
    RELOGIN = "relogin"  # 重新走登录
    RESTART_ENV = "restart_env"  # 关掉浏览器重开一套干净环境
    ESCALATE_HUMAN = "escalate_human"  # 交给人，停止自动操作


@dataclass
class SessionMachine:
    """跟踪一个账号的状态轨迹，并给出"下一步该怎么办"。

    单独拎出来的理由：重试策略是**有状态**的（连续几次才算真的坏了），
    而状态机的输入是纯的。两者分开，就能既好测又可控。
    """

    account_id: str
    max_relogin_attempts: int = 2
    max_unknown_streak: int = 3
    history: list[Verdict] = field(default_factory=list)
    relogin_attempts: int = 0

    @property
    def current(self) -> Verdict | None:
        return self.history[-1] if self.history else None

    def observe(self, verdict: Verdict) -> Verdict:
        """记录一次观测。状态发生变化时重置重登计数。"""
        previous = self.current
        self.history.append(verdict)
        if previous is not None and previous.state is not verdict.state:
            self.relogin_attempts = 0
        return verdict

    def streak(self, state: SessionState | None = None) -> int:
        """末尾连续相同状态的次数。"""
        if not self.history:
            return 0
        target = state or self.history[-1].state
        count = 0
        for verdict in reversed(self.history):
            if verdict.state is not target:
                break
            count += 1
        return count

    def record_relogin(self) -> None:
        self.relogin_attempts += 1

    def decide(self) -> RetryAction:
        verdict = self.current
        if verdict is None:
            return RetryAction.NONE

        state = verdict.state
        if state is SessionState.LOGGED_IN:
            return RetryAction.NONE
        if state.is_gate:
            return RetryAction.ESCALATE_HUMAN
        if state in (SessionState.SESSION_EXPIRED, SessionState.LOGGED_OUT):
            # 反复重登本身就是风控信号，超过上限就交给人。
            if self.relogin_attempts >= self.max_relogin_attempts:
                return RetryAction.ESCALATE_HUMAN
            return RetryAction.RELOGIN
        if state is SessionState.NETWORK_ERROR:
            return RetryAction.RESTART_ENV if self.streak() >= 2 else RetryAction.NONE
        # UNKNOWN
        return RetryAction.RESTART_ENV if self.streak() >= self.max_unknown_streak else RetryAction.NONE
