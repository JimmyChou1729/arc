from __future__ import annotations

import json

import httpx
import pytest

from arc_paper.providers._http import create_http_client
from arc_paper.providers.base import ProviderError


def test_inherited_socks5h_client_initialization_needs_no_request(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "socks5h://127.0.0.1:1")
    def forbidden(*args, **kwargs):
        raise AssertionError("client construction must not make a network request")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    with create_http_client(timeout=5) as client:
        assert client.trust_env is True


def test_missing_socks_dependency_is_typed_without_proxy_credentials(monkeypatch):
    def missing(**kwargs):
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
    monkeypatch.setattr(httpx, "Client", missing)
    with pytest.raises(ProviderError) as raised:
        create_http_client()
    assert raised.value.code == "proxy_dependency_missing"
    assert "fresh runtime" in str(raised.value)


def test_unrelated_import_error_is_not_misclassified(monkeypatch):
    def missing(**kwargs):
        raise ImportError("unrelated dependency")
    monkeypatch.setattr(httpx, "Client", missing)
    with pytest.raises(ImportError, match="unrelated"):
        create_http_client()


def test_injected_offline_transport_ignores_inherited_proxy_routes(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    with create_http_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"fixture": True}))) as client:
        assert client.get("https://example.invalid").json() == {"fixture": True}


def test_paper_cli_exposes_proxy_dependency_failure(monkeypatch, capsys):
    from arc_paper.cli import main
    def missing(**kwargs):
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
    monkeypatch.setattr(httpx, "Client", missing)
    assert main(["get-metadata", "arXiv:0911.3380"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "proxy_dependency_missing"
