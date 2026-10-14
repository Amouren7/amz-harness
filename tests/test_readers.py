"""只读采集管线测试——用 fixtures 里那份真实的订单页。

这份 HTML 同时是 mock 服务器要发的页面，所以测试和 demo 验的是同一份数据，
不会出现"测试里过、demo 里挂"的分裂。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amz_harness.models import Marker, PageProbe, SessionState
from amz_harness.readers import (
    FIELDS_BY_KIND,
    CollectRequest,
    DeltaTracker,
    collect,
    map_headers,
    records_from_html,
    row_key,
)
from amz_harness.sites import get_site

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mock_sellercentral"
US = get_site("US")


@pytest.fixture(scope="module")
def orders_html() -> str:
    return (FIXTURES / "orders.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inventory_html() -> str:
    return (FIXTURES / "inventory.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 表选择与字段映射
# ---------------------------------------------------------------------------


def test_picks_target_table_over_decoy(orders_html: str) -> None:
    rows, mapped, warnings, candidates = records_from_html(orders_html, FIELDS_BY_KIND["orders"])
    assert candidates == 2, "页面上一共有两张表"
    assert len(rows) == 3
    assert warnings == []
    assert mapped["order_id"] == "订单编号"
    assert mapped["sku"] == "SKU"


def test_orders_rows_are_complete(orders_html: str) -> None:
    rows, _, _, _ = records_from_html(orders_html, FIELDS_BY_KIND["orders"])
    first = rows[0]
    assert first["order_id"] == "113-1234567-8901234"
    assert first["purchase_date"] == "2026-10-08T09:12:00+00:00"
    assert first["status"] == "Pending"
    assert first["sku"] == "SKU-RED-L"
    assert first["quantity"] == "1"
    assert first["item_total"] == "29.99"
    assert first["currency"] == "USD"


def test_inventory_rows(inventory_html: str) -> None:
    rows, mapped, warnings, _ = records_from_html(inventory_html, FIELDS_BY_KIND["inventory"])
    assert len(rows) == 3
    assert warnings == []
    assert mapped["available"] == "可售数量"
    assert rows[2]["available"] == "0"


def test_header_aliases_cover_multiple_languages() -> None:
    """同一套字段定义要能吃下不同语言的账号后台。"""
    fields = FIELDS_BY_KIND["orders"]
    for headers in (
        ["Order ID", "Purchase Date", "Status", "SKU", "Quantity", "Item Total", "Currency"],
        ["Bestellnummer", "Bestelldatum", "Bestellstatus", "SKU", "Menge", "Gesamt", "Währung"],
        ["订单编号", "购买日期", "订单状态", "SKU", "数量", "商品总额", "币种"],
    ):
        mapping, warnings = map_headers(headers, fields)
        assert "order_id" in mapping, f"没匹配上：{headers}"
        assert warnings == []


def test_missing_required_field_produces_warning() -> None:
    html = "<table><tr><th>状态</th><th>数量</th></tr><tr><td>Pending</td><td>1</td></tr></table>"
    rows, mapped, warnings, _ = records_from_html(html, FIELDS_BY_KIND["orders"])
    assert "order_id" not in mapped
    assert any("order_id" in w for w in warnings)
    assert rows and "order_id" not in rows[0]


def test_no_table_gives_clear_warning() -> None:
    rows, mapped, warnings, candidates = records_from_html("<div>没有表</div>", FIELDS_BY_KIND["orders"])
    assert (rows, mapped, candidates) == ([], {}, 0)
    assert "没有解析到任何表格" in warnings[0]


def test_table_with_only_empty_cells_is_reported() -> None:
    html = "<table><tr><th>订单编号</th></tr><tr><td></td></tr></table>"
    rows, _, warnings, _ = records_from_html(html, FIELDS_BY_KIND["orders"])
    assert rows == []
    assert any("非空行" in w for w in warnings)


# ---------------------------------------------------------------------------
# collect：闸门优先，绝不越过闸门采集
# ---------------------------------------------------------------------------


class StubDriver:
    def __init__(self, probe: PageProbe, html: str = "") -> None:
        self._probe = probe
        self._html = html
        self.content_calls = 0

    async def goto(self, url: str, *, timeout_ms: int = 30_000) -> PageProbe:
        return self._probe

    async def content(self) -> str:
        self.content_calls += 1
        return self._html


def request(kind: str = "orders") -> CollectRequest:
    return CollectRequest("a1", US, kind, US.orders_url)


def test_collect_returns_rows_when_logged_in(orders_html: str) -> None:
    import asyncio

    probe = PageProbe(url=US.orders_url, markers=frozenset({Marker.SELLER_NAV, Marker.ORDERS_TABLE}))
    outcome = asyncio.run(collect(StubDriver(probe, orders_html), request()))
    assert outcome.state is SessionState.LOGGED_IN
    assert outcome.ok
    assert outcome.row_count == 3


def test_collect_refuses_to_read_through_a_gate() -> None:
    """出现验证码时不能去读页面内容——多刷几次只会让情况更糟。"""
    import asyncio

    driver = StubDriver(PageProbe(url=US.orders_url, markers=frozenset({Marker.CAPTCHA})), "<html></html>")
    outcome = asyncio.run(collect(driver, request()))
    assert outcome.state is SessionState.NEEDS_CAPTCHA
    assert outcome.row_count == 0
    assert driver.content_calls == 0, "闸门状态下不该去取页面内容"
    assert any("未登录" in w for w in outcome.warnings)


def test_collect_on_logged_out_page() -> None:
    import asyncio

    driver = StubDriver(PageProbe(url=US.signin_url, markers=frozenset({Marker.SIGNIN_FORM})), "<html></html>")
    outcome = asyncio.run(collect(driver, request()))
    assert outcome.state is SessionState.LOGGED_OUT
    assert outcome.row_count == 0


# ---------------------------------------------------------------------------
# 增量轮询
# ---------------------------------------------------------------------------


def test_row_key_prefers_stable_identifier() -> None:
    assert row_key("orders", {"order_id": "A-1", "status": "Pending"}) == "A-1"
    assert row_key("inventory", {"sku": "SKU-1", "available": "3"}) == "SKU-1"


def test_row_key_falls_back_to_full_row() -> None:
    assert row_key("orders", {"status": "Pending"}) == "status=Pending"


def test_delta_tracker_only_reports_new_rows(orders_html: str) -> None:
    rows, _, _, _ = records_from_html(orders_html, FIELDS_BY_KIND["orders"])
    tracker = DeltaTracker()

    assert len(tracker.new_rows("US", "orders", rows)) == 3
    assert tracker.new_rows("US", "orders", rows) == []
    # 新来一单
    rows.append({"order_id": "113-9999999-0000000", "status": "Pending"})
    assert len(tracker.new_rows("US", "orders", rows)) == 1


def test_delta_tracker_is_scoped_per_site(orders_html: str) -> None:
    """同一个 order_id 在不同站点是两回事，不能混在一起去重。"""
    rows, _, _, _ = records_from_html(orders_html, FIELDS_BY_KIND["orders"])
    tracker = DeltaTracker()
    tracker.new_rows("US", "orders", rows)
    assert len(tracker.new_rows("DE", "orders", rows)) == 3


def test_delta_tracker_known() -> None:
    tracker = DeltaTracker()
    row = {"order_id": "A-1"}
    assert not tracker.known("US", "orders", row)
    tracker.new_rows("US", "orders", [row])
    assert tracker.known("US", "orders", row)
    assert len(tracker) == 1
