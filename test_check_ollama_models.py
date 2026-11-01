from __future__ import annotations

import builtins
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

from scripts import check_ollama_models as diagnostic
from orchestrator.core import state

URL = "http://localhost:11434"
REQUESTS = diagnostic.backends.requests
CLI_CODE = compile(
    Path(diagnostic.__file__).read_text(encoding="utf-8"),
    diagnostic.__file__,
    "exec",
)


def response(data=None, *, status=200, text=None, error=None):
    result = mock.Mock()
    result.status_code = status
    result.text = json.dumps(data) if text is None else text
    if error is None:
        result.json.return_value = deepcopy(data)
    else:
        result.json.side_effect = error
    return result


def completion(*, context=65536, thinking=None, capabilities=None):
    data = {
        "capabilities": ["completion"] if capabilities is None else capabilities,
        "details": {"parameter_size": "7B"},
        "model_info": {"example.context_length": context},
    }
    if thinking is not None:
        data["thinking"] = thinking
    return data


class MemoryHTTP:

    def __init__(self):
        self.tags = {"models": []}
        self.shows = {}
        self.calls = []
        self.unexpected = []

    def install(self, models):
        self.tags = {"models": [{"name": name} for name in models]}
        self.shows = deepcopy(models)

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)

    def forbidden(self, *args, **kwargs):
        self.unexpected.append((args, kwargs))
        raise AssertionError("Unregistered HTTP method")

    def _request(self, method, url, kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        try:
            expected = {
                "timeout": diagnostic.DEFAULT_CONFIG["ollama_router_timeout"],
                "allow_redirects": False,
            }
            if method == "GET":
                assert url == f"{URL}/api/tags", url
                assert kwargs == expected, kwargs
                value = self.tags
            else:
                assert method == "POST", method
                assert url == f"{URL}/api/show", url
                payload = kwargs.get("json")
                assert isinstance(payload, dict), payload
                assert set(payload) == {"model"}, payload
                expected["json"] = payload
                assert kwargs == expected, kwargs
                assert payload["model"] in self.shows, payload
                value = self.shows[payload["model"]]
        except AssertionError as exc:
            self.unexpected.append(str(exc))
            raise
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, mock.Mock) else response(value)


def read_only_open(original):
    def guarded(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError(f"File write attempted: {file}")
        return original(file, mode, *args, **kwargs)

    return guarded


class CheckOllamaModelsTests(unittest.TestCase):

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.http = MemoryHTTP()
        self.defaults = deepcopy(diagnostic.DEFAULT_CONFIG)

        for name in ("connect", "connect_ex"):
            self.stack.enter_context(
                mock.patch.object(
                    socket.socket,
                    name,
                    side_effect=AssertionError("Socket connection forbidden"),
                )
            )
        self.stack.enter_context(
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("Socket connection forbidden"),
            )
        )
        self.stack.enter_context(mock.patch.object(REQUESTS, "get", self.http.get))
        self.stack.enter_context(mock.patch.object(REQUESTS, "post", self.http.post))
        self.stack.enter_context(
            mock.patch.object(REQUESTS.sessions.Session, "request", self.http.forbidden)
        )
        for method in ("request", "put", "patch", "delete", "head", "options"):
            self.stack.enter_context(
                mock.patch.object(REQUESTS, method, self.http.forbidden)
            )

        self.stack.enter_context(
            mock.patch.object(builtins, "open", read_only_open(builtins.open))
        )
        self.stack.enter_context(mock.patch.object(io, "open", read_only_open(io.open)))
        original_os_open = os.open

        def guarded_os_open(path, flags, *args, **kwargs):
            forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & forbidden:
                raise AssertionError(f"File write attempted: {path}")
            return original_os_open(path, flags, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(os, "open", guarded_os_open))
        for name in (
            "mkdir",
            "makedirs",
            "remove",
            "unlink",
            "rename",
            "replace",
            "system",
        ):
            self.stack.enter_context(
                mock.patch.object(
                    os, name, side_effect=AssertionError("Mutation forbidden")
                )
            )
        for name in (
            "write_text",
            "write_bytes",
            "touch",
            "mkdir",
            "unlink",
            "rename",
            "replace",
        ):
            self.stack.enter_context(
                mock.patch.object(
                    Path, name, side_effect=AssertionError("Mutation forbidden")
                )
            )
        for name in ("run", "Popen"):
            self.stack.enter_context(
                mock.patch.object(
                    subprocess,
                    name,
                    side_effect=AssertionError("Subprocess forbidden"),
                )
            )
        self.stack.enter_context(
            mock.patch.object(
                sqlite3,
                "connect",
                side_effect=AssertionError("Database access forbidden"),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                state,
                "AgentOrchestrator",
                side_effect=AssertionError("Orchestrator creation forbidden"),
            )
        )

    def tearDown(self):
        self.assertIs(diagnostic.backends.requests, REQUESTS)
        self.assertEqual(diagnostic.DEFAULT_CONFIG, self.defaults)
        self.assertEqual(self.http.unexpected, [])

    def rows(self, report):
        return {row["name"]: row for row in report["models"]}

    def cli(self, *arguments):
        stream = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["check_ollama_models", *arguments]
        ), redirect_stdout(stream):
            with self.assertRaises(SystemExit) as exited:
                exec(CLI_CODE, {"__name__": "__main__"})
            self.assertIs(sys.stdout, stream)
        self.assertIs(diagnostic.backends.requests, REQUESTS)
        return exited.exception.code, stream.getvalue()

    def test_empty_list_is_success(self):
        report = diagnostic.diagnose(URL)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["models"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(self.http.calls), 1)

    def test_reuses_backend_options_and_separates_router(self):
        self.http.install(
            {
                "text:7b": completion(
                    thinking={"values": ["low", "high"], "default": "low"}
                ),
                "vision:3b": completion(
                    context=2048,
                    thinking={"values": [False, True], "default": False},
                    capabilities=["completion", "vision"],
                ),
                "embed:latest": {"capabilities": ["embedding"]},
                "unknown:latest": {},
                "tev1:99b": completion(),
            }
        )
        original = diagnostic.backends.ollama_request_options
        with mock.patch.object(
            diagnostic.backends, "ollama_request_options", wraps=original
        ) as options:
            report = diagnostic.diagnose(URL)

        self.assertEqual(report["exit_code"], 2)
        rows = self.rows(report)
        self.assertEqual(rows["tev1:99b"]["classification"], "router")
        self.assertNotIn("request", rows["tev1:99b"])
        self.assertEqual(rows["embed:latest"]["classification"], "excluded")
        self.assertEqual(rows["unknown:latest"]["classification"], "unclassified")
        self.assertEqual(rows["vision:3b"]["classification"], "completion")
        self.assertEqual(rows["text:7b"]["metadata"]["context_length"], 65536)
        self.assertEqual(
            rows["text:7b"]["request"],
            {"options": {"num_ctx": 9216, "num_predict": 8192}, "think": "high"},
        )
        self.assertEqual(
            rows["vision:3b"]["request"],
            {"options": {"num_ctx": 2048, "num_predict": 1534}, "think": True},
        )
        self.assertEqual(
            {call.args[1] for call in options.call_args_list},
            {"text:7b", "vision:3b"},
        )
        for call in options.call_args_list:
            self.assertEqual(call.args[2], [{"role": "user", "content": "Hello."}])
            self.assertEqual(
                set(call.args[0].config),
                {
                    "ollama_url",
                    "ollama_router_timeout",
                    "ollama_router_model",
                    "ollama_num_ctx",
                    "ollama_num_predict",
                    "ollama_think",
                },
            )
            self.assertEqual(call.args[0].config["ollama_router_model"], "tev1")
        queried = [
            call[2]["json"]["model"] for call in self.http.calls if call[0] == "POST"
        ]
        self.assertCountEqual(queried, self.http.shows)

    def test_thinking_metadata_is_preserved_without_new_compatibility_rules(self):
        cases = [
            (False, None),
            (True, None),
            ("high", None),
            ({"values": [False, True], "default": False}, True),
            ({"values": ["low", "high"], "default": "low"}, "high"),
            ({"values": ["low", "medium"], "default": "medium"}, "medium"),
            ({"values": ["low"], "default": "invalid"}, None),
            ({"future_control": "unknown"}, None),
        ]
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                self.http.install({"text:1": completion(thinking=metadata)})
                report = diagnostic.diagnose(URL)
                row = self.rows(report)["text:1"]
                self.assertEqual(report["exit_code"], 0)
                self.assertEqual(row["metadata"]["thinking"], metadata)
                self.assertEqual(row["show"]["thinking"], metadata)
                self.assertEqual(row["request"]["think"], expected)

    def test_missing_optional_metadata_is_success(self):
        self.http.install({"minimal:1": {"capabilities": ["completion"]}})
        report = diagnostic.diagnose(URL)
        row = self.rows(report)["minimal:1"]
        self.assertEqual(report["exit_code"], 0)
        self.assertNotIn("parameter_size", row["metadata"])
        self.assertNotIn("context_length", row["metadata"])
        self.assertNotIn("thinking", row["metadata"])
        self.assertIsNone(row["request"]["think"])

        code, text = self.cli("--url", URL)
        self.assertEqual(code, 0)
        self.assertIn("參數量：未知", text)
        self.assertIn("模型最大 context：未知", text)
        self.assertIn("不傳送（不代表不支援）", text)

    def test_repeated_diagnosis_refreshes_membership_and_same_name_metadata(self):
        discover = diagnostic.backends.discover_ollama_chat_models
        with mock.patch.object(
            diagnostic.backends, "discover_ollama_chat_models", wraps=discover
        ) as spy:
            self.http.install({"keep:1": completion(), "remove:1": completion()})
            first = diagnostic.diagnose(URL)
            self.http.install(
                {
                    "keep:1": completion(
                        context=4096,
                        thinking={"values": [False], "default": False},
                    ),
                    "added:2": completion(),
                }
            )
            second = diagnostic.diagnose(URL)

        self.assertEqual(set(self.rows(first)), {"keep:1", "remove:1"})
        self.assertEqual(set(self.rows(second)), {"keep:1", "added:2"})
        self.assertEqual(
            self.rows(second)["keep:1"]["metadata"]["context_length"], 4096
        )
        self.assertIs(self.rows(second)["keep:1"]["request"]["think"], False)
        self.assertIsNot(spy.call_args_list[0].args[0], spy.call_args_list[1].args[0])
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs, {"force_refresh": True})
        self.assertEqual(sum(method == "GET" for method, _, _ in self.http.calls), 2)

    def test_two_cli_runs_refresh_models_versions_and_metadata(self):
        self.http.install(
            {
                "keep:1": completion(),
                "removed:1": completion(),
                "tev1:4b": completion(),
            }
        )
        first_code, first_text = self.cli("--url", URL, "--json")
        self.http.install(
            {
                "keep:1": completion(context=2048),
                "added:2": completion(),
                "tev1:123b": completion(),
            }
        )
        second_code, second_text = self.cli("--url", URL, "--json")
        first = self.rows(json.loads(first_text))
        second = self.rows(json.loads(second_text))
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(set(first), {"keep:1", "removed:1", "tev1:4b"})
        self.assertEqual(set(second), {"keep:1", "added:2", "tev1:123b"})
        self.assertEqual(second["keep:1"]["metadata"]["context_length"], 2048)
        for rows, router in ((first, "tev1:4b"), (second, "tev1:123b")):
            self.assertEqual(rows[router]["classification"], "router")
            self.assertNotIn("request", rows[router])

    def test_list_errors_are_json_and_exit_one(self):
        cases = [
            {},
            [],
            None,
            {"models": {}},
            {"models": [None]},
            {"models": [{}]},
            {"models": [{"name": 7}]},
            {"models": [{"name": ""}]},
            REQUESTS.exceptions.ConnectionError("offline"),
            response(status=500, text="server failure"),
            response(status=302, text="redirect forbidden"),
            response(text="{invalid", error=ValueError("invalid JSON")),
        ]
        for failure in cases:
            with self.subTest(failure=repr(failure)):
                self.http.tags = failure
                code, text = self.cli("--url", URL, "--json")
                report = json.loads(text)
                self.assertEqual(code, 1)
                self.assertEqual(report["exit_code"], 1)
                self.assertEqual(report["models"], [])
                self.assertTrue(report["errors"])
                self.assertEqual(report["errors"][0]["scope"], "list")
                if isinstance(failure, mock.Mock):
                    failure.close.assert_called_once()

    def test_invalid_url_and_arguments_are_json_failures(self):
        for arguments in (
            ("--url", "file:///tmp/service", "--json"),
            ("--url", "http://localhost:11434/api/chat", "--json"),
            ("--url", "http://user:password@localhost:11434", "--json"),
            ("--url", "http://localhost:invalid", "--json"),
            ("--url", URL + "?redirect=1", "--json"),
            ("--url", "--json"),
            ("--unknown", "--json"),
        ):
            with self.subTest(arguments=arguments):
                code, text = self.cli(*arguments)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(text)["exit_code"], 1)
        self.assertEqual(self.http.calls, [])

    def test_model_failures_preserve_other_results_and_raw_errors(self):
        cases = [
            [],
            {"capabilities": "completion"},
            {"capabilities": [True]},
            {"capabilities": ["completion"], "details": None},
            {"capabilities": ["completion"], "model_info": []},
            completion(context="invalid"),
            REQUESTS.exceptions.ConnectionError("model connection failed"),
            response(status=503, text="model unavailable"),
            response(status=307, text="model redirect"),
            response(text="broken model JSON", error=ValueError("decode failed")),
        ]
        for failure in cases:
            with self.subTest(failure=repr(failure)):
                self.http.install({"good:1": completion(), "bad:1": completion()})
                self.http.shows["bad:1"] = failure
                code, text = self.cli("--url", URL, "--json")
                report = json.loads(text)
                rows = self.rows(report)
                self.assertEqual(code, 2)
                self.assertEqual(report["exit_code"], 2)
                self.assertEqual(rows["good:1"]["classification"], "completion")
                self.assertIn("request", rows["good:1"])
                self.assertEqual(rows["bad:1"]["classification"], "error")
                self.assertNotIn("request", rows["bad:1"])
                error = next(
                    item for item in report["errors"] if item.get("model") == "bad:1"
                )
                self.assertEqual(error["scope"], "model")
                self.assertTrue(error["message"])
                if isinstance(failure, mock.Mock):
                    self.assertEqual(error["response_text"], failure.text)
                    failure.close.assert_called_once()

    def test_failed_model_recovers_on_next_diagnosis(self):
        self.http.install({"recover:1": completion()})
        self.http.shows["recover:1"] = response(status=500, text="temporary")
        failed = diagnostic.diagnose(URL)
        self.http.shows["recover:1"] = completion(context=4096)
        recovered = diagnostic.diagnose(URL)
        self.assertEqual(failed["exit_code"], 2)
        self.assertEqual(recovered["exit_code"], 0)
        self.assertEqual(recovered["errors"], [])
        self.assertIn("request", self.rows(recovered)["recover:1"])
        self.assertEqual(len(self.http.calls), 4)

    def test_failed_list_recovers_on_next_cli_run(self):
        self.http.tags = REQUESTS.exceptions.ConnectionError("temporary outage")
        failed_code, failed_text = self.cli("--url", URL, "--json")
        self.http.install({"new:1": completion()})
        recovered_code, recovered_text = self.cli("--url", URL, "--json")
        self.assertEqual(failed_code, 1)
        self.assertEqual(json.loads(failed_text)["exit_code"], 1)
        self.assertEqual(recovered_code, 0)
        self.assertEqual(set(self.rows(json.loads(recovered_text))), {"new:1"})

    def test_required_budget_calculation_failure_is_partial(self):
        self.http.install(
            {"too-small:1": completion(context=512), "good:1": completion()}
        )
        report = diagnostic.diagnose(URL)
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("request", self.rows(report)["good:1"])
        self.assertNotIn("request", self.rows(report)["too-small:1"])
        self.assertTrue(
            any(
                error.get("model") == "too-small:1" and error["type"] == "RuntimeError"
                for error in report["errors"]
            )
        )

    def test_router_show_failure_is_partial_without_request_calculation(self):
        self.http.install({"tev1:8b": completion(), "good:1": completion()})
        self.http.shows["tev1:8b"] = response(status=500, text="router metadata")
        with mock.patch.object(
            diagnostic.backends,
            "ollama_request_options",
            wraps=diagnostic.backends.ollama_request_options,
        ) as options:
            report = diagnostic.diagnose(URL)
        self.assertEqual(report["exit_code"], 2)
        row = self.rows(report)["tev1:8b"]
        self.assertEqual(row["classification"], "router")
        self.assertNotIn("request", row)
        self.assertEqual([call.args[1] for call in options.call_args_list], ["good:1"])

    def test_backend_messages_do_not_pollute_json(self):
        original = diagnostic.backends.discover_ollama_chat_models

        def noisy(context, force_refresh=False):
            print("backend diagnostic noise")
            return original(context, force_refresh=force_refresh)

        with mock.patch.object(
            diagnostic.backends, "discover_ollama_chat_models", noisy
        ):
            code, text = self.cli("--url", URL, "--json")
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(report["backend_messages"], ["backend diagnostic noise"])

    def test_requests_and_stdout_restore_after_unexpected_exception(self):
        outside = sys.stdout
        output = io.StringIO()

        def interrupted(context, force_refresh=False):
            self.assertIsNot(diagnostic.backends.requests, REQUESTS)
            print("captured before interrupt")
            raise KeyboardInterrupt()

        with redirect_stdout(output):
            with mock.patch.object(
                diagnostic.backends,
                "discover_ollama_chat_models",
                interrupted,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    diagnostic.diagnose(URL)
            self.assertIs(sys.stdout, output)
            self.assertIs(diagnostic.backends.requests, REQUESTS)
        self.assertIs(sys.stdout, outside)
        self.assertEqual(output.getvalue(), "")

    def test_transport_rejects_unregistered_destinations_paths_and_payloads(self):
        errors = []
        transport = diagnostic._Transport(URL, REQUESTS, errors)
        transport.installed = {"safe:1"}
        for method, url, payload in (
            ("GET", "http://other:11434/api/tags", None),
            ("GET", URL + "/api/chat", None),
            ("POST", URL + "/api/generate", {"model": "safe:1"}),
            ("POST", URL + "/v1/systemone", {"model": "safe:1"}),
            ("POST", URL + "/api/pull", {"model": "safe:1"}),
            ("POST", URL + "/api/delete", {"model": "safe:1"}),
            ("POST", URL + "/api/show", {"model": "unknown:1"}),
            ("POST", URL + "/api/show", {"model": "safe:1", "prompt": "Hello."}),
            ("DELETE", URL + "/api/show", {"model": "safe:1"}),
        ):
            with self.subTest(method=method, url=url, payload=payload):
                with self.assertRaises(ValueError):
                    transport._request(method, url, timeout=10, payload=payload)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(errors), 9)

    def test_http_mock_blocks_direct_sessions(self):
        with mock.patch.object(self.http, "unexpected", []) as unexpected:
            with REQUESTS.Session() as session:
                with self.assertRaisesRegex(AssertionError, "Unregistered HTTP method"):
                    session.get(f"{URL}/api/chat")
            self.assertEqual(len(unexpected), 1)
        self.assertEqual(self.http.calls, [])


class CheckOllamaModelsImportTests(unittest.TestCase):

    def test_import_execution_has_no_requests_or_project_writes(self):
        script = textwrap.dedent("""
            import importlib
            import os
            import sys
            from contextlib import ExitStack
            from unittest import mock

            attempts = []

            def forbidden(operation):
                attempts.append(operation)
                raise AssertionError(f"Import side effect forbidden: {operation}")

            def audit(event, args):
                if event == "open":
                    path, mode, flags = args
                    write_flags = (
                        os.O_WRONLY | os.O_RDWR | os.O_CREAT
                        | os.O_TRUNC | os.O_APPEND
                    )
                    if (mode and any(flag in mode for flag in "wax+")) or (
                        flags & write_flags
                    ):
                        forbidden(f"File write: {path}")
                elif event in {
                    "os.mkdir", "os.remove", "os.rmdir", "os.rename",
                    "os.link", "os.symlink", "os.truncate", "os.chmod",
                    "os.chown", "os.utime", "os.system", "os.fork",
                    "os.forkpty", "os.exec", "os.posix_spawn",
                    "subprocess.Popen", "sqlite3.connect",
                    "socket.connect", "socket.getaddrinfo",
                    "socket.gethostbyname", "socket.gethostbyaddr",
                    "socket.sendto", "socket.sendmsg",
                    "urllib.Request", "http.client.connect", "http.client.send",
                }:
                    forbidden(event)

            sys.addaudithook(audit)
            assert sys.dont_write_bytecode
            assert not any(
                name.split(".")[0] in {"scripts", "orchestrator"}
                for name in sys.modules
            )
            import requests

            with ExitStack() as stack:
                for method in (
                    "request", "get", "post", "put", "patch",
                    "delete", "head", "options",
                ):
                    stack.enter_context(
                        mock.patch.object(
                            requests, method,
                            side_effect=lambda *args, **kwargs: forbidden("HTTP"),
                        )
                    )
                for method in ("request", "send"):
                    stack.enter_context(
                        mock.patch.object(
                            requests.sessions.Session, method,
                            side_effect=lambda *args, **kwargs: forbidden("HTTP"),
                        )
                    )
                diagnostic = importlib.import_module("scripts.check_ollama_models")
                assert callable(diagnostic.diagnose)
                for name in (
                    "orchestrator.core.backends",
                    "orchestrator.core.config",
                    "orchestrator.core.state",
                    "orchestrator.roles.base_agent",
                ):
                    assert name in sys.modules, name
            assert not attempts, attempts
            """)
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parent,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

# Offline tests do not measure real-model speed, quality, or memory usage.
# Risk: requests replacement and stdout capture are process-global; use a standalone CLI.
