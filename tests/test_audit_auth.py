"""Аудит вызовов и авторизация MCP-клиентов."""

import pytest
from mcp.server.auth.provider import AccessToken

from wbmcp.audit import AuditLog, args_hash, redact_secrets
from wbmcp.auth import (
    LOCAL_PRINCIPAL,
    AccessDeniedError,
    ClientPrincipal,
    ClientRegistry,
    StaticTokenVerifier,
    principal_from_access_token,
    required_scopes,
)
from wbmcp.config import ClientConfig


async def test_audit_record_and_stats(tmp_path, make_token):
    log = AuditLog(tmp_path / "a" / "audit.db")
    token = make_token()
    rec = await log.record(client="c", seller="main", sid="1", tool="wb_request", operation="op",
                           arguments={"token": token}, outcome="error", status=401,
                           error=f"bad {token} here", waited_s=0.12345, elapsed_s=1.0)
    assert rec.error == "bad <token> here" and rec.waited_s == 0.123
    assert rec.args_hash == args_hash({"token": token})
    await log.record(client="c", seller="other", sid="2", tool="wb_ping", operation="ping",
                     arguments={}, outcome="ok", status=200)
    recent = await log.recent()
    assert [r.tool for r in recent] == ["wb_ping", "wb_request"]
    assert (await log.recent(seller="main"))[0].outcome == "error"
    assert recent[0].as_dict()["client"] == "c"
    stats = await log.stats()
    assert stats["calls"] == 2 and stats["by_outcome"] == {"error": 1, "ok": 1}
    assert stats["by_seller"]["other"] == 1 and stats["waited_total_s"] == 0.123
    assert redact_secrets("") == "" and redact_secrets("plain") == "plain"
    assert (await AuditLog(":memory:").stats())["calls"] == 0


def test_principal_checks():
    p = ClientPrincipal("a", frozenset({"read"}), frozenset({"main"}))
    assert p.allows_seller("main") and not p.allows_seller("x")
    assert p.has_scopes(["read"]) and not p.has_scopes(["read", "write"])
    with pytest.raises(AccessDeniedError):
        p.check_seller("x")
    with pytest.raises(AccessDeniedError) as info:
        p.check_scopes(["write", "admin"])
    assert "admin, write" in str(info.value)
    assert LOCAL_PRINCIPAL.allows_seller("anything") and LOCAL_PRINCIPAL.has_scopes(["admin"])


def test_required_scopes(catalog):
    assert required_scopes(catalog.by_route("GET", "/ping")) == {"read"}
    assert required_scopes(catalog.by_route("POST", "/api/v3/supplies")) == {"write"}
    finance = next(op for op in catalog if op.category == "finance")
    assert "finance" in required_scopes(finance)
    users = next(op for op in catalog if op.category == "usermanagement")
    assert "admin" in required_scopes(users)


async def test_registry_and_verifier():
    registry = ClientRegistry(
        [ClientConfig(name="agent", key="secret-1", sellers=["main"], scopes=["read", "write"]),
         ClientConfig(name="all", key_env="K", scopes=["read"])],
        env={"K": "secret-2"},
    )
    assert len(registry) == 2
    assert registry.by_key("secret-1").name == "agent"
    assert registry.by_key("secret-2").sellers is None
    assert registry.by_key("nope") is None
    verifier = StaticTokenVerifier(registry)
    token = await verifier.verify_token("secret-1")
    assert token.client_id == "agent" and token.scopes == ["read", "write"]
    assert await verifier.verify_token("zzz") is None
    assert principal_from_access_token(token, registry).name == "agent"
    assert principal_from_access_token(None, registry) is LOCAL_PRINCIPAL
    assert principal_from_access_token(token, None) is LOCAL_PRINCIPAL
    with pytest.raises(AccessDeniedError):
        principal_from_access_token(AccessToken(token="gone", client_id="x", scopes=[]), registry)
    with pytest.raises(ValueError):
        ClientRegistry([ClientConfig(name="bad", key="k", scopes=["root"])])
