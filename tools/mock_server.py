"""thin wrapper：真正的实现在 amz_harness.mockserver（这样 CLI 与测试都能 import）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amz_harness.mockserver import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
