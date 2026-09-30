"""Настройки сервера."""

from pathlib import Path

import pytest

from wbmcp.config import (
    ClientConfig,
    ConfigError,
    SellerConfig,
    find_specs_dir,
    load_config,
)


def test_defaults_without_file_and_env():
    config = load_config(env={})
    assert config.sellers == []
    assert config.server.mode == "local"
    assert config.server.profiles == ["core"]


def test_default_seller_from_env():
    config = load_config(env={"WB_TOKEN": "x.y.z"})
    assert [s.alias for s in config.sellers] == ["default"]
    assert config.sellers[0].resolve_token({"WB_TOKEN": "x.y.z"}) == "x.y.z"


def test_load_file(tmp_path):
    file = tmp_path / "wb-mcp.toml"
    file.write_text(
        '[server]\nname = "shop"\nprofiles = ["core", "orders"]\n'
        '[[sellers]]\nalias = "main"\ntoken_env = "T_MAIN"\nnote = "основной"\n'
        '[[sellers]]\nalias = "second"\ntoken = "a.b.c"\n'
        '[[clients]]\nname = "agent"\nkey_env = "K1"\nsellers = ["main"]\n'
        'scopes = ["read", "write"]\n',
        encoding="utf-8",
    )
    config = load_config(file, env={"T_MAIN": "m.a.in", "K1": "key1"})
    assert config.server.name == "shop"
    assert config.server.profiles == ["core", "orders"]
    assert config.sellers[0].resolve_token({"T_MAIN": "m.a.in"}) == "m.a.in"
    assert config.sellers[1].resolve_token({}) == "a.b.c"
    assert config.clients[0].resolve_key({"K1": "key1"}) == "key1"
    assert config.clients[0].sellers == ["main"]


def test_config_path_from_env(tmp_path):
    file = tmp_path / "c.toml"
    file.write_text('[server]\nname = "from-env"\n', encoding="utf-8")
    assert load_config(env={"WBMCP_CONFIG": str(file)}).server.name == "from-env"


def test_missing_and_broken_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml", env={})
    broken = tmp_path / "broken.toml"
    broken.write_text("[server\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(broken, env={})


def test_duplicate_aliases(tmp_path):
    file = tmp_path / "d.toml"
    file.write_text(
        '[[sellers]]\nalias = "a"\ntoken = "x"\n[[sellers]]\nalias = "a"\ntoken = "y"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(file, env={})


def test_seller_token_sources(tmp_path):
    with pytest.raises(ConfigError):
        SellerConfig(alias="a").resolve_token({})
    with pytest.raises(ConfigError):
        SellerConfig(alias="a", token_env="E").resolve_token({})
    with pytest.raises(ConfigError):
        SellerConfig(alias="a", token_file=str(tmp_path / "no")).resolve_token({})
    token_file = tmp_path / "t.txt"
    token_file.write_text("f.i.le\n", encoding="utf-8")
    assert SellerConfig(alias="a", token_file=str(token_file)).resolve_token({}) == "f.i.le"
    token_file.write_text("# заметка\n\nfirst.to.ken\nsecond.to.ken\n", encoding="utf-8")
    assert SellerConfig(alias="a", token_file=str(token_file)).resolve_token({}) == "first.to.ken"
    token_file.write_text("# только заметка\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        SellerConfig(alias="a", token_file=str(token_file)).resolve_token({})


def test_client_key_sources():
    assert ClientConfig(name="c", key=" k ").resolve_key({}) == "k"
    with pytest.raises(ConfigError):
        ClientConfig(name="c", key_env="E").resolve_key({})
    with pytest.raises(ConfigError):
        ClientConfig(name="c").resolve_key({})


def test_find_specs_dir(tmp_path, specs_dir):
    assert find_specs_dir(None, env={}) == specs_dir
    assert find_specs_dir(specs_dir, env={}) == specs_dir
    assert find_specs_dir(None, env={"WBMCP_SPECS": str(specs_dir)}) == specs_dir
    with pytest.raises(ConfigError):
        find_specs_dir(tmp_path / "missing", env={})


def test_find_specs_dir_without_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr("wbmcp.config.PACKAGED_SPECS", tmp_path)
    with pytest.raises(ConfigError, match="не найдены"):
        find_specs_dir(None, env={})


def test_data_dir_expands_home():
    from wbmcp.config import ServerConfig

    assert "~" not in str(ServerConfig().resolved_data_dir())
    assert Path(ServerConfig().resolved_data_dir()).is_absolute()


def test_data_dir_from_env(tmp_path):
    config = load_config(env={"WBMCP_DATA_DIR": str(tmp_path / "vol")})
    assert config.server.data_dir == tmp_path / "vol"
    assert config.server.resolved_data_dir() == tmp_path / "vol"
