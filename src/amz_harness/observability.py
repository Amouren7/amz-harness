"""可观测性：每次运行一个目录，事件写 JSONL，截图/trace 落在同目录。

为什么不是简单的 logging：这类项目出问题时，你要回答的是
"**当时那一屏长什么样**"以及"**判定依据是哪几个标记**"。
所以事件日志里必须带 markers 和证据，而不是一句 "login failed"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# 出现在 URL 查询串里就必须打码的参数名。
# 为什么需要这个：URL 会被写进日志、trace 和异常栈。真实后台的 URL 里
# 经常带 token / session / 一次性码，而日志是最容易被随手转发出去的东西。
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "session",
        "sessionid",
        "session_id",
        "auth",
        "authorization",
        "sig",
        "signature",
        "key",
        "apikey",
        "api_key",
        "secret",
        "otp",
        "otpcode",
        "otp_code",
        "code",
        "mfa",
        "email",
    }
)
_REDACTED = "***"


def redact_url(url: str) -> str:
    """把 URL 查询串里的敏感参数打码，其余原样保留。"""
    if not url or "?" not in url:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [
        (key, _REDACTED if key.casefold() in SENSITIVE_QUERY_KEYS else value) for key, value in pairs
    ]
    # safe="*" 让打码标记保持可读（否则会变成 %2A%2A%2A，人一眼看不出是被打了码）
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted, safe="*"), parts.fragment))


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_component(text: str) -> str:
    """把任意字符串变成安全的路径片段（不留 ".."、不留分隔符）。"""
    cleaned = _UNSAFE.sub("_", text)
    cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip("._-")
    return cleaned or "unnamed"


@dataclass
class RunLog:
    """一次运行的日志句柄。用完必须 close()（或用 with）。"""

    run_dir: Path
    account_id: str
    kind: str
    echo: bool = False
    _fh: TextIO | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        runs_dir: Path,
        *,
        account_id: str,
        kind: str,
        suffix: str = "",
        echo: bool = False,
    ) -> "RunLog":
        name = f"{timestamp_slug()}-{safe_component(account_id)}-{safe_component(kind)}"
        if suffix:
            name = f"{name}-{safe_component(suffix)}"
        run_dir = runs_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        log = cls(run_dir=run_dir, account_id=account_id, kind=kind, echo=echo)
        log.event("run_start", account_id=account_id, kind=kind)
        return log

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    def path_for(self, name: str) -> Path:
        return self.run_dir / safe_component(name)

    def event(self, event: str, **fields: Any) -> None:
        # 中心化脱敏：任何以 url 结尾的字段自动打码。
        # 放在这里而不是各个调用点，是因为"记得打码"这种事靠自觉一定会漏。
        safe_fields = {
            key: (redact_url(value) if key.endswith("url") and isinstance(value, str) else value)
            for key, value in fields.items()
        }
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **safe_fields,
        }
        if self._fh is None:
            self._fh = self.events_path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        if self.echo:
            detail = " ".join(
                f"{k}={v}" for k, v in safe_fields.items() if v not in (None, "", (), [])
            )
            print(f"  · {event} {detail}".rstrip())

    def close(self) -> None:
        if self._fh is not None:
            try:
                self.event("run_end")
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
