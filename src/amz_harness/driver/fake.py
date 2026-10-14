"""FakeDriver：离线驱动。

存在的理由很实际——**真实站点联调没法进 CI**。有了它，状态机、登录流程、
站点切换、采集管线的**编排正确性**可以在毫秒级被反复验证，
真机联调时就只剩"选择器对不对"这一个变量。

如果连编排逻辑都要靠开浏览器验，那你每次改代码都得等人工，效率会崩。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Marker, PageProbe


@dataclass
class FakePage:
    url: str
    title: str = ""
    http_status: int | None = 200
    markers: frozenset[Marker] = frozenset()
    html: str = ""
    error: str | None = None

    def to_probe(self) -> PageProbe:
        return PageProbe(
            url=self.url,
            title=self.title,
            http_status=self.http_status,
            markers=frozenset(self.markers),
            error=self.error,
        )


@dataclass
class FakeDriver:
    """按"URL -> 页面"剧本驱动的假浏览器。

    transitions: {(动作, 选择器): 目标URL}
        模拟"点了提交之后跳到哪一页"。写成显式映射而不是隐式猜测，
        是为了让测试一眼能看懂流程走向。
    """

    pages: dict[str, FakePage] = field(default_factory=dict)
    transitions: dict[tuple[str, str], str] = field(default_factory=dict)
    default: FakePage | None = None
    current: FakePage | None = None
    visits: list[str] = field(default_factory=list)
    actions: list[tuple[str, ...]] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    # 让某些选择器"找不到"，用来测"选择器失效"这条分支
    fail_selectors: set[str] = field(default_factory=set)
    closed: bool = False
    started: bool = False
    _forced: dict[str, deque[FakePage]] = field(default_factory=lambda: defaultdict(deque))

    # ---- 剧本编排辅助 ----

    def force(self, url: str, *pages: FakePage) -> "FakeDriver":
        """给某个 URL 排队若干"这次访问看到哪一页"，按顺序消耗。

        用来模拟"第一次访问会话过期，重新登录后就好了"这类时序。
        """
        self._forced[url].extend(pages)
        return self

    def on(self, action: str, selector: str, target_url: str) -> "FakeDriver":
        self.transitions[(action, selector)] = target_url
        return self

    def _resolve(self, url: str) -> FakePage:
        queue = self._forced.get(url)
        if queue:
            return queue.popleft()
        if url in self.pages:
            return self.pages[url]
        # 最长前缀匹配，方便一个前缀覆盖一堆带参 URL
        matches = [prefix for prefix in self.pages if url.startswith(prefix)]
        if matches:
            return self.pages[max(matches, key=len)]
        if self.default is not None:
            return self.default
        return FakePage(url=url, error=f"FakeDriver 没有为 {url!r} 配置任何页面")

    def _follow(self, action: str, key: str) -> None:
        target = self.transitions.get((action, key))
        if target:
            self.current = self._resolve(target)

    # ---- DriverLike 实现 ----

    async def start(self) -> None:
        self.started = True

    async def goto(self, url: str, *, timeout_ms: int = 30_000) -> PageProbe:
        self.visits.append(url)
        self.current = self._resolve(url)
        return self.current.to_probe()

    async def probe(self) -> PageProbe:
        if self.current is None:
            return PageProbe(error="尚未导航到任何页面")
        return self.current.to_probe()

    async def content(self) -> str:
        return self.current.html if self.current else ""

    async def fill(self, selector: str, value: str) -> bool:
        self.actions.append(("fill", selector))
        if selector in self.fail_selectors:
            return False
        self.filled[selector] = value
        self._follow("fill", selector)
        return True

    async def click(self, selector: str) -> bool:
        self.actions.append(("click", selector))
        if selector in self.fail_selectors:
            return False
        self._follow("click", selector)
        return True

    async def click_text(self, text: str, *, exact: bool = False) -> bool:
        self.actions.append(("click_text", text))
        self._follow("click_text", text)
        return True

    async def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")  # 占位，只需要"文件存在"
        self.screenshots.append(path)
        return path

    async def close(self) -> None:
        self.closed = True
