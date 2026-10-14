"""数据模型。全部是 dataclass，没有逻辑依赖，方便单测和序列化。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import unquote, urlsplit

# 账号/环境标识只允许安全字符：它会被拼进文件系统路径，必须挡路径穿越。
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SessionState(str, Enum):
    """会话状态。注意 ACCOUNT_ALERT 不在这里——它是与登录态正交的"标记"。

    把"登录是否有效"和"账号是否有风险提示"塞进同一个枚举，是这类项目最常见
    的建模错误：登录成功但账号健康告警（比如 listing 被下架）会因此被漏报。
    """

    UNKNOWN = "unknown"
    LOGGED_OUT = "logged_out"
    NEEDS_2FA = "needs_2fa"
    NEEDS_CAPTCHA = "needs_captcha"
    LOGGED_IN = "logged_in"
    SESSION_EXPIRED = "session_expired"
    BLOCKED = "blocked"
    NETWORK_ERROR = "network_error"

    @property
    def is_gate(self) -> bool:
        """硬闸门：需要人介入，**任何情况下都不许自动重试**。

        这条是整个骨架里最重要的一条规则。对验证码/2FA/账号锁定盲目重试，
        正是把"一次验证"升级成"账号被挂起"的经典方式。
        """
        return self in (SessionState.NEEDS_2FA, SessionState.NEEDS_CAPTCHA, SessionState.BLOCKED)

    @property
    def is_live(self) -> bool:
        return self is SessionState.LOGGED_IN


class Flag(str, Enum):
    """与状态正交的标记位，可多个同时出现。"""

    ACCOUNT_ALERT = "account_alert"
    UNUSUAL_ACTIVITY = "unusual_activity"
    UNEXPECTED_DOMAIN = "unexpected_domain"
    PARTIAL_RENDER = "partial_render"
    NO_PROXY = "no_proxy"


class Marker(str, Enum):
    """页面语义标记。

    这是驱动层和判定层之间的**唯一契约**：驱动负责把 DOM 翻译成标记，
    判定层只消费标记。好处是状态机能用纯字符串离线单测，不需要起浏览器。
    """

    SIGNIN_FORM = "signin_form"
    OTP_INPUT = "otp_input"
    CAPTCHA = "captcha"
    ACCOUNT_LOCKED = "account_locked"
    SESSION_EXPIRED = "session_expired"
    MKT_SWITCHER = "mkt_switcher"
    SELLER_NAV = "seller_nav"
    HEALTH_ALERT = "health_alert"
    UNUSUAL_ACTIVITY = "unusual_activity"
    ORDERS_TABLE = "orders_table"
    INVENTORY_TABLE = "inventory_table"


@dataclass(frozen=True, slots=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    label: str | None = None

    @classmethod
    def parse(cls, raw: str, *, label: str | None = None) -> "Proxy":
        """接受 'http://user:pass@host:8080' 或 'host:8080'。"""
        text = raw.strip()
        if "://" not in text:
            text = f"http://{text}"
        parts = urlsplit(text)
        if not parts.hostname or not parts.port:
            raise ValueError(f"代理地址缺少 host 或 port：{raw!r}")
        return cls(
            scheme=(parts.scheme or "http").lower(),
            host=parts.hostname,
            port=int(parts.port),
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
            label=label,
        )

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def display(self) -> str:
        """可安全写进日志的形态（不含账密）。"""
        who = f"{self.username}@" if self.username else ""
        tag = f" [{self.label}]" if self.label else ""
        return f"{self.scheme}://{who}{self.host}:{self.port}{tag}"

    def __repr__(self) -> str:  # 防止代理密码随异常栈泄进日志
        return f"Proxy({self.display})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    email: str
    home_site: str
    proxy: Proxy | None
    sites: tuple[str, ...]
    profile_label: str = ""
    totp_seed: str | None = None
    headless: bool = False
    user_agent: str | None = None
    min_seconds_between_jobs: int = 8
    max_actions_per_hour: int = 120
    notes: str = ""

    def validated(self) -> "Account":
        if not _SAFE_ID.match(self.account_id):
            raise ValueError(
                f"account_id {self.account_id!r} 不合法：只允许字母数字和 . _ - ，"
                "长度 1-64，且不能以符号开头（它会被用作 profile 目录名）"
            )
        if not self.sites:
            raise ValueError(f"账号 {self.account_id} 未配置任何站点")
        if self.min_seconds_between_jobs < 0:
            raise ValueError("min_seconds_between_jobs 不能为负")
        if self.max_actions_per_hour <= 0:
            raise ValueError("max_actions_per_hour 必须为正")
        return self

    @property
    def profile_dir_name(self) -> str:
        return f"{self.account_id}"


@dataclass(slots=True)
class PageProbe:
    """一次页面观测的快照——判定层的唯一输入。"""

    url: str = ""
    title: str = ""
    http_status: int | None = None
    markers: frozenset[Marker] = frozenset()
    selector_hits: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    text_excerpt: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def netloc(self) -> str:
        """host 可能带端口。域名比对要用 netloc，否则本地 mock（127.0.0.1:PORT）
        会被误判成"落到了别的域名"。"""
        return urlsplit(self.url).netloc or ""

    def has(self, *markers: Marker) -> bool:
        return all(m in self.markers for m in markers)

    def any_of(self, *markers: Marker) -> bool:
        return any(m in self.markers for m in markers)
