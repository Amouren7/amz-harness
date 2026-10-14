"""TOTP（RFC 6238），只用标准库。

为什么自己实现而不引 pyotp：这东西一共 25 行，而它是**能用就全自动、
不能用就得叫人**的关键路径。依赖越少越好，而且带 RFC 测试向量可以验证正确性。

安全提醒：种子等同于密码。不要写进配置文件、不要进 git、不要打日志。
本模块的 `mask()` 用于安全地展示"配没配"。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time

_DIGITS = 6
_PERIOD = 30
_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


class TotpError(ValueError):
    pass


def normalize_seed(seed: str) -> str:
    """接受 'JBSW Y3DP' / 'jbswy3dp' / 带 '=' 的形态，统一成合法的 base32。"""
    cleaned = "".join(seed.split()).replace("-", "").upper()
    if not cleaned:
        raise TotpError("TOTP 种子为空")
    padding = "=" * (-len(cleaned) % 8)
    return cleaned + padding


def totp(
    seed: str,
    *,
    at: float | None = None,
    digits: int = _DIGITS,
    period: int = _PERIOD,
    algorithm: str = "sha1",
) -> str:
    """算出当前（或指定时刻的）验证码。

    `at` 注入是为了测试——真实调用不传即可。
    """
    try:
        key = base64.b32decode(normalize_seed(seed), casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise TotpError(f"TOTP 种子不是合法 base32：{exc}") from None

    algo = _ALGOS.get(algorithm.lower())
    if algo is None:
        raise TotpError(f"不支持的算法 {algorithm!r}；可用：{', '.join(sorted(_ALGOS))}")

    moment = time.time() if at is None else at
    counter = int(moment // period)
    digest = hmac.new(key, struct.pack(">Q", counter), algo).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def seconds_remaining(*, at: float | None = None, period: int = _PERIOD) -> float:
    """当前验证码还剩多少秒。

    实用细节：只剩 2 秒就别提交了，等下一轮——否则填进去的码在
    页面提交完成前就过期了，白跑一次还多一次登录尝试。
    """
    moment = time.time() if at is None else at
    return period - (moment % period)


def mask(seed: str | None) -> str:
    """日志里安全展示种子状态。"""
    if not seed:
        return "(未配置)"
    return f"(已配置, {len(seed)} 字符)"
