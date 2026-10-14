"""配置加载（TOML，Python 3.11+ 标准库 tomllib，零依赖）。

一条安全约定：**代理密码和 TOTP 种子不写进配置文件**，用 ${ENV_VAR} 引用。
配置文件是要进 git 的，代理密码进了 git 就等于泄露。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import Account, Proxy
from .sites import SITES, get_site

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

EXAMPLE_CONFIG = '''\
# amz-harness 配置示例。复制成 config/harness.toml 再改。
#
# 密钥类字段一律用 ${ENV_VAR} 引用，不要直接写明文——
# 这个文件是要进 git 的。

[harness]
profiles_dir = "profiles"      # 每个账号一个持久化 profile 目录落在这里
runs_dir = "runs"              # 每次运行的日志/截图/trace 落在这里
global_concurrency = 3         # 全局同时在跑的浏览器环境数上限
headless = false               # 首次登录务必 false；跑顺了再考虑 true
require_proxy = true           # 多账号必须一账号一 IP

[site_switcher]
# navigate = 直接换域名到目标站点（默认，稳、可验证）
# dropdown = 点后台右上角的 marketplace 下拉菜单（选择器必须自己校准）
mode = "navigate"
selector = ""

# ---------------------------------------------------------------
# 账号。TOML 表名就是 account_id，会用作 profile 目录名，
# 只能用字母数字和 . _ -（会做路径穿越校验）。
# ---------------------------------------------------------------
[accounts.shop_us_main]
email = "ops@example.com"
home_site = "US"                       # 指纹锚点：locale/时区按这个站点定，且长期不变
sites = ["US", "CA"]                   # 这个账号要轮询的站点
profile_label = "主店-美国"
headless = false
min_seconds_between_jobs = 10          # 同账号两次操作的最小间隔
max_actions_per_hour = 90              # 同账号每小时操作上限
proxy = "http://user:pass@gate.example.com:8000"
proxy_label = "residential-us-01"
# 2FA 走 authenticator（有种子就能全自动）；短信/邮箱 OTP 请留空走人工兜底
totp_seed = "${AMZ_TOTP_SHOP_US_MAIN}"
notes = "主账号，操作额度优先留给它"

[accounts.shop_eu_de]
email = "eu-ops@example.com"
home_site = "DE"
sites = ["DE", "FR"]
profile_label = "欧洲店-德国"
min_seconds_between_jobs = 20          # 新账号/低权重账号放慢
max_actions_per_hour = 40
proxy = "${AMZ_PROXY_SHOP_EU_DE}"      # 整串代理地址也可以来自环境变量
proxy_label = "residential-de-01"
'''


class ConfigError(Exception):
    """配置错误。信息里一定带上"哪一项、为什么、怎么改"。"""


@dataclass(frozen=True, slots=True)
class SiteSwitcherConfig:
    mode: str = "navigate"
    selector: str = ""


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    path: Path
    root: Path
    profiles_dir: Path
    runs_dir: Path
    accounts: dict[str, Account]
    global_concurrency: int = 3
    headless: bool = False
    require_proxy: bool = True
    site_switcher: SiteSwitcherConfig = SiteSwitcherConfig()

    def account(self, account_id: str) -> Account:
        try:
            return self.accounts[account_id]
        except KeyError:
            known = ", ".join(sorted(self.accounts)) or "(没有配置任何账号)"
            raise ConfigError(f"未知账号 {account_id!r}；已配置：{known}") from None

    def routing(self) -> list[tuple[str, str]]:
        """展开成 (account_id, site_code) 全部组合——轮询的作业面。"""
        return [(aid, code) for aid, acc in self.accounts.items() for code in acc.sites]


def _expand(value: str, *, where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"{where} 引用了未设置的环境变量 ${{{name}}}。"
                "密钥类字段应通过环境变量注入，而不是写死在配置文件里。"
            )
        return os.environ[name]

    return _ENV_REF.sub(replace, value)


def _as_str(data: dict, key: str, *, where: str, default: str | None = None) -> str:
    if key not in data:
        if default is None:
            raise ConfigError(f"{where} 缺少必填项 {key!r}")
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} 必须是字符串，实际是 {type(value).__name__}")
    return _expand(value, where=f"{where}.{key}")


def _as_bool(data: dict, key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是 true/false，实际是 {value!r}")
    return value


def _as_int(data: dict, key: str, *, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数，实际是 {value!r}")
    return value


def _parse_account(account_id: str, raw: object) -> Account:
    where = f"accounts.{account_id}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是 TOML 表（[accounts.{account_id}]）")

    email = _as_str(raw, "email", where=where).strip()
    if not email or "@" not in email:
        raise ConfigError(f"{where}.email 不是合法邮箱：{email!r}")

    home_site = _as_str(raw, "home_site", where=where).strip().upper()
    if home_site not in SITES:
        raise ConfigError(f"{where}.home_site={home_site!r} 不是已知站点；可用：{', '.join(sorted(SITES))}")

    raw_sites = raw.get("sites")
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ConfigError(f"{where}.sites 必须是非空字符串数组，例如 [\"US\", \"CA\"]")
    sites: list[str] = []
    for item in raw_sites:
        if not isinstance(item, str):
            raise ConfigError(f"{where}.sites 含非字符串项：{item!r}")
        code = item.strip().upper()
        if code not in SITES:
            raise ConfigError(f"{where}.sites 含未知站点 {code!r}")
        if code not in sites:
            sites.append(code)

    proxy: Proxy | None = None
    if "proxy" in raw:
        raw_proxy = _as_str(raw, "proxy", where=where).strip()
        if raw_proxy:
            label = _as_str(raw, "proxy_label", where=where, default="") or None
            try:
                proxy = Proxy.parse(raw_proxy, label=label)
            except ValueError as exc:
                raise ConfigError(f"{where}.proxy 解析失败：{exc}") from None

    totp_seed = None
    if "totp_seed" in raw:
        totp_seed = _as_str(raw, "totp_seed", where=where).strip() or None

    return Account(
        account_id=account_id,
        email=email,
        home_site=home_site,
        proxy=proxy,
        sites=tuple(sites),
        profile_label=_as_str(raw, "profile_label", where=where, default=""),
        totp_seed=totp_seed,
        headless=_as_bool(raw, "headless", default=False),
        user_agent=(_as_str(raw, "user_agent", where=where, default="") or None),
        min_seconds_between_jobs=_as_int(raw, "min_seconds_between_jobs", default=8),
        max_actions_per_hour=_as_int(raw, "max_actions_per_hour", default=120),
        notes=_as_str(raw, "notes", where=where, default=""),
    ).validated()


def load_config(path: str | Path, *, root: Path | None = None) -> HarnessConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(
            f"配置文件不存在：{config_path}\n"
            "先生成一份： python -m amz_harness init"
        )
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败（{config_path}）：{exc}") from None

    base = (root or config_path.parent).resolve()

    harness = data.get("harness", {})
    if not isinstance(harness, dict):
        raise ConfigError("[harness] 必须是表")

    switcher_raw = data.get("site_switcher", {})
    if not isinstance(switcher_raw, dict):
        raise ConfigError("[site_switcher] 必须是表")
    switcher = SiteSwitcherConfig(
        mode=str(switcher_raw.get("mode", "navigate")),
        selector=str(switcher_raw.get("selector", "")),
    )
    if switcher.mode not in ("navigate", "dropdown"):
        raise ConfigError(f"site_switcher.mode 只能是 navigate 或 dropdown，实际 {switcher.mode!r}")
    if switcher.mode == "dropdown" and not switcher.selector:
        raise ConfigError("site_switcher.mode=dropdown 时必须给 selector，否则切站点无从下手")

    accounts_raw = data.get("accounts", {})
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        raise ConfigError("配置里没有任何账号（[accounts.<id>]）")
    accounts = {aid: _parse_account(aid, raw) for aid, raw in accounts_raw.items()}

    global_concurrency = _as_int(harness, "global_concurrency", default=3)
    if global_concurrency < 1:
        raise ConfigError("harness.global_concurrency 至少为 1")

    return HarnessConfig(
        path=config_path,
        root=base,
        profiles_dir=base / str(harness.get("profiles_dir", "profiles")),
        runs_dir=base / str(harness.get("runs_dir", "runs")),
        accounts=accounts,
        global_concurrency=global_concurrency,
        headless=_as_bool(harness, "headless", default=False),
        require_proxy=_as_bool(harness, "require_proxy", default=True),
        site_switcher=switcher,
    )


def write_example(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ConfigError(f"已存在，不覆盖：{target}")
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return target


def all_site_codes() -> list[str]:
    return sorted(SITES)


def site_label(code: str) -> str:
    site = get_site(code)
    return f"{site.code} {site.label} ({site.domain})"
