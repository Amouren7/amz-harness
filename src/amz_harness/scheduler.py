"""排程：轮询轮次、并发护栏、限速。

三条护栏，每条都对应一个真实会出事的场景：

  P1. 同账号并发恒为 1 —— 同一账号开两个浏览器环境操作后台，是最快的封号方式。
  P2. 全局并发有上限    —— 同时开 30 个 Chromium，机器先崩。
  P3. 同账号最小间隔 + 每小时上限 —— 打得太快就是"异常活动"。

时间与睡眠都是注入的，所以这些护栏能在毫秒内被穷举测试，
而不需要真的 sleep 三分钟。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

from .models import Account

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Job:
    account_id: str
    site: str
    kind: str = "health"
    priority: int = 0

    def label(self) -> str:
        return f"{self.account_id}/{self.site}:{self.kind}"


@dataclass(slots=True)
class JobResult:
    job: Job
    ok: bool
    value: object = None
    error: str | None = None
    skipped_reason: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def label(self) -> str:
        if self.skipped_reason:
            return f"{self.job.label()} 跳过({self.skipped_reason})"
        if self.ok:
            return f"{self.job.label()} 成功 {self.duration:.2f}s"
        return f"{self.job.label()} 失败: {self.error}"


class RateLimiter:
    """按账号限速。纯时间计算，不自己 sleep——sleep 交给调用方，便于测试。"""

    def __init__(
        self,
        accounts: Mapping[str, Account],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._accounts = accounts
        self._clock = clock
        self._last_finish: dict[str, float] = {}
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def delay_for(self, account_id: str) -> float:
        """还需要等多少秒才能开始这个账号的下一件事。"""
        account = self._accounts.get(account_id)
        if account is None:
            return 0.0
        last = self._last_finish.get(account_id)
        if last is None:
            return 0.0
        elapsed = self._clock() - last
        return max(0.0, account.min_seconds_between_jobs - elapsed)

    def _trim(self, account_id: str, now: float) -> None:
        window = self._recent[account_id]
        while window and now - window[0] >= 3600.0:
            window.popleft()

    def would_exceed_hourly_cap(self, account_id: str) -> bool:
        account = self._accounts.get(account_id)
        if account is None:
            return False
        now = self._clock()
        self._trim(account_id, now)
        return len(self._recent[account_id]) >= account.max_actions_per_hour

    def note(self, account_id: str, *, at: float | None = None) -> None:
        """记录一次操作已完成。"""
        moment = self._clock() if at is None else at
        self._last_finish[account_id] = moment
        self._trim(account_id, moment)
        self._recent[account_id].append(moment)

    def usage(self, account_id: str) -> tuple[int, int]:
        account = self._accounts.get(account_id)
        cap = account.max_actions_per_hour if account else 0
        now = self._clock()
        self._trim(account_id, now)
        return len(self._recent[account_id]), cap


def build_round(
    routing: Sequence[tuple[str, str]],
    *,
    round_index: int = 0,
    kind: str = "health",
) -> list[Job]:
    """把 (账号, 站点) 组合排成一轮的作业队列。

    轮转策略：按账号轮询（round-robin），而不是"把一个账号的所有站点跑完再换下一个"。
    理由是公平性——排在后面的账号不该永远饿着；而且集中在同一账号连续动作
    也更容易触发限速。
    """
    by_account: dict[str, list[str]] = {}
    for account_id, site in routing:
        by_account.setdefault(account_id, []).append(site)

    per_account: dict[str, list[str]] = {}
    for account_id, sites in by_account.items():
        unique = list(dict.fromkeys(sites))
        offset = round_index % len(unique)
        per_account[account_id] = unique[offset:] + unique[:offset]

    jobs: list[Job] = []
    order = sorted(per_account)
    depth = max((len(v) for v in per_account.values()), default=0)
    for level in range(depth):
        for account_id in order:
            sites = per_account[account_id]
            if level < len(sites):
                jobs.append(Job(account_id=account_id, site=sites[level], kind=kind))
    return jobs


async def run_round(
    jobs: Sequence[Job],
    worker: Callable[[Job], Awaitable[object]],
    *,
    global_concurrency: int = 3,
    limiter: RateLimiter | None = None,
    sleeper: Sleeper = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[JobResult]:
    """跑一轮。单个作业抛异常不会中断整轮——失败会被记下来，继续跑别的。

    对轮询任务来说，"一个站点挂了"绝不能让"其他站点也不采了"。
    """
    if global_concurrency < 1:
        raise ValueError("global_concurrency 至少为 1")

    semaphore = asyncio.Semaphore(global_concurrency)
    account_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run_one(job: Job) -> JobResult:
        started = clock()
        # P1：账号锁在 semaphore 之外，保证同一账号绝对不并行。
        async with account_locks[job.account_id]:
            if limiter is not None and limiter.would_exceed_hourly_cap(job.account_id):
                return JobResult(
                    job,
                    ok=False,
                    skipped_reason="hourly_cap_reached",
                    started_at=started,
                    finished_at=clock(),
                )
            async with semaphore:
                if limiter is not None:
                    delay = limiter.delay_for(job.account_id)
                    if delay > 0:
                        await sleeper(delay)
                try:
                    value = await worker(job)
                except Exception as exc:  # noqa: BLE001 - 故意兜住一切，单点失败不扩散
                    if limiter is not None:
                        limiter.note(job.account_id)
                    return JobResult(
                        job,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        started_at=started,
                        finished_at=clock(),
                    )
                if limiter is not None:
                    limiter.note(job.account_id)
                return JobResult(job, ok=True, value=value, started_at=started, finished_at=clock())

    return list(await asyncio.gather(*(run_one(job) for job in jobs)))


@dataclass(slots=True)
class RoundSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    states: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0

    def render(self) -> str:
        parts = [
            f"共 {self.total}",
            f"成功 {self.succeeded}",
            f"失败 {self.failed}",
            f"跳过 {self.skipped}",
        ]
        if self.states:
            tally = " ".join(f"{k}={v}" for k, v in sorted(self.states.items()))
            parts.append(f"状态[{tally}]")
        return " | ".join(parts)


def summarize(results: Iterable[JobResult]) -> RoundSummary:
    summary = RoundSummary()
    for result in results:
        summary.total += 1
        if result.skipped_reason:
            summary.skipped += 1
        elif result.ok:
            summary.succeeded += 1
        else:
            summary.failed += 1
            summary.failures.append(f"{result.job.label()}: {result.error}")
        state = getattr(result.value, "state", None)
        if state is not None:
            key = getattr(state, "value", str(state))
            summary.states[key] = summary.states.get(key, 0) + 1
    return summary
