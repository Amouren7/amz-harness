"""只读采集：订单 / 库存。

**明确边界：本模块只读，不写。** 不调价、不改 listing、不点任何提交按钮。
只读采集出了 bug 最多是数据不准；写操作出了 bug 是真金白银的事故。

关于字段映射的诚实说明：
    后台表格的**表头文字随账号后台语言变化**（美国站后台可能是中文，
    德国站是德语）。所以这里的做法是"一个字段配一组多语言别名"，
    并且**默认别名只是起步值，必须对着你真实的账号后台校准一次**。
    校准方式：把页面 HTML 存下来，跑 `python -m amz_harness probe --html page.html`，
    它会告诉你哪几个字段没匹配上、实际表头是什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .htmltable import RawTable, normalize_header, parse_tables, pick_table
from .models import Marker, PageProbe, SessionState
from .session import Verdict, classify
from .sites import Site

# ---------------------------------------------------------------------------
# 字段定义。aliases 是**多语言别名集合**，不是单一列名。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    aliases: frozenset[str]
    required: bool = False


ORDERS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "order_id",
        frozenset({"订单编号", "订单号", "order id", "orderid", "bestellnummer", "注文番号", "numero de commande"}),
        required=True,
    ),
    FieldSpec(
        "purchase_date",
        frozenset({"购买日期", "下单日期", "order date", "purchase date", "date", "bestelldatum", "注文日"}),
    ),
    FieldSpec("status", frozenset({"状态", "订单状态", "status", "bestellstatus", "ステータス"})),
    FieldSpec("sku", frozenset({"sku", "msku", "seller sku", "卖家 sku"})),
    FieldSpec("asin", frozenset({"asin", "asin1"})),
    FieldSpec("quantity", frozenset({"数量", "quantity", "menge", "個数", "quantite"})),
    FieldSpec("item_total", frozenset({"商品总额", "总额", "item total", "total", "gesamt", "合計"})),
    FieldSpec("currency", frozenset({"币种", "currency", "wahrung", "通貨"})),
)

INVENTORY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("sku", frozenset({"sku", "msku", "seller sku", "卖家 sku"}), required=True),
    FieldSpec("asin", frozenset({"asin", "asin1"})),
    FieldSpec("title", frozenset({"商品名称", "标题", "title", "product name", "titel", "商品名"})),
    FieldSpec("available", frozenset({"可售数量", "可用", "available", "verfugbar", "在庫"})),
    FieldSpec("inbound", frozenset({"入库", "inbound", "在途", "reserved", "预留"})),
    FieldSpec("price", frozenset({"价格", "售价", "price", "preis", "価格"})),
)


@dataclass(frozen=True, slots=True)
class CollectRequest:
    account_id: str
    site: Site
    kind: str  # "orders" | "inventory"
    url: str

    @property
    def label(self) -> str:
        return f"{self.account_id}/{self.site.code}:{self.kind}"


@dataclass(slots=True)
class CollectOutcome:
    request: CollectRequest
    verdict: Verdict
    rows: list[dict[str, str]] = field(default_factory=list)
    mapped_columns: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0

    @property
    def state(self) -> SessionState:
        return self.verdict.state

    @property
    def ok(self) -> bool:
        return self.verdict.ok and not self.warnings

    @property
    def row_count(self) -> int:
        return len(self.rows)


class DriverLike(Protocol):
    """采集层对驱动的最小依赖面。只要满足这三个方法，谁实现都行。"""

    async def goto(self, url: str, *, timeout_ms: int = ...) -> PageProbe: ...
    async def content(self) -> str: ...


# ---------------------------------------------------------------------------
# 映射
# ---------------------------------------------------------------------------


def map_headers(headers: list[str], fields: tuple[FieldSpec, ...]) -> tuple[dict[str, int], list[str]]:
    """返回 (内部字段 -> 列下标, 警告列表)。"""
    normalized = [normalize_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for spec in fields:
        wanted = {normalize_header(a) for a in spec.aliases}
        hit = next((i for i, h in enumerate(normalized) if h and h in wanted), None)
        if hit is not None and spec.name not in mapping:
            mapping[spec.name] = hit
    warnings = [
        f"必填字段 {spec.name!r} 在表头里没找到（实际表头：{headers}）"
        for spec in fields
        if spec.required and spec.name not in mapping
    ]
    return mapping, warnings


def score_table(table: RawTable, fields: tuple[FieldSpec, ...]) -> int:
    """匹配到越多字段的表越可能是目标表。"""
    mapping, _ = map_headers(table.headers, fields)
    return len(mapping) * 10 + (1 if table.header_row_index is not None else 0)


def records_from_html(html: str, fields: tuple[FieldSpec, ...]) -> tuple[list[dict[str, str]], dict[str, str], list[str], int]:
    tables = parse_tables(html)
    table = pick_table(tables, lambda t: score_table(t, fields))
    if table is None:
        return [], {}, ["页面上没有解析到任何表格"], 0

    mapping, warnings = map_headers(table.headers, fields)
    records: list[dict[str, str]] = []
    for row in table.data_rows:
        record = {
            name: (row[index] if index < len(row) else "")
            for name, index in mapping.items()
        }
        if any(record.values()):
            records.append(record)
    if not records:
        warnings.append("表格存在但没有解析出任何非空行")
    return records, {k: table.headers[v] for k, v in mapping.items()}, warnings, len(tables)


FIELDS_BY_KIND: dict[str, tuple[FieldSpec, ...]] = {
    "orders": ORDERS_FIELDS,
    "inventory": INVENTORY_FIELDS,
}


async def collect(driver: DriverLike, request: CollectRequest, *, timeout_ms: int = 30_000) -> CollectOutcome:
    """只读采集一轮。

    关键行为：**不通过闸门就不采**。如果判定是需要 2FA / 验证码，
    直接返回空结果 + 状态，绝不"多刷几次试试"——那是把账号往火坑里推。
    """
    probe = await driver.goto(request.url, timeout_ms=timeout_ms)
    verdict = classify(probe, expected_site=request.site)
    outcome = CollectOutcome(request=request, verdict=verdict)

    if not verdict.ok:
        outcome.warnings.append(f"未登录，跳过采集（{verdict.state.value}）")
        return outcome

    html = await driver.content()
    fields = FIELDS_BY_KIND.get(request.kind, ORDERS_FIELDS)
    rows, mapped, warnings, candidates = records_from_html(html, fields)
    outcome.rows = rows
    outcome.mapped_columns = mapped
    outcome.warnings = warnings
    outcome.candidates = candidates
    return outcome


# ---------------------------------------------------------------------------
# 轮询去重：只读轮询的正确姿势是"只报增量"，不是每次把整张表重发一遍
# ---------------------------------------------------------------------------


def row_key(kind: str, row: dict[str, str]) -> str:
    if kind == "orders":
        return row.get("order_id") or "|".join(sorted(f"{k}={v}" for k, v in row.items() if v))
    return row.get("sku") or "|".join(sorted(f"{k}={v}" for k, v in row.items() if v))


class DeltaTracker:
    """记录已经见过的行,只返回新增行。

    多站点共用一个 tracker 时,key 里必须带站点前缀——同一个 order_id
    在不同站点是两回事(亚马逊各站点订单号空间是独立的)。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def new_rows(self, site_code: str, kind: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        fresh: list[dict[str, str]] = []
        for row in rows:
            key = f"{site_code}|{kind}|{row_key(kind, row)}"
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(row)
        return fresh

    def known(self, site_code: str, kind: str, row: dict[str, str]) -> bool:
        return f"{site_code}|{kind}|{row_key(kind, row)}" in self._seen

    def __len__(self) -> int:
        return len(self._seen)


def has_table_marker(probe: PageProbe) -> bool:
    return probe.any_of(Marker.ORDERS_TABLE, Marker.INVENTORY_TABLE)
