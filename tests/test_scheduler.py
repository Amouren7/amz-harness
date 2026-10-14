"""并发护栏测试。

时间与睡眠都是注入的，所以"同账号必须串行""全局不能超并发"这些护栏
可以在毫秒内被穷举验证，而不需要真的 sleep。
"""

from __future__ import annotations

import asyncio

import pytest

from amz_harness.models import Account, SessionState
from amz_harness.scheduler import (
    Job,
    JobResult,
    RateLimiter,
    build_round,
    run_round,
    summarize,
)


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def account(account_id: str, *, min_interval: int = 0, cap: int = 10_000) -> Account:
    return Account(
        account_id=account_id,
        email=f"{account_id}@example.com",
        home_site="US",
        proxy=None,
        sites=("US",),
        min_seconds_between_jobs=min_interval,
        max_actions_per_hour=cap,
    )


# ---------------------------------------------------------------------------
# 轮次编排
# ---------------------------------------------------------------------------


def labels(jobs: list[Job]) -> list[str]:
    return [j.label() for j in jobs]


def test_build_round_interleaves_accounts() -> None:
    """不允许"把一个账号的站点跑完再换下一个"——后面的账号不该永远饿着。"""
    routing = [("a1", "US"), ("a1", "DE"), ("a2", "US")]
    assert labels(build_round(routing)) == ["a1/US:health", "a2/US:health", "a1/DE:health"]


def test_build_round_rotates_starting_site() -> None:
    routing = [("a1", "US"), ("a1", "DE"), ("a1", "JP")]
    assert build_round(routing, round_index=0)[0].site == "US"
    assert build_round(routing, round_index=1)[0].site == "DE"
    assert build_round(routing, round_index=2)[0].site == "JP"
    assert build_round(routing, round_index=3)[0].site == "US"


def test_build_round_covers_every_pair_over_rounds() -> None:
    routing = [("a1", "US"), ("a1", "DE"), ("a1", "JP"), ("a2", "US"), ("a2", "CA")]
    seen = set()
    for index in range(3):
        seen |= {(j.account_id, j.site) for j in build_round(routing, round_index=index)}
    assert seen == set(routing)


def test_build_round_dedupes_pairs() -> None:
    jobs = build_round([("a1", "US"), ("a1", "US")])
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# 并发护栏
# ---------------------------------------------------------------------------


def test_per_account_concurrency_is_always_one() -> None:
    """P1：同账号开两个环境操作后台，是最快的封号方式。"""
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def worker(job: Job) -> str:
        active[job.account_id] = active.get(job.account_id, 0) + 1
        peak[job.account_id] = max(peak.get(job.account_id, 0), active[job.account_id])
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return "ok"
        finally:
            active[job.account_id] -= 1

    jobs = [Job("a1", site) for site in ("US", "DE", "JP", "CA")] + [Job("a2", "US")]
    results = asyncio.run(run_round(jobs, worker, global_concurrency=8))

    assert all(r.ok for r in results)
    assert peak == {"a1": 1, "a2": 1}, f"峰值必须为 1，实际 {peak}"


def test_global_concurrency_is_capped() -> None:
    """P2：同时开 30 个 Chromium，机器先崩。"""
    counters = {"active": 0, "peak": 0}

    async def worker(job: Job) -> str:
        counters["active"] += 1
        counters["peak"] = max(counters["peak"], counters["active"])
        try:
            await asyncio.sleep(0.01)
            return "ok"
        finally:
            counters["active"] -= 1

    jobs = [Job(f"a{i}", "US") for i in range(8)]
    asyncio.run(run_round(jobs, worker, global_concurrency=2))
    assert counters["peak"] <= 2


def test_global_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="至少为 1"):
        asyncio.run(run_round([], lambda job: None, global_concurrency=0))


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------


def test_min_interval_is_awaited_between_jobs_of_same_account() -> None:
    clock = Clock()
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        clock.t += delay

    async def worker(job: Job) -> str:
        return "ok"

    accounts = {"a1": account("a1", min_interval=10)}
    limiter = RateLimiter(accounts, clock=clock)
    jobs = [Job("a1", "US"), Job("a1", "DE")]
    asyncio.run(run_round(jobs, worker, limiter=limiter, sleeper=sleeper, clock=clock))

    assert delays == [10.0], f"第二次操作前应等满 10 秒，实际 {delays}"


def test_different_accounts_do_not_block_each_other() -> None:
    clock = Clock()
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        clock.t += delay

    accounts = {"a1": account("a1", min_interval=30), "a2": account("a2", min_interval=30)}
    limiter = RateLimiter(accounts, clock=clock)
    asyncio.run(
        run_round(
            [Job("a1", "US"), Job("a2", "US")],
            _noop,
            limiter=limiter,
            sleeper=sleeper,
            clock=clock,
        )
    )
    assert delays == [], "不同账号之间不该互相限速"


async def _noop(job: Job | None = None) -> str:
    return "ok"


def test_hourly_cap_skips_further_jobs() -> None:
    """P3：打得太快就是"异常活动"。超了就跳过，而不是继续打。"""
    clock = Clock()
    accounts = {"a1": account("a1", cap=2)}
    limiter = RateLimiter(accounts, clock=clock)
    limiter.note("a1", at=clock.t)

    async def worker(job: Job) -> str:
        return "ok"

    jobs = [Job("a1", site) for site in ("US", "DE", "JP")]
    results = asyncio.run(run_round(jobs, worker, limiter=limiter, sleeper=_noop_sleeper, clock=clock))

    assert results[0].ok is True
    assert results[1].skipped_reason == "hourly_cap_reached"
    assert results[2].skipped_reason == "hourly_cap_reached"


async def _noop_sleeper(delay: float) -> None:
    return None


def test_rate_limiter_usage_window_slides() -> None:
    clock = Clock()
    limiter = RateLimiter({"a1": account("a1", cap=2)}, clock=clock)
    limiter.note("a1", at=clock.t)
    limiter.note("a1", at=clock.t)
    assert limiter.usage("a1") == (2, 2)
    assert limiter.would_exceed_hourly_cap("a1") is True

    clock.t += 3601.0
    assert limiter.usage("a1") == (0, 2)
    assert limiter.would_exceed_hourly_cap("a1") is False


def test_rate_limiter_unknown_account_is_permissive() -> None:
    limiter = RateLimiter({}, clock=Clock())
    assert limiter.delay_for("ghost") == 0.0
    assert limiter.would_exceed_hourly_cap("ghost") is False


# ---------------------------------------------------------------------------
# 失败隔离与汇总
# ---------------------------------------------------------------------------


def test_one_failure_does_not_abort_the_round() -> None:
    """一个站点挂了，绝不能让其他站点也不采了。"""

    async def worker(job: Job) -> str:
        if job.site == "DE":
            raise RuntimeError("站点挂了")
        return "ok"

    jobs = [Job("a1", "US"), Job("a1", "DE"), Job("a1", "JP")]
    results = asyncio.run(run_round(jobs, worker))

    by_site = {r.job.site: r for r in results}
    assert by_site["US"].ok and by_site["JP"].ok
    assert by_site["DE"].ok is False
    assert "站点挂了" in (by_site["DE"].error or "")
    assert by_site["DE"].error.startswith("RuntimeError")


def test_summarize_counts_and_tallies_states() -> None:
    class FakeOutcome:
        def __init__(self, state: SessionState) -> None:
            self.state = state

    results = [
        JobResult(Job("a1", "US"), ok=True, value=FakeOutcome(SessionState.LOGGED_IN)),
        JobResult(Job("a1", "DE"), ok=True, value=FakeOutcome(SessionState.LOGGED_IN)),
        JobResult(Job("a2", "US"), ok=False, value=FakeOutcome(SessionState.NEEDS_2FA), error="需要 2FA"),
        JobResult(Job("a2", "DE"), ok=False, skipped_reason="hourly_cap_reached"),
    ]
    summary = summarize(results)
    assert (summary.total, summary.succeeded, summary.failed, summary.skipped) == (4, 2, 1, 1)
    assert summary.states == {"logged_in": 2, "needs_2fa": 1}
    assert summary.all_ok is False
    assert "needs_2fa=1" in summary.render()


def test_empty_round_is_ok() -> None:
    summary = summarize([])
    assert summary.all_ok and summary.total == 0
