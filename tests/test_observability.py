"""日志脱敏与运行目录。

为什么专门为"打码"写测试：URL 会被写进日志文件、trace 和异常栈，
而日志是最容易被随手转发出去的东西。这种事靠自觉一定会漏，所以用测试钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

from amz_harness.observability import RunLog, redact_url, safe_component


def test_redact_sensitive_query_values() -> None:
    raw = "https://x.example.com/ap/mfa?email=a%40b.com&password=hunter2&otpCode=123456&next=/home"
    cleaned = redact_url(raw)
    assert "hunter2" not in cleaned
    assert "123456" not in cleaned
    assert "a%40b.com" not in cleaned and "a@b.com" not in cleaned
    assert "next=%2Fhome" in cleaned or "next=/home" in cleaned


def test_redact_keeps_harmless_urls_untouched() -> None:
    assert redact_url("https://sellercentral.amazon.com/orders-v3/order/") == (
        "https://sellercentral.amazon.com/orders-v3/order/"
    )
    assert redact_url("") == ""
    plain = "https://sellercentral.amazon.de/home?ref_=xx_nav"
    assert redact_url(plain) == plain


def test_redact_is_case_insensitive() -> None:
    assert "s3cret" not in redact_url("https://x/y?PASSWORD=s3cret")


def test_run_log_redacts_url_fields_automatically(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, account_id="a1", kind="test")
    log.event("login_submit", url="https://x/ap/mfa?password=hunter2", state="needs_2fa")
    log.event("note", text="password=hunter2 这不是 url 字段，不归它管")
    log.close()

    payload = (log.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "hunter2" not in payload.split("\n")[1], "url 字段必须被打码"
    records = [json.loads(line) for line in payload.strip().split("\n")]
    submit = next(r for r in records if r["event"] == "login_submit")
    assert "***" in submit["url"]
    assert submit["state"] == "needs_2fa"
    assert records[-1]["event"] == "run_end"


def test_run_log_creates_timestamped_directory(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, account_id="shop us/main", kind="poll")
    try:
        assert log.run_dir.exists()
        assert log.run_dir.parent == tmp_path
        assert "/" not in log.run_dir.name and "\\" not in log.run_dir.name
    finally:
        log.close()


def test_run_log_path_for_cannot_escape_run_dir() -> None:
    log = RunLog(run_dir=Path("run"), account_id="a1", kind="k")
    target = log.path_for("../../evil name.png")
    assert ".." not in str(target)
    assert target.parent.name == "run"


def test_safe_component() -> None:
    assert safe_component("a b/c") == "a_b_c"
    assert safe_component("...") == "unnamed"
    assert safe_component("订单") == "unnamed", "非 ASCII 不该拼进路径"


def test_run_log_is_idempotent_on_close(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, account_id="a1", kind="k")
    log.event("x")
    log.close()
    log.close()  # 不该抛异常
    assert (log.run_dir / "events.jsonl").exists()


def test_run_log_context_manager(tmp_path: Path) -> None:
    with RunLog.create(tmp_path, account_id="a1", kind="k") as log:
        log.event("hello")
    assert "hello" in (log.run_dir / "events.jsonl").read_text(encoding="utf-8")
