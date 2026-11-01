"""Validate runtime Ollama controls without loading models or accessing services."""

from unittest.mock import Mock

import pytest

from orchestrator.core import backends
from test_orchestrator import initialized_orchestrator, no_git, restore_default_config


@pytest.mark.parametrize("model", ["lfm3:1.7b", "future-small-model:v9"])
def test_new_models_use_discovered_boolean_thinking(
    initialized_orchestrator, monkeypatch, model
):
    app = initialized_orchestrator
    monkeypatch.setattr(
        backends.requests,
        "get",
        Mock(
            return_value=Mock(
                status_code=200, json=lambda: {"models": [{"name": model}]}
            )
        ),
    )
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        if url.endswith("/api/show"):
            return Mock(
                status_code=200,
                json=lambda: {
                    "capabilities": ["completion", "tools", "thinking"],
                    "thinking": {"values": [True, False], "default": True},
                    "details": {"parameter_size": "1.7B"},
                    "model_info": {"new_architecture.context_length": 262144},
                },
            )
        assert url.endswith("/api/chat")
        return Mock(status_code=200, json=lambda: {"message": {"content": "ok"}})

    monkeypatch.setattr(backends.requests, "post", post)
    catalog = backends.discover_ollama_chat_models(app, force_refresh=True)
    assert model in catalog
    assert app.call_ollama("small task", model=model) == "ok"
    chats = [payload for url, payload in calls if url.endswith("/api/chat")]
    assert len(chats) == 1
    assert chats[0]["think"] is True
    assert chats[0]["options"]["num_predict"] == 8192
    assert 8192 < chats[0]["options"]["num_ctx"] < 32768


@pytest.mark.parametrize(
    "requested, controls, expected",
    [
        (False, {"values": [True, False], "default": True}, False),
        ("high", {"values": [False], "default": False}, False),
        ("high", {"values": ["low", "high"], "default": "low"}, "high"),
        ("high", {"values": ["normal", "deep"], "default": "normal"}, "normal"),
        (None, {"values": [True, False], "default": True}, None),
        ("high", {}, None),
        ("high", {"values": ["deep"], "default": "invalid"}, None),
    ],
)
def test_thinking_controls_follow_metadata(
    initialized_orchestrator, requested, controls, expected
):
    app = initialized_orchestrator
    app.config["ollama_think"] = requested
    app._ollama_chat_models_cache = (
        0,
        frozenset({"dynamic:v1"}),
        {"dynamic:v1": {"thinking": controls}},
    )
    _, think = backends.ollama_request_options(
        app, "dynamic:v1", [{"role": "user", "content": "test"}]
    )
    assert type(think) is type(expected) and think == expected


def test_context_and_output_respect_host_and_model_limits(initialized_orchestrator):
    app = initialized_orchestrator
    app.config.update(ollama_num_ctx=16384, ollama_num_predict=2048)
    app._ollama_chat_models_cache = (
        0,
        frozenset({"dynamic:v1"}),
        {"dynamic:v1": {"context_length": 8192}},
    )
    short, _ = backends.ollama_request_options(
        app, "dynamic:v1", [{"content": "short"}]
    )
    long, _ = backends.ollama_request_options(
        app, "dynamic:v1", [{"content": "a" * 18000}]
    )
    assert short == {"num_ctx": 4096, "num_predict": 2048}
    assert short["num_ctx"] < long["num_ctx"] <= 8192
    assert 0 < long["num_predict"] < 2048
    app._ollama_chat_models_cache[2]["dynamic:v1"]["context_length"] = 262144
    expanded, _ = backends.ollama_request_options(
        app, "dynamic:v1", [{"content": "a" * 40000}]
    )
    assert expanded["num_ctx"] <= 16384


def test_oversized_input_fails_before_chat(initialized_orchestrator, monkeypatch):
    app = initialized_orchestrator
    app.config["ollama_num_ctx"] = 4096
    chat = Mock()
    monkeypatch.setattr(backends.requests, "post", chat)
    with pytest.raises(RuntimeError, match="Estimated Ollama input"):
        app.call_ollama("中" * 5000, model="dynamic:v1")
    chat.assert_not_called()


@pytest.mark.parametrize("key", ["ollama_num_ctx", "ollama_num_predict"])
def test_unbounded_or_zero_limits_are_rejected(initialized_orchestrator, key):
    initialized_orchestrator.config[key] = -1
    with pytest.raises(ValueError, match="must be positive"):
        backends.ollama_request_options(
            initialized_orchestrator, "dynamic:v1", [{"content": "test"}]
        )
