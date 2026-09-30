"""Разбор токена WB."""

from datetime import UTC, datetime, timedelta

import pytest

from wbmcp.tokens import (
    READ_ONLY_BIT,
    SPEC_CATEGORY,
    Category,
    TokenFormatError,
    TokenType,
    decode_token,
    short_token,
)


def test_decode_full_personal_token(make_token):
    info = decode_token(make_token(sid="42", acc=3))
    assert info.sid == "42"
    assert info.token_type is TokenType.PERSONAL
    assert Category.MARKETPLACE in info.categories
    assert Category.FINANCE in info.categories
    assert not info.read_only
    assert not info.sandbox
    assert not info.is_expired()
    assert info.days_left() in (179, 180)


@pytest.mark.parametrize("acc,expected", [
    (1, TokenType.BASIC), (2, TokenType.TEST), (3, TokenType.PERSONAL), (4, TokenType.SERVICE),
])
def test_token_types(make_token, acc, expected):
    assert decode_token(make_token(acc=acc)).token_type is expected


def test_read_only_and_sandbox_flags(make_token):
    scopes = (1 << Category.CONTENT) | (1 << READ_ONLY_BIT)
    info = decode_token(make_token(s=scopes, t=True, acc=1))
    assert info.read_only
    assert info.sandbox
    assert info.categories == frozenset({Category.CONTENT})
    assert info.allows(Category.CONTENT)
    assert not info.allows(Category.MARKETPLACE)
    assert info.allows(None)


def test_expiry(make_token):
    past = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    info = decode_token(make_token(exp=past))
    assert info.is_expired()
    assert info.days_left() < 0
    no_exp = decode_token(make_token(exp=False))
    assert no_exp.expires_at is None
    assert not no_exp.is_expired()
    assert no_exp.days_left() is None


def test_summary_has_no_token(make_token):
    token = make_token(sid="7")
    summary = decode_token(token).summary()
    assert summary["sid"] == "7"
    assert summary["token_type"] == "personal"
    assert "Маркетплейс" in summary["categories"]
    assert token not in str(summary)


@pytest.mark.parametrize("bad", [
    "", "abc", "a.b", "a.b.c.d",
    "x.!!!.y",
    "x." + "e30".replace("0", "0") + ".y",  # {} без sid
])
def test_bad_tokens(bad):
    with pytest.raises(TokenFormatError):
        decode_token(bad)


def test_unknown_acc_and_bad_scopes(make_token):
    with pytest.raises(TokenFormatError):
        decode_token(make_token(acc=9))
    with pytest.raises(TokenFormatError):
        decode_token(make_token(acc="3"))
    with pytest.raises(TokenFormatError):
        decode_token(make_token(s="1"))


def test_bool_exp_is_ignored(make_token):
    assert decode_token(make_token(exp=True)).expires_at is None


def test_spec_categories_cover_all_bits():
    assert set(c for c in SPEC_CATEGORY.values() if c is not None) == set(Category)
    assert SPEC_CATEGORY["commonapi"] is None


def test_short_token():
    assert short_token("short") == "***"
    assert short_token("abcdefghijklmnopqrstuvwxyz") == "abcd...wxyz"


def test_test_token_allows_everything(make_token):
    info = decode_token(make_token(acc=2, s=0, t=True))
    assert info.categories == frozenset() and info.allows(Category.MARKETPLACE)
    assert info.summary()["categories"] == "все методы песочницы"
    basic = decode_token(make_token(acc=1, s=0))
    assert not basic.allows(Category.MARKETPLACE) and basic.summary()["categories"] == []
