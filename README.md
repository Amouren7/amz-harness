# amz-harness

多账号亚马逊卖家后台自动化的**骨架**：环境启动、会话健康状态机、站点轮询切换、只读采集。

定位很明确：**这是地基，不是成品。** 它把你入职后要反复踩的那几类坑（多账号串号、
登录态失效、风控闸门、限流、切换后没落到目标站点）先用代码钉死，
真实站点联调时你只需要处理"选择器对不对"这一件事。

```
166 项测试通过（含真开 Chromium 打本地 mock 后台的端到端）
```

---

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 每个账号一套隔离持久化浏览器环境（profile + 代理 + 稳定指纹） | ❌ 不写操作：不调价、不改 listing、不提交任何表单 |
| 登录编排：密码 + TOTP/人工 2FA | ❌ 不绕过验证码、不伪装指纹 |
| 会话健康状态机 + 重试策略 | ❌ 不保证不被风控盯上（那是用量和 IP 质量决定的） |
| 站点切换并**校验**是否真落到目标域名 | ❌ 不内置真实亚马逊后台的选择器（必须你校准） |
| 只读采集（订单/库存）+ 增量去重 | ❌ 不存密码、不存种子到配置文件 |
| 并发护栏：同账号串行、全局限流、每小时上限 | |

---

## 快速开始

### 0. 先解决这个坑（很重要）

如果你在境内，`pip` 默认源可能是清华镜像，而它**可能返回 403**，症状是
`No matching distribution found for <任何包>`——看起来像"Python 版本不兼容"，
实际是源不通：

```powershell
# 验证源是否可用
Invoke-WebRequest https://pypi.tuna.tsinghua.edu.cn/simple/playwright/ -UseBasicParsing
#   403 -> 源不可用；改用官方源
Invoke-WebRequest https://pypi.org/simple/playwright/ -UseBasicParsing
#   200 -> 官方源可用
```

**结论：装依赖时显式指定官方源。** Python 3.14 本身没问题，
playwright 1.63 + greenlet 3.5.6 已有 `cp314` wheel。

```powershell
python -m venv .venv
.venv\Scripts\pip install -i https://pypi.org/simple -e ".[dev]"
.venv\Scripts\python -m playwright install chromium
```

### 1. 跑通（不需要任何真实账号）

```powershell
.venv\Scripts\python -m amz_harness demo --headless
```

这条命令会：起本地 mock 卖家后台 → 真开 Chromium → 环境启动 → 登录 → 2FA →
切站点并校验 → 只读采集订单/库存 → 打印增量 → 验证验证码闸门判成"转人工"。
去掉 `--headless` 可以看着它跑。

```powershell
# 离线跑一轮轮询，并自证并发护栏
.venv\Scripts\python -m amz_harness poll -c config/demo.toml --kinds orders,inventory

# 自检（Playwright / 配置 / 代理连通性）
.venv\Scripts\python -m amz_harness doctor -c config/demo.toml

# 看 21 个站点目录
.venv\Scripts\python -m amz_harness sites
```

### 2. 接真实账号前

```powershell
.venv\Scripts\python -m amz_harness init          # 生成 config/harness.toml
# 改邮箱 / 站点 / 代理；密码和种子用环境变量注入
setx AMZ_PASSWORD_SHOP_US_MAIN "..."
setx AMZ_TOTP_SHOP_US_MAIN "..."
.venv\Scripts\python -m amz_harness doctor
.venv\Scripts\python -m amz_harness env-up
.venv\Scripts\python -m amz_harness poll --live --kinds orders
```

> **第一次务必人工登一次。** 手动跑 `env-up` 后，用同一个 profile 目录人工打开浏览器
> 登录一遍、过完 2FA 并勾选"信任此设备"。之后这套 profile 才是"已登录"状态，
> 自动化才能接上——否则每次都在跟 2FA 较劲。

---

## 模块地图

```
src/amz_harness/
├─ sites.py          21 个 marketplace 目录（域名/语言/时区/marketplace_id）
├─ models.py         Account / Proxy / PageProbe / SessionState / Marker / Flag
├─ config.py         TOML 配置（tomllib，零依赖）+ ${ENV} 密钥引用
├─ profiles.py       环境启动：隔离 profile、指纹锚定、代理绑定、漂移检测
├─ session.py        ⭐ 会话健康状态机（纯函数）+ 重试策略
├─ flows.py          登录编排 + 人工兜底通道（HumanGate）
├─ switch.py         站点切换 + 切换后校验 + 轮转计划
├─ readers.py        只读采集：多语言表头映射 + 增量去重
├─ htmltable.py      标准库 HTML 表格抽取
├─ scheduler.py      ⭐ 并发护栏：同账号串行 / 全局限流 / 每小时上限
├─ observability.py  JSONL 事件日志 + URL 脱敏
├─ totp.py           RFC 6238（带官方测试向量）
├─ mockserver.py     本地 mock 卖家后台
├─ cli.py            命令行
└─ driver/
   ├─ base.py                驱动契约 + 选择器表（⚠️ 校准点在注释里）
   ├─ fake.py                离线驱动（CI 用）
   └─ playwright_driver.py   真实浏览器（唯一依赖 Playwright 的地方）
```

**核心架构决定：判定逻辑与浏览器解耦。**

`classify(probe) -> Verdict` 是纯函数，只吃一个页面快照；驱动负责把 DOM 翻译成
语义标记。这样状态机、限流、轮转、采集管线都能在 CI 里穷举测试，
真机联调时只剩"选择器对不对"一个变量。

---

## 几条刻意的设计决定（这些是这活儿的真正难点）

### 1. 硬闸门永不自动重试

验证码 / 2FA / 账号锁定，**任何情况下都不许自动重试**。

```python
@property
def is_gate(self) -> bool:
    return self in (SessionState.NEEDS_2FA, SessionState.NEEDS_CAPTCHA, SessionState.BLOCKED)
```

对着验证码重试，等于告诉风控"我是脚本"。把"需要人"写成正常分支，
而不是 `except: retry(3)`——这是把账号从"被验证一次"救回"被挂起"的关键。

### 2. 指纹锚定在"账号归属站点"，不是"当前访问的站点"

同一账号从 US 切到 DE 时，`locale` / `timezone` **不许跟着变**。
一个"美国时区、美国语言"的会话突然变成德语区，本身就是异常信号。

### 3. 视口用 `hashlib` 派生，不用内置 `hash()`

CPython 的 `str` hash 带进程随机盐，换个进程就变，
会造成"同一账号每次启动视口都不同"——这是真会咬人的坑，已用测试钉住。

### 4. 账号没配代理 → 拒绝启动，不静默降级

多账号共用出口 IP 是这类项目出事的头号原因。要单账号本机调试，
必须显式 `--allow-no-proxy` / `require_proxy = false`。

### 5. "登录态"与"账号风险提示"分开建模

登录成功 + 账号健康告警（listing 被下架）必须能同时成立。
把两者塞进同一个枚举，就会漏报。

### 6. 密码不进配置文件，URL 进日志前自动脱敏

密码只从环境变量/凭据库经 `CredentialProvider` 取，`Account` 里根本没有
`password` 字段。日志里任何 `*url` 字段自动打码敏感查询参数——
真实后台的 URL 里经常带 token，而日志是最容易被随手转发出去的东西。

### 7. 切换站点必须**校验**，不是"加载成功就算成功"

两件事同时验：落到目标域名 + 仍是已登录态。否则后面采集会拿到一堆登录页，
还查不出原因。

### 8. 单个作业失败不中断整轮

一个站点挂了，绝不能让其他站点也不采了。

---

## ⚠️ 必须你自己校准的部分（诚实清单）

| 位置 | 现状 | 怎么校准 |
|---|---|---|
| `driver/base.py` 的 `MARKER_SELECTORS` | 只有登录页那几个 id 是长期稳定的；`data-marker` 那类是给自建/mock 用的通用约定 | 存下真实页面 HTML，跑 `probe --html page.html` |
| `driver/base.py` 的 `SIGNIN_*` / `MFA_*` | 亚马逊登录页长期稳定的 id，但仍会变 | 同上 |
| `readers.py` 的字段别名 | 覆盖中/英/德/日几种常见列头，**不可能全** | `probe` 会告诉你哪个字段没匹配上、实际列头是什么 |
| `sites.py` 的 `marketplace_id` | SP-API 口径常量 | `GET /sellers/v1/marketplaceParticipations` 核对 |
| `site_switcher.mode = "dropdown"` | 用"站点码/域名"文本定位下拉项，**没猜后台菜单的中文/德文名** | 自己看一遍真实下拉项 |

```powershell
# 校准入口
.venv\Scripts\python -m amz_harness probe --html 存下来的页面.html
```

---

## 风险与边界

- **亚马逊卖家协议对自动化访问、多账号操作有明确限制。** 技术风险可控，
  **业务责任风险才是这活真正的雷**。
- 入职前建议问清三件事：
  1. 这套东西操作的是**自有店铺**还是**客户店铺**？
  2. 出事了（账号被验证/受限）**责任怎么算**？
  3. 有没有**代理/指纹浏览器采购预算**？没有的话，"多账号"这个需求本身就不成立。
- 本骨架**只读**。要加写操作（调价/改 listing）之前，先把
  dry-run + 回读校验 + 幂等补上——那是另一个量级的风险。

---

## 开发

```powershell
.venv\Scripts\python -m pytest              # 全部（含真浏览器端到端）
.venv\Scripts\python -m pytest -m "not e2e" # 只想跑纯逻辑（毫秒级）
```

`.gitignore` 已经排除 `profiles*/` 和 `runs*/`：
**profile 目录里有登录 cookie，绝不能提交进 git。**
