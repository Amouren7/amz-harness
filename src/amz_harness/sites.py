"""站点（marketplace）目录。

字段可信度说明（重要，别把不确定的当确定的用）：

  domain / locale / timezone
      可以直接用。亚马逊各站点 sellercentral 域名与本地化语言是公开事实。

  marketplace_id
      用的是 SP-API 口径的常量。上线前请用
      GET /sellers/v1/marketplaceParticipations 核对一次——那才是权威来源，
      这里只是为了让"只读采集"那条路能跑通、以及日志里能对上号。

  switch_path
      默认 None，含义是"切站点靠直接换域名"。
      **不要抄网上那种 /gp/switch-merchant-marketplace.html 之类的路径**——
      页面内切换器属于后台 UI，选择器必须对着你真实看到的界面校准。
      真要走上层下拉菜单，用 config 里的 site_switcher 选择器覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

REGION_LABELS: dict[str, str] = {
    "na": "北美",
    "eu": "欧洲",
    "fe": "远东",
}


@dataclass(frozen=True, slots=True)
class Site:
    code: str
    domain: str
    marketplace_id: str
    locale: str
    timezone: str
    region: str
    currency: str
    label: str
    # 一般不用改。存在的意义是让本地 mock / 预发环境能复用同一套站点定义，
    # 而不必为了测试去写第二套 URL 拼接逻辑。
    scheme: str = "https"

    @property
    def region_label(self) -> str:
        return REGION_LABELS.get(self.region, self.region)

    @property
    def home_url(self) -> str:
        return f"{self.scheme}://{self.domain}/"

    @property
    def signin_url(self) -> str:
        return f"{self.scheme}://{self.domain}/ap/signin"

    @property
    def orders_url(self) -> str:
        return f"{self.scheme}://{self.domain}/orders-v3/order/"

    @property
    def inventory_url(self) -> str:
        return f"{self.scheme}://{self.domain}/inventory/"


def _site(
    code: str,
    domain: str,
    marketplace_id: str,
    locale: str,
    timezone: str,
    region: str,
    currency: str,
    label: str,
) -> Site:
    return Site(code, domain, marketplace_id, locale, timezone, region, currency, label)


_SITES: tuple[Site, ...] = (
    # ---- 北美 ----
    _site("US", "sellercentral.amazon.com", "ATVPDKIKX0DER", "en-US", "America/Los_Angeles", "na", "USD", "美国"),
    _site("CA", "sellercentral.amazon.ca", "A2EUQ1WTGCTBG2", "en-CA", "America/Toronto", "na", "CAD", "加拿大"),
    _site("MX", "sellercentral.amazon.com.mx", "A1AM78C64UM0Y8", "es-MX", "America/Mexico_City", "na", "MXN", "墨西哥"),
    _site("BR", "sellercentral.amazon.com.br", "A2Q3Y263D00KWC", "pt-BR", "America/Sao_Paulo", "na", "BRL", "巴西"),
    # ---- 欧洲 ----
    _site("UK", "sellercentral.amazon.co.uk", "A1F83G8C2ARO7P", "en-GB", "Europe/London", "eu", "GBP", "英国"),
    _site("DE", "sellercentral.amazon.de", "A1PA6795UKMFR9", "de-DE", "Europe/Berlin", "eu", "EUR", "德国"),
    _site("FR", "sellercentral.amazon.fr", "A13V1IB3VIYZZH", "fr-FR", "Europe/Paris", "eu", "EUR", "法国"),
    _site("IT", "sellercentral.amazon.it", "APJ6JRA9NG5V4", "it-IT", "Europe/Rome", "eu", "EUR", "意大利"),
    _site("ES", "sellercentral.amazon.es", "A1RKKUPIHCS9HS", "es-ES", "Europe/Madrid", "eu", "EUR", "西班牙"),
    _site("NL", "sellercentral.amazon.nl", "A1805IZSGTT6HS", "nl-NL", "Europe/Amsterdam", "eu", "EUR", "荷兰"),
    _site("SE", "sellercentral.amazon.se", "A2NODRKZP88ZB9", "sv-SE", "Europe/Stockholm", "eu", "SEK", "瑞典"),
    _site("PL", "sellercentral.amazon.pl", "A1C3SOZRARQ6R3", "pl-PL", "Europe/Warsaw", "eu", "PLN", "波兰"),
    _site("BE", "sellercentral.amazon.com.be", "AMEN7PMS3EDWL", "nl-BE", "Europe/Brussels", "eu", "EUR", "比利时"),
    # ---- 远东 ----
    _site("JP", "sellercentral.amazon.co.jp", "A1VC38T7YXB528", "ja-JP", "Asia/Tokyo", "fe", "JPY", "日本"),
    _site("AU", "sellercentral.amazon.com.au", "A39IBJ37TRP1C6", "en-AU", "Australia/Sydney", "fe", "AUD", "澳大利亚"),
    _site("SG", "sellercentral.amazon.sg", "A19VAU5U5O7RUS", "en-SG", "Asia/Singapore", "fe", "SGD", "新加坡"),
    _site("IN", "sellercentral.amazon.in", "A21TJRUUN4KGV", "en-IN", "Asia/Kolkata", "fe", "INR", "印度"),
    _site("AE", "sellercentral.amazon.ae", "A2VIGQ35RCS4UG", "en-AE", "Asia/Dubai", "fe", "AED", "阿联酋"),
    _site("SA", "sellercentral.amazon.sa", "A17E79C6D8DWNP", "ar-SA", "Asia/Riyadh", "fe", "SAR", "沙特"),
    _site("TR", "sellercentral.amazon.com.tr", "A33AVAJ2PDY3EV", "tr-TR", "Europe/Istanbul", "fe", "TRY", "土耳其"),
    _site("EG", "sellercentral.amazon.eg", "ARBP9OOSHTCHU", "ar-EG", "Africa/Cairo", "fe", "EGP", "埃及"),
)

SITES: dict[str, Site] = {s.code: s for s in _SITES}


def get_site(code: str) -> Site:
    """按站点代码取站点，未知代码直接抛错（不要静默返回 None）。"""
    key = code.strip().upper()
    try:
        return SITES[key]
    except KeyError:
        known = ", ".join(sorted(SITES))
        raise KeyError(f"未知站点代码 {code!r}；可用：{known}") from None


def region_of(codes: list[str]) -> dict[str, list[str]]:
    """把站点代码按大区分组，用于排程时"同区相邻"以减少跨区跳变。"""
    out: dict[str, list[str]] = {}
    for code in codes:
        site = get_site(code)
        out.setdefault(site.region, []).append(site.code)
    return out


def all_codes() -> list[str]:
    return sorted(SITES)
