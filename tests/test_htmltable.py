from __future__ import annotations

from amz_harness.htmltable import normalize_header, parse_tables, to_records


def test_simple_table_with_thead() -> None:
    html = """
    <table id="t1">
      <thead><tr><th>订单编号</th><th>数量</th></tr></thead>
      <tbody>
        <tr><td>A-1</td><td>2</td></tr>
        <tr><td>A-2</td><td>5</td></tr>
      </tbody>
    </table>
    """
    (table,) = parse_tables(html)
    assert table.attr("id") == "t1"
    assert table.headers == ["订单编号", "数量"]
    assert table.data_rows == [["A-1", "2"], ["A-2", "5"]]


def test_header_row_index_detected_from_th() -> None:
    html = "<table><tr><td>说明</td><td>值</td></tr><tr><th>SKU</th><th>价格</th></tr><tr><td>X</td><td>9</td></tr></table>"
    (table,) = parse_tables(html)
    assert table.header_row_index == 1
    assert table.headers == ["SKU", "价格"]
    assert table.data_rows == [["说明", "值"], ["X", "9"]]


def test_nested_table_does_not_pollute_outer_cell() -> None:
    html = """
    <table id="outer">
      <tr><th>名称</th><th>值</th></tr>
      <tr>
        <td>内部<div><table><tr><td>1</td><td>2</td></tr></table></div></td>
        <td>x</td>
      </tr>
    </table>
    """
    tables = parse_tables(html)
    outer = next(t for t in tables if t.attr("id") == "outer")
    assert outer.data_rows == [["内部", "x"]]
    assert "1" not in outer.data_rows[0][0]


def test_unclosed_tags_are_tolerated() -> None:
    html = "<table><tr><td>a<td>b<tr><td>c"
    (table,) = parse_tables(html)
    assert table.rows == [["a", "b"], ["c"]]


def test_br_and_entities() -> None:
    html = "<table><tr><th>A&amp;B</th></tr><tr><td>x<br>y</td></tr></table>"
    (table,) = parse_tables(html)
    assert table.headers == ["A&B"]
    assert table.data_rows == [["x y"]]


def test_script_and_style_ignored() -> None:
    html = "<table><tr><th>H</th></tr><tr><td><script>var a=1;</script>V</td></tr></table>"
    (table,) = parse_tables(html)
    assert table.data_rows == [["V"]]


def test_empty_tables_dropped() -> None:
    assert parse_tables("<table></table><div>无表格</div>") == []


def test_to_records_without_header_uses_positional_names() -> None:
    html = "<table><tr><td>a</td><td>b</td></tr></table>"
    (table,) = parse_tables(html)
    assert table.header_row_index is None
    assert to_records(table) == [{"col1": "a", "col2": "b"}]


def test_duplicate_headers_get_suffixes() -> None:
    html = "<table><tr><th>金额</th><th>金额</th></tr><tr><td>1</td><td>2</td></tr></table>"
    (table,) = parse_tables(html)
    assert to_records(table) == [{"金额": "1", "金额_1": "2"}]


def test_short_rows_are_padded() -> None:
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>"
    (table,) = parse_tables(html)
    assert to_records(table) == [{"A": "1", "B": ""}]


def test_header_normalization_collapses_real_world_variants() -> None:
    variants = ["Order ID", "order_id", "Order-Id", "  ORDER   ID  ", "订单编号："]
    normalized = {normalize_header(v) for v in variants}
    assert "order id" in normalized
    assert "订单编号" in normalized
    assert len(normalized) == 2
