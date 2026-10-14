"""环境启动：给每个账号造一套**互相隔离且长期稳定**的浏览器环境。

两条容易被忽略的规则，这里刻意做成硬约束：

  R1. 指纹锚定在"账号归属站点"，不是"当前要访问的站点"。
      同一个账号从 US 切到 DE 时，locale/timezone **不许跟着变**。
      一个"美国时区、美国语言"的会话突然变成德语区，本身就是异常信号。

  R2. 账号没配代理 → 直接拒绝启动，不做静默降级。
      多账号共用一个出口 IP 是这类项目出事的头号原因。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import Account, Flag, Proxy
from .sites import Site, get_site

# 只从固定几个常见分辨率里选，避免出现"1920x1080 之外还有 1913x1078"这种
# 一眼假的视口尺寸。
_VIEWPORTS: tuple[tuple[int, int], ...] = ((1920, 1080), (1536, 864), (1600, 900), (1366, 768))

# 常规的"别太明显"启动参数。不碰 --user-agent 之类的指纹伪装，
# 需要伪装时请走指纹浏览器（connect_over_cdp），不要在这里硬堆 flag。
_BASE_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints",
    "--password-store=basic",
)

MANIFEST_NAME = "environment.json"


def _stable_pick(account_id: str, options: tuple, salt: str):
    """由 account_id 稳定地派生一个选择。

    必须用 hashlib，**不能用内置 hash()**——CPython 的 str hash 带进程随机盐，
    换个进程就变，会造成"同一个账号每次启动视口都不一样"。
    这是个真会咬人的坑。
    """
    digest = hashlib.sha256(f"{account_id}:{salt}".encode()).digest()
    return options[int.from_bytes(digest[:4], "big") % len(options)]


def accept_language(locale: str) -> str:
    """'de-DE' -> 'de-DE,de;q=0.9,en;q=0.8'"""
    lang = locale.split("-")[0]
    if lang == "en":
        return f"{locale},en;q=0.9"
    return f"{locale},{lang};q=0.9,en;q=0.8"


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    account_id: str
    profile_dir: Path
    locale: str
    timezone: str
    accept_language: str
    viewport: tuple[int, int]
    launch_args: tuple[str, ...]
    headless: bool
    user_agent: str | None = None
    proxy: Proxy | None = None
    home_site: str = ""

    @property
    def fingerprint(self) -> dict:
        """会被持久化下来、用于检测"指纹漂移"的那部分字段。"""
        return {
            "locale": self.locale,
            "timezone": self.timezone,
            "accept_language": self.accept_language,
            "viewport": list(self.viewport),
            "user_agent": self.user_agent,
        }

    def playwright_proxy(self) -> dict | None:
        if self.proxy is None:
            return None
        payload = {"server": self.proxy.server}
        if self.proxy.username:
            payload["username"] = self.proxy.username
        if self.proxy.password:
            payload["password"] = self.proxy.password
        return payload


@dataclass(frozen=True, slots=True)
class EnvironmentIssue:
    level: str  # "error" | "warn"
    code: str
    message: str


@dataclass(slots=True)
class EnvironmentReport:
    spec: EnvironmentSpec
    created: bool
    drift: tuple[EnvironmentIssue, ...] = ()
    blockings: tuple[EnvironmentIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockings


def resolve_profile_dir(root: Path, name: str) -> Path:
    """把 profile 目录限制在 root 之内。

    account_id 来自配置文件，会被拼进路径。没有这道校验，一条
    `account_id = "../../../Windows/System32"` 就能写到工作区外面去。
    """
    base = root.resolve()
    candidate = (base / name).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"profile 目录逃出根目录：{candidate} 不在 {base} 之下")
    return candidate


def build_environment(
    account: Account,
    *,
    root: Path,
    require_proxy: bool = True,
) -> tuple[EnvironmentSpec, tuple[EnvironmentIssue, ...]]:
    """构造一个账号的环境规格。返回 (规格, 阻断项)。"""
    account.validated()
    issues: list[EnvironmentIssue] = []

    if require_proxy and account.proxy is None:
        issues.append(
            EnvironmentIssue(
                "error",
                "missing_proxy",
                f"账号 {account.account_id} 未配置代理。多账号共用出口 IP 是头号风控诱因，"
                "这里直接拒绝启动（确实要单账号本机调试，用 --allow-no-proxy 显式放行）。",
            )
        )

    home: Site = get_site(account.home_site)
    viewport = _stable_pick(account.account_id, _VIEWPORTS, "viewport")
    profile_dir = resolve_profile_dir(root, account.profile_dir_name)

    spec = EnvironmentSpec(
        account_id=account.account_id,
        profile_dir=profile_dir,
        locale=home.locale,
        timezone=home.timezone,
        accept_language=accept_language(home.locale),
        viewport=viewport,
        launch_args=_BASE_ARGS,
        headless=account.headless,
        user_agent=account.user_agent,
        proxy=account.proxy,
        home_site=home.code,
    )
    return spec, tuple(issues)


def materialize(spec: EnvironmentSpec) -> EnvironmentReport:
    """落盘：建目录，写/比对环境清单，检出指纹漂移。"""
    created = not spec.profile_dir.exists()
    spec.profile_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = spec.profile_dir / MANIFEST_NAME
    previous: dict | None = None
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    drift: list[EnvironmentIssue] = []
    if previous:
        old_fp = previous.get("fingerprint", {})
        new_fp = spec.fingerprint
        for key in ("locale", "timezone", "viewport", "user_agent"):
            if old_fp.get(key) != new_fp.get(key):
                drift.append(
                    EnvironmentIssue(
                        "warn",
                        "fingerprint_drift",
                        f"{key} 变了：{old_fp.get(key)!r} -> {new_fp.get(key)!r}。"
                        "同一账号的指纹突变本身就是异常信号，确认这是有意为之。",
                    )
                )
        old_proxy = (previous.get("proxy") or {}).get("server")
        new_proxy = spec.proxy.server if spec.proxy else None
        if old_proxy != new_proxy:
            drift.append(
                EnvironmentIssue(
                    "warn",
                    "proxy_drift",
                    f"出口代理变了：{old_proxy} -> {new_proxy}。IP 跳变会显著提高验证概率。",
                )
            )

    payload = {
        "account_id": spec.account_id,
        "home_site": spec.home_site,
        "fingerprint": spec.fingerprint,
        "proxy": {"server": spec.proxy.server, "label": spec.proxy.label} if spec.proxy else None,
        # 注意：清单里**不写**代理账密，只记 server 用于漂移比对。
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    return EnvironmentReport(spec=spec, created=created, drift=tuple(drift))


def environment_flags(spec: EnvironmentSpec) -> frozenset[Flag]:
    return frozenset() if spec.proxy else frozenset({Flag.NO_PROXY})


def spec_to_dict(spec: EnvironmentSpec) -> dict:
    data = asdict(spec)
    data["profile_dir"] = str(spec.profile_dir)
    data["viewport"] = list(spec.viewport)
    data["launch_args"] = list(spec.launch_args)
    data["proxy"] = spec.proxy.display if spec.proxy else None
    return data
