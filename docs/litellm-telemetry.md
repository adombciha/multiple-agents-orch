# LiteLLM Telemetry and LLM Usage Log

This project records lightweight metadata for LLM calls. The implementation is
in `orchestrator/core/telemetry.py`, and `orchestrator/core/state.py` calls it
for both successful and failed backend callbacks. It uses LiteLLM's
`token_counter` when the package and selected model support it. The log is
intended for workflow accounting and troubleshooting, not for capturing
conversations. `summary_markdown` reads the same JSONL file and reports calls,
failures, and token totals grouped by model.

## `telemetry_enabled`

`telemetry_enabled` is the configuration switch for usage recording. Its
default is `true` in `orchestrator/core/config.py`.

Set it in `.ai-company/config.json`:

```json
{
  "telemetry_enabled": true
}
```

Set it to `false` to disable recording:

```json
{
  "telemetry_enabled": false
}
```

The value is checked at the start of `record_call`. When disabled, the
function returns without token counting, directory creation, or file I/O.
When enabled, the normal cost is one input token-counting attempt, one output
token-counting attempt when output exists, and one JSONL append. A telemetry
file or directory write failure is ignored so telemetry cannot change workflow
behavior.

## `llm_usage_log`

`llm_usage_log` selects the log filename or path below the workflow's
`.ai-company` directory. Its default is `llm_usage.jsonl`.

```json
{
  "llm_usage_log": "metrics/llm_usage.jsonl"
}
```

The effective path is `.ai-company/<llm_usage_log>` for a relative value. A
configured absolute `Path` is used as an absolute path by Python path joining.
Parent directories are created automatically. Each record is appended as one
UTF-8 JSON object followed by a newline; existing records are not rewritten.

The log is related to telemetry as its only current output: disabling
`telemetry_enabled` prevents both token counting and writes to this path.

For a successful callback, an entry is written with the elapsed time and
returned output length. If the callback raises, `state.py` calls `record_call`
with `output: null` and the exception before re-raising it; the entry therefore
has `success: false`, zero output characters, and an error category. The log
represents attempted calls, not only successful calls.

## `llm_usage.jsonl` field catalog

Every record contains all fields below. A field whose value is `null` is still
present; this is how unavailable values are represented.

| Name | Type / format | Optional or required | Meaning | Example |
| --- | --- | --- | --- | --- |
| `timestamp` | string, timezone-aware ISO 8601 | Required | UTC time at which the record is created. | `"2026-07-14T03:21:45.123456+00:00"` |
| `role` | string | Required | Workflow role that made the call. | `"developer_junior"` |
| `backend` | string | Required | Backend selected for the call. | `"ollama"` |
| `model` | string or `null` | Required; value nullable | Model name supplied to the call. | `"gemma4:latest"` |
| `success` | boolean | Required | `true` when the callback returned; `false` when it raised. | `true` |
| `error_category` | string or `null` | Required; value nullable | Classified failure: `timeout`, `quota`, `contract`, an exception class name, or `null` on success. | `null` |
| `elapsed_ms` | number | Required | Callback duration in milliseconds, rounded to one decimal place. | `842.7` |
| `input_characters` | integer | Required | Length of the prompt plus the system prompt, in characters. | `1280` |
| `output_characters` | integer | Required | Length of returned output, or zero when there is no output. | `3560` |
| `input_tokens` | integer or `null` | Required; value nullable | Count for system and user input, or provider-reported input count. | `412` |
| `output_tokens` | integer or `null` | Required; value nullable | Count for assistant output, or provider-reported output count. | `986` |
| `total_tokens` | integer or `null` | Required; value nullable | Sum of available input and output counts. | `1398` |
| `token_status` | string | Required | How the token values were obtained. See below. | `"exact"` |
| `image_count` | integer | Required | Number of image paths supplied to the call. | `1` |

Prompt, system prompt, output text, image bytes, and their contents are not
fields in this record.

## `token_status` values

| Value | Condition |
| --- | --- |
| `exact` | LiteLLM is importable and `token_counter` successfully counts the configured model and messages. |
| `unsupported_model` | LiteLLM is available, but `token_counter` raises while counting the model/messages. The affected count is `null`; if another count succeeded, the status is still changed to this value. |
| `litellm_unavailable` | Importing LiteLLM failed. Token counts are unavailable. |
| `provider` | `provider_usage` was supplied as a dictionary. Provider `prompt_tokens`/`input_tokens` and `completion_tokens`/`output_tokens` take precedence, including `null` when the provider omits them. |

`total_tokens` is computed when at least one input or output count is
available, treating a missing side as zero for the sum. It is otherwise
`null`.

## Privacy limits and compliance

The log records metadata and lengths only. It does not record prompt text,
system-prompt text, model output, image data, or image paths. Character counts
can still reveal approximate message size, and role, backend, model, timing,
success, error category, and token counts may be operationally sensitive.

The implementation does not redact, hash, encrypt, rotate, expire, or delete
usage records. It also does not inspect content for secrets or personal data.
Do not treat the JSONL file as a compliance boundary: protect the workflow
directory with normal filesystem permissions, apply the organization's
retention and access rules, and assess whether metadata itself is personal or
confidential data. If policy requires stronger controls, implement them in the
storage and retention environment rather than assuming telemetry has removed
all sensitive information.

## Installation and enablement

1. Install the project dependencies from the repository root:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

   `requirements.txt` includes `litellm>=1.92,<2`. The telemetry module treats
   LiteLLM as optional at runtime for token counting, but without it records
   use `token_status: "litellm_unavailable"` and contain no token counts.

2. Create the workflow configuration if needed:

   ```bash
   .venv/bin/python orchestrator.py init
   ```

3. In `.ai-company/config.json`, leave telemetry enabled or configure the log
   path explicitly:

   ```json
   {
     "telemetry_enabled": true,
     "llm_usage_log": "llm_usage.jsonl"
   }
   ```

4. Run the workflow. The file is created under `.ai-company/` only after the
   first attempted LLM call when telemetry is enabled.

There is no telemetry-specific environment variable in this project. Backend
authentication and service settings are separate: the repository does not
define an extra Grok API-key environment variable, and Ollama uses the
configured `ollama_url` rather than a telemetry environment variable.
