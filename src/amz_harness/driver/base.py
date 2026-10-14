"""驱动层公共契约。

**这是全项目唯一"必须开浏览器才能验证"的边界。**
边界的形状决定了上层能不能离线测试：
驱动只负责把"DOM"翻译成"语义标记 + 内容 + 截图"，
不做任何判断（判断全在 session.classify 里）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Marker, PageProbe

# DOM 选择器 -> 语义标记。
#
# ⚠️ 校准点：下面每个选择器里靠后的那几个（data-marker / #orders-table 之类）
# 是给本地 mock 页面和自建后台用的通用约定。真实亚马逊后台的选择器
# **必须你自己对着页面校准**——DOM 会改，抄来的选择器活不过一个季度。
# 校准方法：把真实页面 HTML 存下来，跑
#     python -m amz_harness probe --html page.html --site US
# 走一遍，看每个标记命中了没有。
MARKER_SELECTORS: dict[Marker, str] = {
    Marker.SIGNIN_FORM: "form[name='signIn'], #ap_email, input[name='email'], [data-marker='signin-form']",
    Marker.OTP_INPUT: "input[name='otpCode'], #auth-mfa-otpcode, input[autocomplete='one-time-code'], [data-marker='otp']",
    Marker.CAPTCHA: "#captchacharacters, img[src*='captcha'], form[action*='validateCaptcha'], [data-marker='captcha']",
    Marker.ACCOUNT_LOCKED: "[data-marker='account-locked'], #account-locked",
    Marker.SESSION_EXPIRED: "[data-marker='session-expired'], #session-expired",
    Marker.MKT_SWITCHER: "#sc-mkt-switcher, [data-marker='mkt-switcher'], #marketplace-switcher",
    Marker.SELLER_NAV: "#sc-navbar, nav.seller-nav, [data-marker='seller-nav']",
    Marker.HEALTH_ALERT: "[data-marker='health-alert'], #account-health-alert",
    Marker.UNUSUAL_ACTIVITY: "[data-marker='unusual-activity']",
    Marker.ORDERS_TABLE: "#orders-table, [data-marker='orders-table']",
    Marker.INVENTORY_TABLE: "#inventory-table, [data-marker='inventory-table']",
}

# 登录表单选择器。前三个是亚马逊登录页上长期稳定的 id。
SIGNIN_EMAIL = "#ap_email, input[name='email']"
SIGNIN_PASSWORD = "#ap_password, input[name='password']"
SIGNIN_SUBMIT = "#signInSubmit, button[type='submit']"
MFA_CODE = "#auth-mfa-otpcode, input[name='otpCode']"
MFA_SUBMIT = "#auth-signin-button, button[type='submit']"


class DriverError(RuntimeError):
    """驱动层不可恢复的错误（浏览器起不来、页面打不开等）。"""


@runtime_checkable
class DriverLike(Protocol):
    """采集/流程层依赖的最小接口。"""

    async def start(self) -> None: ...

    async def goto(self, url: str, *, timeout_ms: int = ...) -> PageProbe: ...

    async def probe(self) -> PageProbe: ...

    async def content(self) -> str: ...

    async def fill(self, selector: str, value: str) -> bool: ...

    async def click(self, selector: str) -> bool: ...

    async def click_text(self, text: str, *, exact: bool = False) -> bool: ...

    async def screenshot(self, path: Path) -> Path: ...

    async def close(self) -> None: ...
