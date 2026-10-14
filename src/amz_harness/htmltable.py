"""极简 HTML 表格抽取（只用标准库 html.parser，不引 bs4/lxml）。

存在的意义：把"页面 HTML -> 结构化行"这段变成**可离线测试**的纯函数。
真实后台的 DOM 千变万化，但这段逻辑不该因此变成只能靠肉眼看的东西。

已知取舍：不处理 rowspan/colspan 的合并单元格。
后台的订单/库存表基本都是规整的矩形表，为合并单元格写一套 span 展开
属于过度设计；真遇到了再补，而不是先写 200 行"也许用得上"的代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """压掉空白与不换行空格。表头比对前一律走这个。"""
    return _WS.sub(" ", text.replace("\u00a0", " ")).strip()


def normalize_header(text: str) -> str:
    """表头归一化：小写、去空格、去全角冒号等装饰。

    'Order ID' / 'order_id' / 'Order  ID ' / '订单编号：' 都归一到同一把钥匙。
    """
    cleaned = normalize(text).casefold()
    cleaned = cleaned.replace("：", "").replace(":", "").replace("_", " ").replace("-", " ")
    return _WS.sub(" ", cleaned).strip()


@dataclass
class RawTable:
    attrs: dict[str, str] = field(default_factory=dict)
    rows: list[list[str]] = field(default_factory=list)
    header_row_index: int | None = None
    caption: str = ""

    @property
    def headers(self) -> list[str]:
        if self.header_row_index is None or self.header_row_index >= len(self.rows):
            return []
        return self.rows[self.header_row_index]

    @property
    def data_rows(self) -> list[list[str]]:
        if self.header_row_index is None:
            return list(self.rows)
        return [r for i, r in enumerate(self.rows) if i != self.header_row_index]

    def attr(self, name: str) -> str:
        return self.attrs.get(name, "")


class _Frame:
    __slots__ = ("attrs", "rows", "row", "cell", "cell_is_header", "header_row_index", "caption", "in_caption")

    def __init__(self, attrs: dict[str, str]) -> None:
        self.attrs = attrs
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.cell_is_header = False
        self.header_row_index: int | None = None
        self.caption = ""
        self.in_caption = False


class _TableParser(HTMLParser):
    """用帧栈处理嵌套表格：文本只喂给最内层帧，所以内层表格不会污染外层单元格。

    另外对"标签没闭合"的手写 HTML 做容错：开新单元格/新行之前先把上一个冲刷掉。
    浏览器序列化出来的 DOM 一定是闭合的，但人会保存半截 HTML 来做校准，
    那时候静默丢数据比报错更讨厌。
    """

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[RawTable] = []
        self._stack: list[_Frame] = []
        self._skip_depth = 0

    # -- 冲刷 --
    @staticmethod
    def _flush_cell(frame: _Frame) -> None:
        if frame.cell is not None and frame.row is not None:
            frame.row.append(normalize("".join(frame.cell)))
            if frame.cell_is_header and frame.header_row_index is None:
                frame.header_row_index = len(frame.rows)
        frame.cell = None
        frame.cell_is_header = False

    @staticmethod
    def _flush_row(frame: _Frame) -> None:
        if frame.row:
            frame.rows.append(frame.row)
        frame.row = None

    # -- 生命周期 --
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag == "table":
            self._stack.append(_Frame({k: (v or "") for k, v in attrs}))
            return
        if not self._stack:
            return

        frame = self._stack[-1]
        if tag == "tr":
            self._flush_cell(frame)
            self._flush_row(frame)
            frame.row = []
        elif tag in ("td", "th"):
            self._flush_cell(frame)
            if frame.row is None:  # 容错：有些页面省略 <tr>
                frame.row = []
            frame.cell = []
            frame.cell_is_header = tag == "th"
        elif tag == "caption":
            frame.in_caption = True
        elif tag == "br":
            self._flush_br(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self._stack:
            self._flush_br(self._stack[-1])

    @staticmethod
    def _flush_br(frame: _Frame) -> None:
        if frame.cell is not None:
            frame.cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth or not self._stack:
            return

        frame = self._stack[-1]
        if tag in ("td", "th"):
            self._flush_cell(frame)
        elif tag == "tr":
            self._flush_cell(frame)
            self._flush_row(frame)
        elif tag == "caption":
            frame.in_caption = False
        elif tag == "table":
            done = self._stack.pop()
            self._flush_cell(done)
            self._flush_row(done)
            self.tables.append(
                RawTable(
                    attrs=done.attrs,
                    rows=done.rows,
                    header_row_index=done.header_row_index,
                    caption=done.caption,
                )
            )

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._stack:
            return
        frame = self._stack[-1]
        if frame.in_caption:
            frame.caption += data
        elif frame.cell is not None:
            frame.cell.append(data)


def parse_tables(html: str) -> list[RawTable]:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    # 未闭合的表格也收进来，别丢（先把挂起的单元格/行冲刷掉）
    for frame in parser._stack:  # noqa: SLF001 - 同模块内部使用
        parser._flush_cell(frame)  # noqa: SLF001
        parser._flush_row(frame)  # noqa: SLF001
        parser.tables.append(
            RawTable(
                attrs=frame.attrs,
                rows=frame.rows,
                header_row_index=frame.header_row_index,
                caption=frame.caption,
            )
        )
    # 有数据的表格排前面，方便调用方按"最像的那张"挑
    return [t for t in parser.tables if t.rows]


def to_records(table: RawTable) -> list[dict[str, str]]:
    """按表头转成 dict 列表；表头缺失或重复时给出确定性的列名。"""
    headers = [normalize(h) for h in table.headers]
    if not headers and table.data_rows:
        headers = [f"col{i + 1}" for i in range(max(len(r) for r in table.data_rows))]
    seen: dict[str, int] = {}
    keys: list[str] = []
    for index, header in enumerate(headers):
        key = header or f"col{index + 1}"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 0
        keys.append(key)

    records: list[dict[str, str]] = []
    for row in table.data_rows:
        record = {keys[i]: (row[i] if i < len(row) else "") for i in range(len(keys))}
        records.append(record)
    return records


def pick_table(tables: list[RawTable], score) -> RawTable | None:
    """挑出评分最高的表。score(table) -> 数值，越低越不可能被选中时返回 None。"""
    best: RawTable | None = None
    best_score = 0
    for table in tables:
        value = score(table)
        if value > best_score:
            best, best_score = table, value
    return best
