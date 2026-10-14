"""Playwright 驱动：真的开浏览器。

为什么选 Playwright 而不是 Selenium（给面试/汇报用的一段话）：

  · 自动等待：Selenium 里满屏的 WebDriverWait 在这里是默认行为，
    少写一半代码，也少一半"偶发失败"。
  · `launch_persistent_context`：一个账号一个 profile 目录，
    登录态和"信任此设备"标记天然持久化。
  · trace viewer：出事能把整个会话录下来回放（含网络和 DOM 快照）。
    这是排障效率的量级差异，不是锦上添花。
  · 多 context + CDP：要接指纹浏览器时，`connect_over_cdp` 直接连上去，
    不用自己维护指纹补丁。

关于反检测的诚实说明：
  这个文件里的 `--disable-blink-features=AutomationControlled` 只是**降低明显的
  automation 特征**，不等于隐身。真正决定触发率的是：出口 IP 质量、
  一账号一 profile 的持久化、以及**别打得太快**（后两条由 profiles/scheduler 保证）。
  需要更强的指纹隔离时，正确做法是接 AdsPower/比特这类指纹浏览器，
  用 `connect_over_cdp` 连上去，而不是在这里无限堆 flag。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Marker, PageProbe
from ..profiles import EnvironmentSpec
from .base import MARKER_SELECTORS, DriverError

# 视作"被挡住"的 HTTP 状态码。
# 取舍说明：把 401/403 判成 BLOCKED 会让一次偶发 403 就升级到人工确认。
# 这是**故意偏保守**——在账号安全这件事上，宁可多打扰人一次，
# 也不要对着一个被挡的会话继续刷。觉得吵就把 403 从集合里拿掉。
BLOCKING_HTTP = frozenset({401, 403})


class PlaywrightDriver:
    def __init__(
        self,
        spec: EnvironmentSpec,
        *,
        channel: str | None = None,
        default_timeout_ms: int = 30_000,
        trace_path: Path | None = None,
        log=None,
    ) -> None:
        self.spec = spec
        self.channel = channel
        self.default_timeout_ms = default_timeout_ms
        self.trace_path = trace_path
        self.log = log
        self._pw = None
        self._context = None
        self._page = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - 环境问题，不是逻辑问题
            raise DriverError(
                "没装 playwright。装法：\n"
                "  python -m venv .venv\n"
                "  .venv\\Scripts\\pip install -i https://pypi.org/simple playwright\n"
                "  .venv\\Scripts\\python -m playwright install chromium\n"
                "注意别用默认的 pip 源——某些镜像（如清华源）会返回 403 导致装不上。"
            ) from exc

        self._pw = await async_playwright().start()
        kwargs: dict = {
            "user_data_dir": str(self.spec.profile_dir),
            "headless": self.spec.headless,
            "locale": self.spec.locale,
            "timezone_id": self.spec.timezone,
            "viewport": {"width": self.spec.viewport[0], "height": self.spec.viewport[1]},
            "accept_downloads": False,
            "args": list(self.spec.launch_args),
            "ignore_default_args": ["--enable-automation"],
        }
        if self.spec.user_agent:
            kwargs["user_agent"] = self.spec.user_agent
        proxy = self.spec.playwright_proxy()
        if proxy:
            kwargs["proxy"] = proxy
        if self.channel:
            kwargs["channel"] = self.channel

        try:
            self._context = await self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            await self._shutdown_playwright()
            raise DriverError(f"启动浏览器失败（profile={self.spec.profile_dir}）：{exc}") from exc

        self._context.set_default_timeout(self.default_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            await self._context.tracing.start(screenshots=True, snapshots=True)

    async def close(self) -> None:
        try:
            if self.trace_path and self._context is not None:
                try:
                    await self._context.tracing.stop(path=str(self.trace_path))
                except Exception:  # trace 失败不该影响关闭
                    pass
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            self._page = None
            await self._shutdown_playwright()

    async def _shutdown_playwright(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            finally:
                self._pw = None

    async def __aenter__(self) -> "PlaywrightDriver":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ---- 观测 ----

    async def goto(self, url: str, *, timeout_ms: int | None = None) -> PageProbe:
        page = self._require_page()
        wait_ms = timeout_ms or self.default_timeout_ms
        status: int | None = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=wait_ms)
            if response is not None:
                status = response.status
        except Exception as exc:
            # 驱动层不抛异常给上层：导航失败也是一种"观测结果"，
            # 交给状态机判成 NETWORK_ERROR，上层逻辑就只有一条路径。
            return PageProbe(url=url, error=f"导航失败 {type(exc).__name__}: {exc}")

        probe = await self.probe()
        probe.http_status = status
        if status in BLOCKING_HTTP:
            probe.markers = frozenset(probe.markers | {Marker.ACCOUNT_LOCKED})
        return probe

    async def probe(self) -> PageProbe:
        page = self._require_page()
        try:
            url = page.url
        except Exception as exc:
            return PageProbe(error=f"读取页面 URL 失败：{exc}")

        try:
            title = await page.title()
        except Exception:
            title = ""

        markers: set[Marker] = set()
        hits: dict[str, int] = {}
        for marker, selector in MARKER_SELECTORS.items():
            try:
                count = await page.locator(selector).count()
            except Exception:
                count = 0
            hits[selector] = count
            if count > 0:
                markers.add(marker)

        return PageProbe(
            url=url,
            title=title,
            markers=frozenset(markers),
            selector_hits=hits,
        )

    async def content(self) -> str:
        return await self._require_page().content()

    # ---- 交互 ----

    async def fill(self, selector: str, value: str) -> bool:
        locator = self._require_page().locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=self.default_timeout_ms)
            await locator.fill(value)
            return True
        except Exception:
            return False

    async def click(self, selector: str) -> bool:
        page = self._require_page()
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=self.default_timeout_ms)
            await locator.click()
            await self._settle(page)
            return True
        except Exception:
            return False

    async def click_text(self, text: str, *, exact: bool = False) -> bool:
        page = self._require_page()
        locator = page.get_by_text(text, exact=exact).first
        try:
            await locator.wait_for(state="visible", timeout=self.default_timeout_ms)
            await locator.click()
            await self._settle(page)
            return True
        except Exception:
            return False

    @staticmethod
    async def _settle(page) -> None:
        """点击后等页面稳定。

        Playwright 的 click 本身会等它触发的那次导航，但表单提交/前端路由
        的时序并不总是干净。这里补一次**有超时上限**的兜底等待：
        等不到就继续，绝不无限期挂住——轮询任务最怕的就是卡死。
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass

    async def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._require_page().screenshot(path=str(path))
        return path

    # ---- 内部 ----

    @property
    def page(self):
        """逃生通道：正常流程请走上面的方法；写测试/一次性脚本时可以直接用。"""
        return self._require_page()

    @property
    def context(self):
        if self._context is None:
            raise DriverError("驱动还没 start()，或者已经 close() 了")
        return self._context

    def _require_page(self):
        if self._page is None:
            raise DriverError("驱动还没 start()，或者已经 close() 了")
        return self._page
