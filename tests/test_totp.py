"""TOTP 用 RFC 6238 官方测试向量验证——自己实现的算法必须对得起这个标准。

种子是 ASCII "12345678901234567890"，其 base32 形式如下。
"""

from __future__ import annotations

import pytest

from amz_harness.totp import TotpError, mask, normalize_seed, seconds_remaining, totp

RFC_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

# (unix 时间, 期望的 8 位验证码) —— RFC 6238 Appendix B，SHA1 段
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("moment", "expected"), RFC_VECTORS)
def test_rfc6238_vectors(moment: int, expected: str) -> None:
    assert totp(RFC_SEED, at=float(moment), digits=8) == expected


def test_default_is_six_digits() -> None:
    # 8 位向量 94287082 的后 6 位
    assert totp(RFC_SEED, at=59.0) == "287082"
    assert len(totp(RFC_SEED, at=59.0)) == 6


def test_seed_normalization_accepts_real_world_forms() -> None:
    variants = [
        RFC_SEED,
        RFC_SEED.lower(),
        "GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ",
        RFC_SEED.rstrip("="),
    ]
    codes = {totp(v, at=59.0, digits=8) for v in variants}
    assert codes == {"94287082"}


def test_empty_seed_rejected() -> None:
    with pytest.raises(TotpError):
        normalize_seed("   ")


def test_invalid_base32_rejected() -> None:
    with pytest.raises(TotpError):
        totp("!!!not-base32!!!", at=59.0)


def test_unknown_algorithm_rejected() -> None:
    with pytest.raises(TotpError):
        totp(RFC_SEED, at=59.0, algorithm="md5")


def test_seconds_remaining_within_period() -> None:
    for moment in (0.0, 1.0, 29.9, 59.0, 100.5):
        assert 0.0 < seconds_remaining(at=moment) <= 30.0


def test_mask_never_leaks_seed() -> None:
    secret = "GEZDGNBVGY3TQOJQ"
    shown = mask(secret)
    assert secret not in shown
    assert "已配置" in shown
    assert "未配置" in mask(None)
