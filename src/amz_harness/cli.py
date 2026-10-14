"""命令行入口。

    python -m amz_harness init                 生成配置模板
    python -m amz_harness sites                列出全部站点目录
    python -m amz_harness doctor               自检（含 playwright / 代理连通性）
    python -m amz_harness env-up               构造并落盘每个账号的隔离环境
    python -m amz_harness totp --account X     打印当前 2FA 验证码
    python -m amz_harness probe --html p.html  离线诊断选择器/表头（校准用）
    python -m amz_harness poll --dry-run       离线跑一轮轮询（验证并发护栏）
    python -m amz_harness poll --live          真机跑一轮（会真开浏览器）
    python -m amz_harness demo                 用本地 mock 后台跑通全链路
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .config import ConfigError, HarnessConfig, load_config, write_example
from .driver.base import MARKER_SELECTORS
from .models import Account, Marker
from .observability import RunLog
from .profiles import build_environment, materialize, spec_to_dict
from .readers import (
    FIELDS_BY_KIND,
    CollectRequest,
    DeltaTracker,
    records_from_html,
)
from .scheduler import RateLimiter, summarize
from .sites import SITES, get_site
from .switch import switch_to
from .totp import seconds_remaining, totp

DEFAULT_CONFIG = Path("config") / "harness.toml"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _rule(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 68 - len(title)))


def _ok(flag: bool) -> str:
    return "✅" if flag else "❌"


def _load(args: argparse.Namespace, *, required: bool = True) -> HarnessConfig | None:
    path = Path(args.config or DEFAULT_CONFIG)
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        if required:
            raise SystemExit(2) from None
        return None


# ---------------------------------------------------------------------------
# init / sites
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or DEFAULT_CONFIG)
    try:
        written = write_example(target)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"已生成配置模板：{written}")
    print("接下来：")
    print("  1) 改 accounts 里的邮箱/站点/代理")
    print("  2) 代理密码与 TOTP 种子用环境变量注入，别写进文件")
    print("     setx AMZ_PASSWORD_SHOP_US_MAIN \"...\"")
    print("     setx AMZ_TOTP_SHOP_US_MAIN \"...\"")
    print("  3) python -m amz_harness doctor")
    return 0


def cmd_sites(args: argparse.Namespace) -> int:
    _rule(f"站点目录（共 {len(SITES)} 个 marketplace）")
    print(f"{'代码':<5}{'市场':<10}{'域名':<32}{'语言':<8}{'时区':<22}{'大区':<6}{'币种'}")
    for code in sorted(SITES):
        site = SITES[code]
        print(
            f"{site.code:<5}{site.label:<10}{site.domain:<32}{site.locale:<8}"
            f"{site.timezone:<22}{site.region_label:<6}{site.currency}"
        )
    print()
    print("marketplace_id 用的是 SP-API 口径常量，上线前用")
    print("GET /sellers/v1/marketplaceParticipations 核对一次。")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


async def _check_playwright() -> tuple[bool, str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "未安装。用官方源装：pip install -i https://pypi.org/simple playwright"

    pw = await async_playwright().start()
    try:
        exe = Path(pw.chromium.executable_path)
        if exe.exists():
            return True, f"chromium 就绪：{exe}"
        return False, f"chromium 未下载（期望路径 {exe}）。跑：python -m playwright install chromium"
    finally:
        await pw.stop()


async def _check_proxy(proxy) -> tuple[bool, str]:
    if proxy is None:
        return False, "未配置代理（多账号必须一账号一 IP）"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port), timeout=6.0
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, f"TCP 可达 {proxy.display}"
    except Exception as exc:
        return False, f"TCP 不可达 {proxy.display}：{type(exc).__name__}: {exc}"


async def _doctor_async(cfg: HarnessConfig | None, workdir: Path) -> int:
    failures = 0

    _rule("运行环境")
    print(f"Python      {sys.version.split()[0]}")
    print(f"amz-harness v{__version__}")
    ok, detail = await _check_playwright()
    print(f"{_ok(ok)} Playwright  {detail}")
    failures += 0 if ok else 1

    if cfg is None:
        print("⚠️  没有可用配置，账号相关的检查跳过")
        return failures

    _rule(f"配置：{cfg.path}")
    print(f"根目录        {cfg.root}")
    print(f"profile 目录  {cfg.profiles_dir}")
    print(f"账号数        {len(cfg.accounts)}")
    print(f"全局并发上限  {cfg.global_concurrency}")
    print(f"站点切换模式  {cfg.site_switcher.mode}")
    print(f"轮询作业面    {len(cfg.routing())} 个 (账号 × 站点) 组合")

    _rule("账号自检")
    for account_id, account in sorted(cfg.accounts.items()):
        print(f"\n[{account_id}] {account.profile_label or ''}  ({account.email})")
        print(f"  指纹锚点站点 {account.home_site}  轮询站点 {', '.join(account.sites)}")
        print(f"  限速         ≥{account.min_seconds_between_jobs}s/次，≤{account.max_actions_per_hour} 次/小时")
        print(f"  TOTP        {'已配置（2FA 可全自动）' if account.totp_seed else '未配置（2FA 走人工兜底）'}")

        spec, issues = build_environment(account, root=cfg.profiles_dir, require_proxy=cfg.require_proxy)
        blocking = [i for i in issues if i.level == "error"]
        failures += len(blocking)
        for issue in issues:
            mark = "❌" if issue.level == "error" else "⚠️ "
            print(f"  {mark} [{issue.code}] {issue.message}")
        if not blocking:
            print(f"  ✅ 环境规格：locale={spec.locale} tz={spec.timezone} viewport={spec.viewport[0]}x{spec.viewport[1]}")

        if account.proxy is None and not cfg.require_proxy:
            print("  ⏭️  代理：未配置（require_proxy=false，仅限单账号本机调试）")
        else:
            proxy_ok, proxy_detail = await _check_proxy(account.proxy)
            print(f"  {_ok(proxy_ok)} 代理：{proxy_detail}")
            if not proxy_ok and cfg.require_proxy:
                failures += 1

        try:
            spec.profile_dir.mkdir(parents=True, exist_ok=True)
            probe_file = spec.profile_dir / ".write_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            print(f"  ✅ profile 目录可写：{spec.profile_dir}")
        except OSError as exc:
            print(f"  ❌ profile 目录不可写：{exc}")
            failures += 1

    _rule("结论")
    print("全部通过。" if failures == 0 else f"发现 {failures} 个必须处理的问题。")
    return failures


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args, required=False)
    workdir = Path(args.workdir or ".").resolve()
    return asyncio.run(_doctor_async(cfg, workdir))


# ---------------------------------------------------------------------------
# env-up
# ---------------------------------------------------------------------------


def cmd_env_up(args: argparse.Namespace) -> int:
    cfg = _load(args)
    assert cfg is not None
    _rule("环境启动（每个账号一套隔离环境）")
    problems = 0
    for account_id, account in sorted(cfg.accounts.items()):
        spec, issues = build_environment(account, root=cfg.profiles_dir, require_proxy=cfg.require_proxy)
        if issues:
            for issue in issues:
                mark = "❌" if issue.level == "error" else "⚠️ "
                print(f"{mark} [{account_id}] [{issue.code}] {issue.message}")
                problems += 1 if issue.level == "error" else 0
            if any(i.level == "error" for i in issues):
                continue
        report = materialize(spec)
        status = "新建" if report.created else "复用"
        print(f"\n[{account_id}] {status} {report.spec.profile_dir}")
        print(f"  locale={spec.locale} timezone={spec.timezone} viewport={spec.viewport[0]}x{spec.viewport[1]}")
        print(f"  代理={spec.proxy.display if spec.proxy else '(无)'}")
        for drift in report.drift:
            print(f"  ⚠️  [{drift.code}] {drift.message}")

    _rule("提示")
    print("· profile 目录本身**不要**提交进 git：里面有登录 cookie。")
    print("· 先在本机用真实环境人工登录一次（尤其是过 2FA 并勾选'信任此设备'），")
    print("  之后这套 profile 才是'已登录'状态，自动化才能接上。")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# totp
# ---------------------------------------------------------------------------


def cmd_totp(args: argparse.Namespace) -> int:
    cfg = _load(args)
    assert cfg is not None
    account = cfg.account(args.account)
    if not account.totp_seed:
        print(f"账号 {account.account_id} 没有配置 TOTP 种子，2FA 只能走人工兜底。")
        return 1
    code = totp(account.totp_seed)
    print(f"{account.account_id} 当前验证码：{code}")
    print(f"剩余有效期：{seconds_remaining():.1f} 秒")
    return 0


# ---------------------------------------------------------------------------
# probe：离线诊断（选择器校准的入口）
# ---------------------------------------------------------------------------


_ID_RE = re.compile(r"#([A-Za-z][\w-]*)")
_NAME_RE = re.compile(r"\[name=['\"]?([\w-]+)['\"]?\]")
_DM_RE = re.compile(r"\[data-marker=['\"]?([\w-]+)['\"]?\]")


def diagnose_html(html: str) -> dict:
    page_ids = set(re.findall(r'id=["\']([^"\']+)["\']', html))
    page_names = set(re.findall(r'name=["\']([^"\']+)["\']', html))
    page_data_markers = set(re.findall(r'data-marker=["\']([^"\']+)["\']', html))

    marker_hits: dict[str, tuple[bool, str]] = {}
    for marker, selector in MARKER_SELECTORS.items():
        ids = set(_ID_RE.findall(selector))
        names = set(_NAME_RE.findall(selector))
        data_markers = set(_DM_RE.findall(selector))
        hit_id = ids & page_ids
        hit_name = names & page_names
        hit_dm = data_markers & page_data_markers
        evidence = []
        if hit_id:
            evidence.append(f"id={'/'.join(sorted(hit_id))}")
        if hit_name:
            evidence.append(f"name={'/'.join(sorted(hit_name))}")
        if hit_dm:
            evidence.append(f"data-marker={'/'.join(sorted(hit_dm))}")
        marker_hits[marker.value] = (bool(evidence), " ".join(evidence))

    tables_report = []
    for kind, fields in FIELDS_BY_KIND.items():
        rows, mapped, warnings, candidates = records_from_html(html, fields)
        tables_report.append(
            {
                "kind": kind,
                "rows": len(rows),
                "candidates": candidates,
                "mapped": mapped,
                "warnings": warnings,
            }
        )
    return {"markers": marker_hits, "tables": tables_report}


def cmd_probe(args: argparse.Namespace) -> int:
    path = Path(args.html)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    html = path.read_text(encoding="utf-8", errors="replace")
    report = diagnose_html(html)

    _rule(f"语义标记诊断：{path.name}")
    print("（只覆盖 id / name / data-marker 三类通用约定；CSS 类选择器无法离线判定）")
    for name, (hit, evidence) in report["markers"].items():
        print(f"  {_ok(hit)} {name:<18} {evidence}")

    _rule("表格与字段映射诊断")
    for entry in report["tables"]:
        print(f"\n[{entry['kind']}] 页面上共 {entry['candidates']} 张表，解析出 {entry['rows']} 行")
        if entry["mapped"]:
            for field, header in entry["mapped"].items():
                print(f"  ✅ {field:<14} <- 列头 {header!r}")
        else:
            print("  ❌ 一个字段都没映射上")
        for warning in entry["warnings"]:
            print(f"  ⚠️  {warning}")

    _rule("怎么用这份报告")
    print("· 哪个标记没命中 -> 去 session.py 里查它会判成什么状态；")
    print("  若判成 unknown，说明该页面的识别特征需要补进 MARKER_SELECTORS。")
    print("· 哪个字段没映射上 -> 把真实列头加进 readers.py 的 aliases。")
    print("  这一步是**必须做的校准**，别指望默认别名在真实后台全覆盖。")
    return 0


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


async def _dry_run(cfg: HarnessConfig, kinds: list[str]) -> int:
    from .driver.fake import FakeDriver, FakePage
    from .readers import collect
    from .scheduler import build_round, run_round

    routing = cfg.routing()
    _rule(f"离线轮询：{len(routing)} 个 (账号 × 站点) 组合")

    # 每个账号一个独立的假浏览器；统计"同账号同时在跑"的峰值用于自证
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    trackers: dict[str, DeltaTracker] = {aid: DeltaTracker() for aid in cfg.accounts}

    async def worker(job):
        active[job.account_id] = active.get(job.account_id, 0) + 1
        peak[job.account_id] = max(peak.get(job.account_id, 0), active[job.account_id])
        try:
            base = f"http://127.0.0.1:9/{job.account_id}"  # 只用于拼 URL，不会真的联网
            pages = {
                f"{base}/home": FakePage(
                    url=f"{base}/home",
                    markers=frozenset({Marker.SELLER_NAV, Marker.MKT_SWITCHER}),
                ),
                **{
                    f"{base}/{kind}": FakePage(
                        url=f"{base}/{kind}",
                        markers=frozenset({Marker.SELLER_NAV}),
                        html=_fake_table_html(kind),
                    )
                    for kind in kinds
                },
            }
            default_page = FakePage(url=f"{base}/home", markers=frozenset({Marker.SELLER_NAV}))
            site = get_site(job.site)
            last = None
            for kind in kinds:
                # 每个作业自己开一个假驱动，模拟"一次独立的环境使用"
                driver = FakeDriver(pages=dict(pages), default=default_page)
                await driver.start()
                outcome = await collect(
                    driver, CollectRequest(job.account_id, site, kind, f"{base}/{kind}")
                )
                await driver.close()
                fresh = trackers[job.account_id].new_rows(site.code, kind, outcome.rows)
                if log_verbose:
                    print(f"    {job.account_id}/{site.code} {kind}: {outcome.row_count} 行，新增 {len(fresh)}")
                last = outcome
            return last if last is not None else default_page.to_probe()
        finally:
            active[job.account_id] -= 1

    # 用虚拟时钟绕过限速等待，让离线跑秒级完成（护栏逻辑本身仍然生效）
    clock_state = {"t": 1_000_000.0}
    limiter = RateLimiter(cfg.accounts)
    limiter._clock = lambda: clock_state["t"]  # noqa: SLF001 - 离线演示，显式标注
    for account_id in cfg.accounts:
        limiter.note(account_id, at=-10_000.0)

    async def sleeper(delay: float) -> None:
        clock_state["t"] += delay

    results = await run_round(
        build_round(routing, kind="poll"),
        worker,
        global_concurrency=cfg.global_concurrency,
        limiter=limiter,
        sleeper=sleeper,
        clock=lambda: clock_state["t"],
    )

    _rule("结果")
    for result in results:
        print(f"  {'✅' if result.ok else '❌'} {result.label()}")

    summary = summarize(results)
    print()
    print(f"汇总：{summary.render()}")

    _rule("并发护栏自证")
    for account_id in sorted(cfg.accounts):
        peak_value = peak.get(account_id, 0)
        print(f"  {_ok(peak_value <= 1)} 账号 {account_id} 同时在跑的作业峰值 = {peak_value}（必须 ≤ 1）")
    print(f"  ✅ 全局并发上限 = {cfg.global_concurrency}")
    return 0 if summary.all_ok else 1


log_verbose = False
args_verbose = False


def _fake_table_html(kind: str) -> str:
    from .readers import FIELDS_BY_KIND

    headers = []
    for spec in FIELDS_BY_KIND.get(kind, ()):
        headers.append(sorted(spec.aliases)[0])
    th = "".join(f"<th>{h}</th>" for h in headers)
    return f'<nav id="sc-navbar"></nav><table id="{kind}-table"><tr>{th}</tr><tr>{"".join("<td>x</td>" for _ in headers)}</tr></table>'


async def _live_poll(cfg: HarnessConfig, kinds: list[str], *, channel: str | None, headless: bool) -> int:
    from .driver.playwright_driver import PlaywrightDriver
    from .flows import ConsoleGate, EnvCredentials, login
    from .readers import collect

    by_account: dict[str, list[str]] = {}
    for account_id, site in cfg.routing():
        by_account.setdefault(account_id, []).append(site)

    tracker = DeltaTracker()
    semaphore = asyncio.Semaphore(cfg.global_concurrency)
    lines: list[str] = []
    hard_failures = 0

    async def one_account(account_id: str, site_codes: list[str]) -> None:
        nonlocal hard_failures
        account = cfg.accounts[account_id]
        if headless:
            account = replace(account, headless=True)
        async with semaphore:
            spec, issues = build_environment(account, root=cfg.profiles_dir, require_proxy=cfg.require_proxy)
            if any(i.level == "error" for i in issues):
                for issue in issues:
                    lines.append(f"❌ {account_id} [{issue.code}] {issue.message}")
                hard_failures += 1
                return
            report = materialize(spec)
            for drift in report.drift:
                lines.append(f"⚠️  {account_id} [{drift.code}] {drift.message}")

            log = RunLog.create(cfg.runs_dir, account_id=account_id, kind="poll", echo=True)
            driver = PlaywrightDriver(spec, channel=channel, log=log, trace_path=log.path_for("trace.zip"))
            try:
                await driver.start()
                home = get_site(account.home_site)
                outcome = await login(
                    driver,
                    account,
                    home,
                    credentials=EnvCredentials(),
                    gate=ConsoleGate(),
                    screenshot_dir=log.run_dir / "shots",
                    log=log,
                )
                lines.append(f"{'✅' if outcome.ok else '❌'} {account_id} {outcome.render()}")
                if not outcome.ok:
                    hard_failures += 1
                    if outcome.human_message:
                        lines.append(outcome.human_message)
                    return

                for code in site_codes:
                    site = get_site(code)
                    switch = await switch_to(
                        driver,
                        site,
                        from_site=home.code,
                        mode=cfg.site_switcher.mode,
                        selector=cfg.site_switcher.selector,
                        log=log,
                    )
                    lines.append(f"  {switch.render()}")
                    if not switch.ok:
                        if switch.requires_relogin or switch.needs_human:
                            hard_failures += 1
                        continue
                    for kind in kinds:
                        url = site.orders_url if kind == "orders" else site.inventory_url
                        collected = await collect(driver, CollectRequest(account_id, site, kind, url))
                        fresh = tracker.new_rows(site.code, kind, collected.rows)
                        note = "；".join(collected.warnings) if collected.warnings else ""
                        lines.append(
                            f"    {'✅' if collected.ok else '⚠️ '} {site.code} {kind}: "
                            f"{collected.row_count} 行，新增 {len(fresh)} {note}"
                        )
            except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮整轮
                hard_failures += 1
                lines.append(f"❌ {account_id} 异常：{type(exc).__name__}: {exc}")
            finally:
                await driver.close()
                log.close()

    _rule("真机轮询（会真的打开浏览器）")
    await asyncio.gather(*(one_account(aid, codes) for aid, codes in sorted(by_account.items())))
    for line in lines:
        print(line)

    _rule("结论")
    if hard_failures:
        print(f"有 {hard_failures} 处需要人工处理。日志目录：{cfg.runs_dir}")
        return 1
    print(f"本轮无阻断问题。日志目录：{cfg.runs_dir}")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    cfg = _load(args)
    assert cfg is not None
    kinds = [k.strip() for k in (args.kinds or "orders").split(",") if k.strip()]
    unknown = [k for k in kinds if k not in FIELDS_BY_KIND]
    if unknown:
        print(f"未知采集类型 {unknown}；可用：{', '.join(sorted(FIELDS_BY_KIND))}", file=sys.stderr)
        return 2

    if args.live:
        return asyncio.run(
            _live_poll(cfg, kinds, channel=args.channel, headless=args.headless)
        )
    return asyncio.run(_dry_run(cfg, kinds))


# ---------------------------------------------------------------------------
# demo：本地 mock 全链路
# ---------------------------------------------------------------------------


async def _demo_async(args: argparse.Namespace) -> int:
    from .driver.playwright_driver import PlaywrightDriver
    from .flows import ScriptedGate, StaticCredentials, login
    from .mockserver import mock_seller_central
    from .readers import collect
    from .session import classify

    workdir = Path(args.workdir).resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="amz-demo-"))
    profiles_dir = workdir / "profiles"
    runs_dir = workdir / "runs"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    demo_account = Account(
        account_id="demo",
        email="demo@example.com",
        home_site="US",
        proxy=None,
        sites=("US", "DE"),
        profile_label="演示账号（本地 mock，无真实站点）",
        headless=bool(args.headless),
        notes="demo",
    )

    print(f"工作目录：{workdir}")

    with mock_seller_central() as base_url:
        netloc = urlsplit(base_url).netloc
        mock_us = replace(get_site("US"), scheme="http", domain=netloc)
        mock_de = replace(get_site("DE"), scheme="http", domain=netloc)
        print(f"mock 卖家后台：{base_url}")

        _rule("1. 环境启动（隔离 profile + 指纹锚定）")
        spec, issues = build_environment(demo_account, root=profiles_dir, require_proxy=False)
        report = materialize(spec)
        print(json.dumps(spec_to_dict(spec), ensure_ascii=False, indent=2))
        print(f"profile 目录：{spec.profile_dir}（{'新建' if report.created else '复用'}）")
        print("注意：locale/timezone 是按 home_site=US 定的，等下切到 DE 也**不会变**。")
        if issues:
            for issue in issues:
                print(f"⚠️  [{issue.code}] {issue.message}")

        log = RunLog.create(runs_dir, account_id="demo", kind="demo", echo=True)
        driver = PlaywrightDriver(spec, channel=args.channel, trace_path=log.path_for("trace.zip"), log=log)

        try:
            await driver.start()
            print(f"浏览器已启动（headless={spec.headless}，channel={args.channel or 'bundled chromium'}）")

            _rule("2. 登录（密码走凭据提供方，不进配置文件）")
            outcome = await login(
                driver,
                demo_account,
                mock_us,
                credentials=StaticCredentials({"demo": "demo-password"}),
                gate=ScriptedGate(codes=["123456"]),
                screenshot_dir=log.run_dir / "shots",
                log=log,
            )
            print(outcome.render())
            print(f"结果：{'登录成功' if outcome.ok else '未登录'}；2FA 来源={outcome.code_source}")
            for step in outcome.steps:
                print(f"  · {step}")
            if not outcome.ok:
                print("登录未成功，后续步骤跳过。")
                return 1

            _rule("3. 站点切换 + 切换后校验")
            switched = await switch_to(driver, mock_de, from_site="US", log=log)
            print(switched.render())
            for step in switched.steps:
                print(f"  · {step}")

            _rule("4. 只读采集：订单")
            collected = await collect(driver, CollectRequest("demo", mock_de, "orders", mock_de.orders_url))
            print(f"状态={collected.state.value}，解析 {collected.row_count} 行，候选表 {collected.candidates} 张")
            print(f"字段映射：{collected.mapped_columns}")
            for warning in collected.warnings:
                print(f"  ⚠️  {warning}")
            for row in collected.rows:
                print(f"  {row.get('order_id','?'):<22}{row.get('status',''):<10}{row.get('sku',''):<14}{row.get('item_total','')}")

            _rule("5. 轮询增量（只报新增，不重发全量）")
            tracker = DeltaTracker()
            first = tracker.new_rows("DE", "orders", collected.rows)
            second = tracker.new_rows("DE", "orders", collected.rows)
            print(f"第一次：新增 {len(first)} 行；第二次同样的数据：新增 {len(second)} 行（应为 0）")

            _rule("6. 只读采集：库存")
            inventory = await collect(
                driver, CollectRequest("demo", mock_de, "inventory", mock_de.inventory_url)
            )
            print(f"状态={inventory.state.value}，解析 {inventory.row_count} 行")
            for row in inventory.rows:
                print(f"  {row.get('sku','?'):<14}{row.get('available',''):>5}  {row.get('title','')}")

            _rule("7. 闸门行为：验证码 -> 转人工，绝不自动重试")
            probe = await driver.goto(f"{base_url}/ap/captcha")
            verdict = classify(probe, expected_site=mock_de)
            print(f"判定：{verdict.summary}")
            print(f"需要人工：{verdict.needs_human}")
            print(f"提示文案：{verdict.human_prompt}")
            print("关键：状态机对闸门状态只返回 ESCALATE_HUMAN，不存在'重试 3 次'这条路径。")

            _rule("8. profile 内容（登录态落在这里，别提交进 git）")
            entries = sorted(p.name for p in spec.profile_dir.iterdir())
            print(f"{spec.profile_dir} 下有 {len(entries)} 项，例如：{entries[:6]}")

            print()
            print(f"运行日志：{log.run_dir}")
            print(f"trace（可用 playwright show-trace 回放）：{log.path_for('trace.zip')}")
        finally:
            await driver.close()
            log.close()

    _rule("demo 完成")
    print("这条链路证明的是**编排正确性**：环境隔离、登录、2FA、切换校验、只读采集、增量、闸门上报。")
    print("它不能证明的是：真实亚马逊后台的选择器是否正确——那必须你自己对着真机校准。")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    return asyncio.run(_demo_async(args))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amz-harness",
        description="多账号亚马逊卖家后台：环境启动 / 会话健康状态机 / 站点轮询切换 / 只读采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"amz-harness {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help=f"配置文件（默认 {DEFAULT_CONFIG}）")

    p_init = sub.add_parser("init", help="生成配置模板")
    p_init.add_argument("--path", default=str(DEFAULT_CONFIG))
    p_init.set_defaults(func=cmd_init)

    p_sites = sub.add_parser("sites", help="列出站点目录")
    p_sites.set_defaults(func=cmd_sites)

    p_doctor = sub.add_parser("doctor", help="自检环境、配置、代理")
    add_config(p_doctor)
    p_doctor.add_argument("--workdir", default=".")
    p_doctor.set_defaults(func=cmd_doctor)

    p_env = sub.add_parser("env-up", help="构造并落盘每个账号的隔离环境")
    add_config(p_env)
    p_env.set_defaults(func=cmd_env_up)

    p_totp = sub.add_parser("totp", help="打印账号当前 2FA 验证码")
    add_config(p_totp)
    p_totp.add_argument("--account", required=True)
    p_totp.set_defaults(func=cmd_totp)

    p_probe = sub.add_parser("probe", help="离线诊断选择器与表头（校准用）")
    p_probe.add_argument("--html", required=True, help="保存下来的真实页面 HTML")
    p_probe.set_defaults(func=cmd_probe)

    p_poll = sub.add_parser("poll", help="跑一轮轮询")
    add_config(p_poll)
    p_poll.add_argument("--live", action="store_true", help="真机模式（会真开浏览器）")
    p_poll.add_argument("--dry-run", action="store_true", help="离线模式（默认）")
    p_poll.add_argument("--kinds", default="orders", help="采集类型，逗号分隔：orders,inventory")
    p_poll.add_argument("--channel", default=None, help="浏览器通道，如 chrome（用真实 Chrome 更不易被识别）")
    p_poll.add_argument("--headless", action="store_true")
    p_poll.add_argument("-v", "--verbose", action="store_true")
    p_poll.set_defaults(func=cmd_poll)

    p_demo = sub.add_parser("demo", help="用本地 mock 后台跑通全链路（离线、真浏览器）")
    p_demo.add_argument("--workdir", default=None, help="工作目录（默认临时目录）")
    p_demo.add_argument("--channel", default=None, help="浏览器通道，如 chrome")
    p_demo.add_argument("--headless", action="store_true")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    global args_verbose, log_verbose
    args = build_parser().parse_args(argv)
    args_verbose = bool(getattr(args, "verbose", False))
    log_verbose = args_verbose
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
