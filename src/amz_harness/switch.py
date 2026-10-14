"""站点切换 + **切换后校验**。

只有"切过去了"不算成功，"切过去且确实是目标站点"才算。
后者靠两件事验证：
  1. 落到目标域名（host 比对）
  2. 仍然是已登录态

第二条经常被忽略：切站点时亚马逊有时会要求重新认证。
如果只检查 URL 就认为成功，后面采集会拿到一堆登录页，还查不出原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .driver.base import DriverLike
from .models import Flag, SessionState
from .session import Verdict, classify
from .sites import Site

# dropdown 模式下尝试用来定位目标站点的候选项文本。
# ⚠️ 校准点：真实后台的下拉项文字需要你自己看一遍。这里只用"机器能确定的东西"
# （站点码、域名）打头，绝不去猜后台界面上的中文/德文菜单名。
DROPDOWN_TEXT_CANDIDATES = ("code", "domain", "label")


@dataclass(frozen=True, slots=True)
class SwitchOutcome:
    from_site: str
    to_site: str
    ok: bool
    verdict: Verdict
    note: str = ""
    steps: tuple[str, ...] = ()

    @property
    def requires_relogin(self) -> bool:
        return self.verdict.state in (SessionState.LOGGED_OUT, SessionState.SESSION_EXPIRED)

    @property
    def needs_human(self) -> bool:
        return self.verdict.state.is_gate

    def render(self) -> str:
        arrow = f"{self.from_site}→{self.to_site}" if self.from_site else self.to_site
        mark = "OK" if self.ok else "FAIL"
        tail = f" ({self.note})" if self.note else ""
        return f"切换 {arrow} {mark} {self.verdict.state.value}{tail}"


async def switch_to(
    driver: DriverLike,
    target: Site,
    *,
    from_site: str = "",
    mode: str = "navigate",
    selector: str = "",
    log=None,
    timeout_ms: int = 30_000,
) -> SwitchOutcome:
    steps: list[str] = []

    if mode == "navigate":
        # 每个 marketplace 有自己独立的 sellercentral 域名，"直接换域名"
        # 是最稳也最容易验证的一条路。
        probe = await driver.goto(target.home_url, timeout_ms=timeout_ms)
        steps.append(f"导航到 {target.home_url}")
    elif mode == "dropdown":
        if not selector:
            raise ValueError("mode=dropdown 必须提供 site_switcher.selector")
        if not await driver.click(selector):
            return SwitchOutcome(
                from_site,
                target.code,
                False,
                Verdict(
                    SessionState.UNKNOWN,
                    0.5,
                    (f"打不开站点切换器：{selector}",),
                ),
                note="切换器选择器需要校准",
                steps=("点击切换器失败",),
            )
        steps.append(f"已点开切换器 {selector}")
        clicked = None
        for field_name in DROPDOWN_TEXT_CANDIDATES:
            candidate = getattr(target, field_name)
            if candidate and await driver.click_text(str(candidate)):
                clicked = str(candidate)
                break
        if clicked is None:
            return SwitchOutcome(
                from_site,
                target.code,
                False,
                Verdict(
                    SessionState.UNKNOWN,
                    0.5,
                    ("下拉里没找到目标站点",),
                ),
                note="下拉项文本需要校准",
                steps=tuple(steps),
            )
        steps.append(f"点选 {clicked!r}")
        probe = await driver.probe()
    else:
        raise ValueError(f"未知切换模式 {mode!r}；只能是 navigate 或 dropdown")

    verdict = classify(probe, expected_site=target)
    steps.append(f"校验→{verdict.state.value}")

    ok = verdict.ok and Flag.UNEXPECTED_DOMAIN not in verdict.flags
    note = ""
    if verdict.ok and Flag.UNEXPECTED_DOMAIN in verdict.flags:
        note = f"落到了非目标域名 {probe.netloc or probe.host}，期望 {target.domain}"
    elif verdict.state.is_gate:
        note = "触发闸门，停止自动重试，转人工"
    elif verdict.state in (SessionState.LOGGED_OUT, SessionState.SESSION_EXPIRED):
        note = "会话失效，需要重新登录"

    if log is not None:
        log.event(
            "switch_site",
            to=target.code,
            ok=ok,
            state=verdict.state.value,
            url=probe.url,
            note=note or None,
        )

    return SwitchOutcome(from_site, target.code, ok, verdict, note=note, steps=tuple(steps))


@dataclass(slots=True)
class RotationPlan:
    """轮询顺序计划器：按站点序列循环，并记住上次停在哪。

    为什么要记住位置：每次轮询都从头开始，会让排在前面的站点被打得更频繁，
    长期下来这些小差异会累积成风控画像。
    """

    sites: tuple[str, ...]
    cursor: int = field(default=0)
    completed: int = field(default=0)

    def next_site(self) -> str:
        if not self.sites:
            raise ValueError("RotationPlan 没有任何站点")
        site = self.sites[self.cursor]
        self.cursor += 1
        if self.cursor >= len(self.sites):
            self.cursor = 0
            self.completed += 1
        return site

    def peek(self) -> str:
        if not self.sites:
            raise ValueError("RotationPlan 没有任何站点")
        return self.sites[self.cursor]

    @property
    def rounds(self) -> int:
        """已经完整走完几轮。"""
        return self.completed
