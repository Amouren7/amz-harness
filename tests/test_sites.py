from __future__ import annotations

from dataclasses import replace

import pytest

from amz_harness.sites import REGION_LABELS, SITES, all_codes, get_site, region_of


def test_catalog_size_and_uniqueness() -> None:
    assert len(SITES) == 21
    domains = [s.domain for s in SITES.values()]
    assert len(set(domains)) == len(domains), "域名必须唯一"
    ids = [s.marketplace_id for s in SITES.values()]
    assert len(set(ids)) == len(ids), "marketplace_id 必须唯一"
    for site in SITES.values():
        assert site.region in REGION_LABELS
        assert site.locale and "-" in site.locale
        assert "/" in site.timezone


def test_lookup_is_case_insensitive() -> None:
    assert get_site("us") is get_site("US")
    assert get_site(" us ") is get_site("US")


def test_unknown_code_raises_with_usable_message() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_site("ZZ")
    message = str(excinfo.value)
    assert "ZZ" in message
    assert "US" in message, "报错要告诉调用方有哪些合法值"


def test_urls_follow_scheme_and_domain() -> None:
    us = get_site("US")
    assert us.home_url == "https://sellercentral.amazon.com/"
    assert us.signin_url == "https://sellercentral.amazon.com/ap/signin"

    mock = replace(us, scheme="http", domain="127.0.0.1:8848")
    assert mock.home_url == "http://127.0.0.1:8848/"
    assert mock.signin_url == "http://127.0.0.1:8848/ap/signin"


def test_known_marketplace_ids() -> None:
    assert get_site("US").marketplace_id == "ATVPDKIKX0DER"
    assert get_site("JP").marketplace_id == "A1VC38T7YXB528"
    assert get_site("DE").marketplace_id == "A1PA6795UKMFR9"


def test_region_grouping() -> None:
    grouped = region_of(["US", "CA", "DE", "JP", "FR"])
    assert grouped["na"] == ["US", "CA"]
    assert sorted(grouped["eu"]) == ["DE", "FR"]
    assert grouped["fe"] == ["JP"]


def test_all_codes_sorted() -> None:
    assert all_codes() == sorted(SITES)
