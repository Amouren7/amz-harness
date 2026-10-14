"""amz-harness：多账号亚马逊卖家后台自动化骨架。

设计上分两层，边界很重要：

  纯逻辑层（零第三方依赖，可离线单测）
      sites / models / config / profiles / session / scheduler / readers

  驱动层（唯一依赖 Playwright 的地方）
      driver.playwright_driver

这样做的理由：真实站点联调必须人工看着跑，但**判定逻辑**不该依赖浏览器。
把状态机、限流、轮询排程抽成纯函数，就能在 CI 里用 mock 页面反复验证，
而不是等到线上才发现"重试逻辑写反了"。
"""

__version__ = "0.1.0"
