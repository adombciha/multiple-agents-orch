"""Verify endpoint detection and recovery without contacting model services."""

import copy
import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from orchestrator.core import backends, ollama_endpoint
from orchestrator.core.config import DEFAULT_CONFIG


@pytest.fixture
def app(tmp_path):
    return SimpleNamespace(
        config=copy.deepcopy(DEFAULT_CONFIG), state={}, ai_dir=tmp_path
    )


def test_loopback_is_preferred_and_cached_without_reading_gateway(app, monkeypatch):
    probe = Mock(return_value=True)
    gateway = Mock(side_effect=AssertionError("Gateway should not be needed"))
    monkeypatch.setattr(ollama_endpoint, "available", probe)
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", gateway)

    assert ollama_endpoint.resolve(app) == "http://localhost:11434"
    assert ollama_endpoint.resolve(app) == "http://localhost:11434"
    probe.assert_called_once_with("http://localhost:11434")
    gateway.assert_not_called()
    assert app.config["ollama_url"] == "auto"
    assert not list(app.ai_dir.iterdir())


@pytest.mark.parametrize("gateway", ["192.0.2.10", "192.0.2.20"])
def test_auto_uses_the_current_gateway(app, monkeypatch, gateway):
    probe = Mock(side_effect=[False, True])
    monkeypatch.setattr(ollama_endpoint, "available", probe)
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: gateway)

    assert ollama_endpoint.resolve(app) == f"http://{gateway}:11434"
    assert [call.args[0] for call in probe.call_args_list] == [
        "http://localhost:11434",
        f"http://{gateway}:11434",
    ]
    assert app.config["ollama_url"] == "auto"


@pytest.mark.parametrize(
    "output, expected",
    [
        ("default via 192.0.2.20 dev eth0 proto kernel\n", "192.0.2.20"),
        ("default dev eth0\n", None),
        ("default via invalid dev eth0\n", None),
        ("default via\n", None),
        ("", None),
    ],
)
def test_gateway_is_read_from_the_live_route(monkeypatch, output, expected):
    run = Mock(return_value=SimpleNamespace(stdout=output))
    monkeypatch.setattr(ollama_endpoint.subprocess, "run", run)

    assert ollama_endpoint.get_windows_host_ip() == expected
    assert run.call_args.args[0] == ["ip", "-4", "route", "show", "default"]
    assert run.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("ip", 2)]
)
def test_gateway_lookup_failure_is_bounded(monkeypatch, error):
    monkeypatch.setattr(ollama_endpoint.subprocess, "run", Mock(side_effect=error))
    assert ollama_endpoint.get_windows_host_ip() is None


@pytest.mark.parametrize(
    "status, data, expected",
    [
        (200, {"models": []}, True),
        (200, {}, False),
        (200, [], False),
        (302, {"models": []}, False),
        (500, {"models": []}, False),
    ],
)
def test_probe_requires_an_ollama_models_response(monkeypatch, status, data, expected):
    response = Mock(status_code=status)
    response.json.return_value = data
    get = Mock(return_value=response)
    monkeypatch.setattr(ollama_endpoint.requests, "get", get)

    assert ollama_endpoint.available("http://localhost:11434") is expected
    assert get.call_args.kwargs["timeout"] == 2
    assert get.call_args.kwargs["allow_redirects"] is False
    response.close.assert_called_once()


@pytest.mark.parametrize("error", [requests.Timeout(), ValueError("Invalid JSON")])
def test_probe_failure_is_not_available(monkeypatch, error):
    monkeypatch.setattr(ollama_endpoint.requests, "get", Mock(side_effect=error))
    assert ollama_endpoint.available("http://localhost:11434") is False


def test_unavailable_candidates_fail_without_an_inference(app, monkeypatch):
    probe = Mock(return_value=False)
    post = Mock(side_effect=AssertionError("No inference is allowed"))
    monkeypatch.setattr(ollama_endpoint, "available", probe)
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(ollama_endpoint.requests, "post", post)

    with pytest.raises(requests.ConnectionError, match="No Ollama endpoint detected"):
        ollama_endpoint.resolve(app)
    assert probe.call_count == 2
    post.assert_not_called()


def test_explicit_url_does_not_probe_or_fall_back(app, monkeypatch):
    app.config["ollama_url"] = "https://custom.example/ollama/"
    probe = Mock(side_effect=AssertionError("Explicit URL must be preserved"))
    monkeypatch.setattr(ollama_endpoint, "available", probe)
    send = Mock(side_effect=requests.ConnectionError("offline"))

    assert ollama_endpoint.resolve(app) == "https://custom.example/ollama"
    with pytest.raises(requests.ConnectionError):
        ollama_endpoint.request(app, send, "https://custom.example/ollama/api/tags")
    send.assert_called_once_with("https://custom.example/ollama/api/tags")
    probe.assert_not_called()


def test_metadata_failure_redetects_gateway_and_clears_model_cache(app, monkeypatch):
    gateways = iter(["192.0.2.10", "192.0.2.20"])
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: next(gateways))
    monkeypatch.setattr(
        ollama_endpoint, "available", lambda url: "localhost" not in url
    )
    assert ollama_endpoint.resolve(app) == "http://192.0.2.10:11434"
    app._installed_ollama_models_cache = (time.monotonic(), {"old-model"})
    app._ollama_chat_models_cache = (0, frozenset({"old-model"}), {"old-model": {}})
    app._tev1_routing_cache = {"old": ["old-model"]}
    app._ollama_no_think_models = {"old-model"}
    response = Mock(status_code=200)
    response.json.return_value = {"models": [{"name": "new-model"}]}
    send = Mock(side_effect=[requests.ConnectionError("old gateway"), response])
    monkeypatch.setattr(backends.requests, "get", send)

    assert backends.discover_installed_ollama_models(app, force_refresh=True) == {
        "new-model"
    }
    assert [call.args[0] for call in send.call_args_list] == [
        "http://192.0.2.10:11434/api/tags",
        "http://192.0.2.20:11434/api/tags",
    ]
    assert not hasattr(app, "_ollama_chat_models_cache")
    assert not hasattr(app, "_tev1_routing_cache")
    assert not hasattr(app, "_ollama_no_think_models")
    assert app.config["ollama_url"] == "auto"


def test_inference_timeout_invalidates_endpoint_without_replaying(app, monkeypatch):
    monkeypatch.setattr(
        ollama_endpoint, "available", lambda url: "localhost" not in url
    )
    gateway = Mock(side_effect=["192.0.2.10", "192.0.2.20"])
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", gateway)
    base = ollama_endpoint.resolve(app)
    send = Mock(side_effect=requests.ReadTimeout("inference interrupted"))

    with pytest.raises(requests.ReadTimeout):
        ollama_endpoint.request(app, send, f"{base}/api/chat", timeout=3)
    assert send.call_count == 1
    assert not hasattr(app, "_resolved_ollama_url")
    assert ollama_endpoint.resolve(app) == "http://192.0.2.20:11434"


@pytest.mark.parametrize("model", ["test:1", "gpt-oss:20b"])
def test_chat_and_gpt_use_the_resolved_endpoint(app, monkeypatch, model):
    monkeypatch.setattr(
        ollama_endpoint, "available", lambda url: "localhost" not in url
    )
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: "192.0.2.20")
    response = Mock(status_code=200)
    response.json.return_value = {
        "message": {
            "content": json.dumps({"files": [{"path": "app.py", "content": "ok"}]})
        }
    }
    send = Mock(return_value=response)
    monkeypatch.setattr(backends.requests, "post", send)

    assert backends.call_ollama(app, "write code", model=model)
    assert send.call_args.args[0] == "http://192.0.2.20:11434/api/chat"
    assert send.call_args.kwargs["json"]["model"] == model
    assert app.config["ollama_url"] == "auto"


def test_router_uses_the_resolved_endpoint(app, monkeypatch):
    monkeypatch.setattr(
        ollama_endpoint, "available", lambda url: "localhost" not in url
    )
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: "192.0.2.20")
    tags = Mock(status_code=200)
    tags.json.return_value = {
        "models": [{"name": name} for name in ["d3:4b", "a:1", "b:1"]]
    }
    monkeypatch.setattr(backends.requests, "get", Mock(return_value=tags))
    response = Mock(status_code=200)
    response.json.return_value = {
        "answers": {
            "selected_model": {
                "choice": "a:1",
                "confidence": 0.8,
                "probabilities": {"a:1": 0.8, "b:1": 0.2},
            }
        }
    }
    send = Mock(return_value=response)
    monkeypatch.setattr(backends.requests, "post", send)

    assert backends.route_ollama_model_with_tev1(app, "qa", ["a:1", "b:1"]) == [
        "a:1",
        "b:1",
    ]
    assert send.call_args.args[0] == "http://192.0.2.20:11434/v1/systemone"


def test_metadata_from_different_gateways_is_not_mixed(app, monkeypatch):
    gateways = iter(["192.0.2.10", "192.0.2.20"])
    monkeypatch.setattr(ollama_endpoint, "get_windows_host_ip", lambda: next(gateways))
    monkeypatch.setattr(
        ollama_endpoint, "available", lambda url: "localhost" not in url
    )
    tags = []
    for name in ["old-model", "new-model"]:
        response = Mock(status_code=200)
        response.json.return_value = {"models": [{"name": name}]}
        tags.append(response)
    monkeypatch.setattr(backends.requests, "get", Mock(side_effect=tags))
    metadata = Mock(status_code=200)
    metadata.json.return_value = {"capabilities": ["completion"]}
    post = Mock(
        side_effect=[requests.ConnectionError("old gateway"), metadata, metadata]
    )
    monkeypatch.setattr(backends.requests, "post", post)

    assert backends.discover_ollama_chat_models(app) == {}
    assert not hasattr(app, "_ollama_chat_models_cache")
    assert list(backends.discover_ollama_chat_models(app)) == ["new-model"]


def test_changing_config_discards_the_previous_endpoint_and_catalog(app, monkeypatch):
    monkeypatch.setattr(ollama_endpoint, "available", Mock(return_value=True))
    ollama_endpoint.resolve(app)
    app._installed_ollama_models_cache = (time.monotonic(), {"old-model"})
    app.config["ollama_url"] = "http://explicit.example:11434"

    assert ollama_endpoint.resolve(app) == "http://explicit.example:11434"
    assert not hasattr(app, "_resolved_ollama_url")
    assert not hasattr(app, "_installed_ollama_models_cache")
