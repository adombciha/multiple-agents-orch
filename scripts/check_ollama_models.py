"""Read-only Ollama metadata and request-budget diagnostics."""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from orchestrator.core import backends
from orchestrator.core.config import DEFAULT_CONFIG


def _error(errors, scope, exc, model=None, **details):
    entry = {
        "scope": scope,
        "type": type(exc).__name__,
        "message": str(exc),
        **details,
    }
    if model is not None:
        entry["model"] = model
    errors.append(entry)


def _base_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in url)
    ):
        raise ValueError("URL 必須是無帳密、查詢字串及路徑的 HTTP(S) 服務位址")
    parsed.port
    return url.rstrip("/")


def _validate_tags(data):
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("清單回應必須包含 models 陣列")
    for model in data["models"]:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("name"), str)
            or not model["name"].strip()
        ):
            raise ValueError("models 每個項目必須包含非空字串 name")


def _validate_show(data):
    if not isinstance(data, dict):
        raise ValueError("模型回應必須是 JSON 物件")
    if "capabilities" in data and (
        not isinstance(data["capabilities"], list)
        or any(not isinstance(value, str) for value in data["capabilities"])
    ):
        raise ValueError("capabilities 必須是字串陣列")
    for key in ("details", "model_info"):
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"{key} 必須是 JSON 物件")
    for key, value in data.get("model_info", {}).items():
        if key.endswith(".context_length") and value is not None:
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} 必須是正整數或 null")


class _Transport:
    """Restrict discovery to tags and show while retaining swallowed failures."""

    def __init__(self, url, client, errors):
        self.url = url
        self.client = client
        self.errors = errors
        self.tags = None
        self.tags_valid = False
        self.installed = set()
        self.shows = {}

    def get(self, url, *, timeout):
        return self._request("GET", url, timeout=timeout)

    def post(self, url, *, json, timeout):
        return self._request("POST", url, timeout=timeout, payload=json)

    def _request(self, method, url, *, timeout, payload=None):
        model = payload.get("model") if isinstance(payload, dict) else None
        scope = "list" if method == "GET" else "model"
        response = None
        response_text = None
        status_code = None
        try:
            try:
                if method == "GET":
                    if url != f"{self.url}/api/tags" or payload is not None:
                        raise ValueError("禁止未登記的清單請求")
                    response = self.client.get(
                        url, timeout=timeout, allow_redirects=False
                    )
                elif method == "POST":
                    if (
                        url != f"{self.url}/api/show"
                        or not isinstance(payload, dict)
                        or set(payload) != {"model"}
                        or not isinstance(model, str)
                        or model not in self.installed
                    ):
                        raise ValueError("禁止未登記的模型查詢")
                    response = self.client.post(
                        url,
                        json=payload,
                        timeout=timeout,
                        allow_redirects=False,
                    )
                else:
                    raise ValueError(f"禁止 HTTP 方法：{method}")

                status_code = response.status_code
                response_text = response.text
                if status_code != 200:
                    raise RuntimeError(f"HTTP {status_code}：{response_text}")
                data = response.json()
                if method == "GET":
                    self.tags = data
                    _validate_tags(data)
                    self.installed = {item["name"] for item in data["models"]}
                else:
                    self.shows[model] = data
                    _validate_show(data)
            finally:
                if response is not None:
                    response.close()
        except Exception as exc:
            _error(
                self.errors,
                scope,
                exc,
                model,
                status_code=status_code,
                response_text=response_text,
            )
            raise

        if method == "GET":
            self.tags_valid = True
        return SimpleNamespace(status_code=200, json=lambda: data)


def _report(url):
    return {
        "url": url,
        "exit_code": 1,
        "tags": None,
        "models": [],
        "errors": [],
        "backend_messages": [],
    }


def diagnose(url):
    """Inspect a service without constructing or persisting an orchestrator."""
    report = _report(url)
    try:
        base = _base_url(url)
        config = {
            key: deepcopy(DEFAULT_CONFIG[key])
            for key in (
                "ollama_router_timeout",
                "ollama_num_ctx",
                "ollama_num_predict",
                "ollama_think",
            )
        }
        selector = DEFAULT_CONFIG["ollama_router_model"].rsplit(":", 1)[0]
        config.update(ollama_url=base, ollama_router_model=selector)
    except Exception as exc:
        _error(report["errors"], "list", exc)
        return report

    context = SimpleNamespace(config=config)
    transport = _Transport(base, backends.requests, report["errors"])
    captured = io.StringIO()

    # shortcut: these patches are process-global; use a dedicated CLI process.
    with redirect_stdout(captured), patch.object(backends, "requests", transport):
        try:
            catalog = backends.discover_ollama_chat_models(context, force_refresh=True)
            if not isinstance(catalog, dict):
                raise TypeError("既有探索未回傳模型字典")
        except Exception as exc:
            catalog = {}
            _error(report["errors"], "discovery", exc)

        if transport.tags_valid:
            for name in sorted(transport.installed):
                router = backends._ollama_model_matches(selector, {name})
                row = {
                    "name": name,
                    "classification": "router" if router else "error",
                }
                report["models"].append(row)

                if router:
                    try:
                        transport.post(
                            f"{base}/api/show",
                            json={"model": name},
                            timeout=config["ollama_router_timeout"],
                        )
                    except Exception:
                        pass

                row["show"] = transport.shows.get(name)
                if any(error.get("model") == name for error in report["errors"]):
                    continue

                data = row["show"]
                if data is None:
                    _error(
                        report["errors"],
                        "model",
                        RuntimeError("既有探索未取得此模型的查詢結果"),
                        name,
                    )
                    continue
                if router:
                    continue
                if "capabilities" not in data:
                    row["classification"] = "unclassified"
                    _error(
                        report["errors"],
                        "model",
                        ValueError("缺少 capabilities，無法判定文字執行能力"),
                        name,
                    )
                    continue

                if name in catalog:
                    row["classification"] = "completion"
                    row["metadata"] = catalog[name]
                    try:
                        options, think = backends.ollama_request_options(
                            context,
                            name,
                            [{"role": "user", "content": "Hello."}],
                        )
                        row["request"] = {"options": options, "think": think}
                    except Exception as exc:
                        _error(report["errors"], "model", exc, name)
                elif "completion" not in data["capabilities"]:
                    row["classification"] = "excluded"
                else:
                    _error(
                        report["errors"],
                        "model",
                        RuntimeError("服務宣告 completion，但既有探索未回傳此模型"),
                        name,
                    )

    report["tags"] = transport.tags
    report["backend_messages"] = captured.getvalue().splitlines()
    report["exit_code"] = (
        1 if not transport.tags_valid else 2 if report["errors"] else 0
    )
    return report


def _value(value):
    if value is None:
        return "未知"
    return json.dumps(value, ensure_ascii=False)


def _print_text(report):
    print(f"Ollama 服務：{report['url']}")
    labels = {
        "completion": "一般文字執行模型",
        "router": "路由模型",
        "excluded": "排除：明確非 completion",
        "unclassified": "未分類：能力未知",
        "error": "查詢失敗",
    }
    for row in report["models"]:
        print(f"\n模型：{row['name']}（{labels[row['classification']]}）")
        if row["classification"] == "completion":
            metadata = row["metadata"]
            print(f"  參數量：{_value(metadata.get('parameter_size'))}")
            print(f"  模型最大 context：{_value(metadata.get('context_length'))}")
            print(f"  thinking metadata：{_value(metadata.get('thinking'))}")
            request = row.get("request")
            if request is not None:
                print(f"  此次 num_ctx：{request['options']['num_ctx']}")
                print(f"  此次 num_predict：{request['options']['num_predict']}")
                think = request["think"]
                print(
                    "  此次 think："
                    + ("不傳送（不代表不支援）" if think is None else _value(think))
                )
        elif row["classification"] == "router":
            print("  僅供路由；不計算文字執行請求")
            print(f"  原始 metadata：{_value(row['show'])}")
        else:
            print(f"  原始 metadata：{_value(row['show'])}")

    if not report["models"] and report["exit_code"] == 0:
        print("已安裝模型清單為空。")
    for error in report["errors"]:
        target = error.get("model", "清單／探索")
        print(f"錯誤 [{target}] {error['type']}：{error['message']}")
    if report["backend_messages"]:
        print("\n既有探索訊息：")
        for message in report["backend_messages"]:
            print(message)
    print(f"\n診斷退出碼：{report['exit_code']}")


class _Parser(argparse.ArgumentParser):

    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _Parser(description="唯讀查詢 Ollama 模型與既有請求預算")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    try:
        args = parser.parse_args(arguments)
    except ValueError as exc:
        report = _report(None)
        _error(report["errors"], "list", exc)
        as_json = "--json" in arguments
    else:
        report = diagnose(args.url)
        as_json = args.json

    if as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        _print_text(report)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
