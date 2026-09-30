"""Общие фикстуры: фабрика токенов и пути к снимкам спецификаций."""

import base64
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = sorted(p for p in (ROOT / "wbmcp" / "specs").iterdir() if p.is_dir())[-1]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def build_token(sid="1234567", acc=3, s=None, t=False, exp=None, **extra):
    """Собрать JWT без настоящей подписи, как это делает WB по форме."""
    if s is None:
        s = sum(1 << bit for bit in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 16))
    payload = {"sid": sid, "s": s, "t": t, "acc": acc, **extra}
    if exp is None:
        exp = int(time.time()) + 180 * 86400
    if exp is not False:
        payload["exp"] = exp
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.{_b64url(b'signature-bytes-here')}"


@pytest.fixture
def make_token():
    return build_token


@pytest.fixture(scope="session")
def specs_dir():
    return SPECS_DIR


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory):
    """Адрес встроенного PostgreSQL (pgserver) на время сессии тестов."""
    import pgserver

    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture(scope="session")
def catalog(specs_dir):
    from wbmcp.catalog import Catalog

    return Catalog.load(specs_dir)
