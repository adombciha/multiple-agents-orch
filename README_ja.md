# Multi-Agent Orchestrator

[English](README.md) | [繁體中文](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md) | [Русский](README_ru.md)

計画、実装、QA、コードレビュー、最終報告を決定論的な状態機械で行う Python のマルチエージェント開発ワークフローです。

## 要件とインストール

shell で次のコマンドを実行します。デフォルトの Git worktree workflow には Git が必要で、Python 3、`requests`、`litellm` が必要です。テストには `pytest` を使用します。

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py --help
```

## ワークフローとファイル

`start` は request を保存し、state を `PLANNING` に reset します。通常の state flow は次のとおりです。

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

PM は要件を分析して任意の specialists を配置します。その結果は Architect の plan review に渡されます。RD はタスクを実装し、QA は設定済みの test command を実行して結果を報告します。Reviewer は要件、計画、tests、specialist の結果、Git diff を評価し、Assistant は完了時に `CHANGELOG.md` を作成します。

`init` は `.ai-company/` を作成します。これには `config.json`、`state.json`、`request.md`、`requirements.json`、プログラムが生成する人間向けの `requirements.md`、`implementation_plan.md`、`action_items.json`、agent outputs、`test_results.txt`、`qa_report.md`、`reviewer_output.json`、`reviewer_output.md`、`specialist_reviews.md`、`human_report.md`、`final_report.md` が含まれます。機械間のデータ交換には可能な限り JSON を使い、人間向けの出力には Markdown を使います。

## 設定

`.ai-company/config.json` を編集する前に `python3 orchestrator.py init` を実行してください。存在しない key には `orchestrator/core/config.py` のデフォルト値が使用されます。

| Key | 用途とデフォルト動作 |
| --- | --- |
| `ollama_url`, `ollama_model` | Ollama endpoint とデフォルト model（`http://localhost:11434`、`gemma4:latest`）。 |
| `test_command`, `max_revisions` | QA command と自動 plan/code revision の上限。デフォルトは `python3 -m pytest -q` と `2` です。 |
| `backends` | roles に割り当てる backend。 |
| `model_tiers`, `role_models`, `role_model_backends`, `role_model_tiers` | model のリストと role ごとの model/backend routing。 |
| `use_ponytail`, `use_worktree` | 簡潔な prompting と Git-worktree isolation。デフォルトはそれぞれ `false` と `true` です。 |
| `backend_escalation_path`, `staffing_limits` | backend escalation と RD/QA staffing の上限。 |
| `telemetry_enabled`, `llm_usage_log` | LiteLLM の token metadata 記録と JSONL path。デフォルトは workflow directory の `llm_usage.jsonl` で、prompt/output は保存しません。 |

## 自動モデル選択

Codex の `auto` は CLI の推奨モデルを使用します。AGY の `auto:gemini-flash-medium` は `agy models` から最新の Gemini Flash Medium を選択し、一覧を 1 時間キャッシュします。`auto:gemini-flash-low`、`auto:gemini-pro-low`、`auto:claude-sonnet-medium`、`auto:claude-opus-medium` も使用できます。Gemini 3.8 Flash Low、3.7 Flash Low/Medium、3.1 Pro、Claude Sonnet、Opus のモデル ID は明示指定できます。削除したのは独立した Claude Code と Grok backend であり、AGY 内の Claude モデルではありません。

```bash
agy models
```

### ローカル Ollama の動的モデルルーティング

ローカル Ollama の実行では、`tev1:4b` を Ollama の `/v1/systemone` endpoint 経由の判断専用ルーターとして使用します（`/api/chat` のタスク実行モデルとしては使用しません）。
- **モデルの検出**：設定済み Ollama endpoint の `/api/tags` からインストール済みモデルを動的に検出します。モデルの pull や削除は行いません。
- **プライバシー**：候補選択には role、短いタスク要約、候補モデルの説明のみを送信し、完全な task prompt はルーターに送信しません。
- **検証と fallback**：選択結果、confidence、インストール済み候補それぞれの確率を検証し、確率の高い順にローカルモデルを試します。低 confidence は記録しますが、有効な順位付けは使用します。endpoint／ルーターが利用できない場合や応答が無効な場合は、ローカルモデルの固定順序を使用します。
- **ローカル限定**：動的 routing は Ollama 内で完結し、Codex と AGY の route は変更しません。

## 自動 review と human review

Automatic review と owner review は異なる result 名と transition を使用します。

| 生成元 | Result | 効果 |
| --- | --- | --- |
| QA | `PASSED` | `TESTING` から `REVIEWING_CODE` へ進みます。 |
| QA | `FAILED` | QA fix task を作成して `IMPLEMENTING` に戻ります。`max_revisions` 到達時は `WAITING_FOR_OWNER` で停止します。 |
| Reviewer | `APPROVED` | 完了まで進みます。 |
| Reviewer | `REJECTED` | reviewer fix task を作成して `IMPLEMENTING` に戻ります。`max_revisions` 到達時は `WAITING_FOR_OWNER` で停止します。 |
| Architect | plan revision | `max_revisions` まで計画を修正し、その後は human review を作成せず `IMPLEMENTING` に進みます。 |

`max_revisions` のデフォルトは `2` ですが、実際の値は現在の `.ai-company/config.json` が制御します。QA と Reviewer の失敗は `code_revisions` を増やし、retry limit 到達が owner review を作成する条件です。AI backend failure、test-command の非ゼロ exit status、その他の command error は execution failure です。owner による明示的な `reject` decision とは異なります。

`WAITING_FOR_OWNER` では `approve` または `review` を使用します。

| Decision | 意味 | 後続の動作 |
| --- | --- | --- |
| `pass` | 保存済みの通過パスを受け入れます。 | 記録済みの `pass_state` から再開し、必要なら `--run` で続行します。 |
| `revise` | もう一度の実装を要求します。 | `code_revisions` を増やし、`HUMAN-REVIEW-N` を追加して `IMPLEMENTING` に移動します。以前の automatic retry threshold には妨げられません。 |
| `reject` | この workflow 結果を拒否します。 | `FAILED` に移動し、通常の実行は停止します。 |

`approve` は停止中の通過パス用のショートカットです。成功した `review` output は次の内容で識別できます。

```text
Human review '<decision>' recorded; workflow moved to <STATE>.
```

現在の state と revision counts は `python3 orchestrator.py status` で確認します。`--run` は選択した decision の後に実行可能な workflow が残る場合だけ loop を続け、`reject` の後は実行しません。

## CLI リファレンス

現在の command list は `python3 orchestrator.py --help` で確認します。以下の command はすべて repository root から実行します。`init`、`start`、`--help` 以外は初期化済みの `.ai-company/` directory が必要です。

| コマンド | 構文 | 必須 arguments | 任意 arguments / デフォルト | 出力、前提条件、例 |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`, `--help` | commands を出力して終了します。`python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | `.ai-company/config.json` と state files を作成します。`python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | 初期化して request を保存し、`PLANNING` に reset します。agents は実行しません。`python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | state-machine step を 1 回実行します。選択した AI CLIs、Ollama、Git worktree support が必要になる場合があります。`python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | `COMPLETED`、`WAITING_FOR_OWNER`、`FAILED` まで実行します。`python3 orchestrator.py run` |
| `status` | `python3 orchestrator.py status` | None | None | state、revision counters、選択済み backends、test command、action items を出力します。`python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`: approval 後に続行、デフォルトは off | `WAITING_FOR_OWNER` でのみ有効です。記録済み state から再開します。`python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run]` | `decision`: `pass`、`revise`、`reject` | `--run`: `pass` または `revise` 後に続行、デフォルトは off | `WAITING_FOR_OWNER` でのみ有効です。recorded-decision message を出力します。`python3 orchestrator.py review revise --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`: デフォルトは `PLANNING`、string state value を受理 | revision/runtime routing data を消去し、worktree を merge せず削除して、選択した state を保存します。`python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | config の `backends` を更新します。roles: `manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends: `ollama`、`codex`、`agy`。`developer` と `qa` は senior/middle/junior routes を更新します。`python3 orchestrator.py set-backend reviewer agy` |

各 subcommand も `--help` をサポートします。例: `python3 orchestrator.py review --help`。無効な role/backend/decision の値は argparse に拒否されます。`.ai-company/` がない場合、`status`、`approve`、`review`、`reset`、`set-backend` は初期化が必要だと報告します。`approve` と `review` は `WAITING_FOR_OWNER` 以外の state も拒否します。

### よく使う CLI の手順

最小限のローカル設定とタスク:

```bash
python3 orchestrator.py init
python3 orchestrator.py start "Add contact search and tests"
python3 orchestrator.py step
python3 orchestrator.py status
```

通常の自動 workflow を実行します（設定済み backends と、デフォルトでは Git が必要です）。

```bash
python3 orchestrator.py run
```

サポートされる backend を設定するか、workflow 実行前に AGY モデルを選択します。

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json to pin role_model_routes.ra[0][1] if needed.
agy models
```

停止した review を処理します。

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## 自動 weighting と role-play staffing

このシステムは internal staff（Ollama）と有料 vendor（Codex、Agy）の実際の performance に基づき、trust weight と voting influence を調整します。

### Staff と vendor

- **Internal staff**: `ollama` backend に routed された `developer_senior/middle/junior` と `qa_senior/middle/junior` roles は、蓄積する trust weight（`worker_weights`）を持つ無料の長期 staff として扱われます。
- **Paid vendors**：`codex` 和 `agy` CLI backends 由 `vendor_scores` 独立跟踪。

### Trust weight と scoring event

- 各 role（`rd`/`qa`）x level（`senior`/`middle`/`junior`）には、`0.2` から `3.0` の trust weight があり、`worker_weights` の値がデフォルトになります。
- task completion、QA pass/fail、Reviewer approve/reject event は EWMA（exponentially weighted moving average）update を発生させ、時間とともに weight を少しずつ調整します。vendor は代わりに独自の scorecard を更新するため、両者が互いに干渉することはありません。

### QA weighted voting

- QA は unanimous agreement を必要としなくなりました。各 level の vote は trust weight で重み付けされ、weighted ratio が `qa_vote_threshold`（デフォルト `0.6`）に達すると run は pass します。test-command の non-zero exit code は、vote にかかわらず run を直ちに fail させます。
- weighted majority が minority の FAILED vote を覆した場合、その dissent は QA report に記録され、Reviewer に second check として提示されます。後で human が QA failure を覆した場合、FAILED と投票した level(s) は false positive として penalty を受けます。

### Manager performance review

- 各 run の終了時に、Manager はその run の telemetry に基づいて、`weight_adjustment_max_delta`（デフォルト `0.3`）以内の小さな（±）weight adjustment を提案します。adjustment は legal range に clamp され、自動的に config に書き戻されます。Vendor scorecard は informational only であり、この review では変更されません。

### Daily roster

- task の staffing 前に、Manager は当日の roster を読み取ります。そこには、どの internal staff role が online か（Ollama に reach 可能か）、どの vendor が利用可能か、各自の current weight または rating が含まれます。
- assigned level が offline の場合、task は最も近い available level に自動 reassignment されます。他の条件が同等なら、より高い weight/rating が優先されます。

### Vendor firefighting

- internal staff が task で継続的に苦戦している場合（revision が少なくなった、trust weight が低下しすぎた、または Ollama に reach できない場合）、システムは `backend_escalation_path` から最も高く rating された利用可能な vendor に task を自動的に引き継がせ、firefight させます。これは deterministic な decision であり、別の model call ではありません。

### Internal company memory（brain）

- Internal staff（Ollama backend のみ）は、failure の lessons と meeting summary を記録する sandbox 化された SQLite + FTS5 の「brain」database にアクセスできます。これらは RAG を使用して関連する future prompt に注入されます。Vendor prompt がこの memory を見ることはないため、company knowledge が sandbox の外に出ることはありません。
- この database に write するのは固定された code hook だけです。model output が直接書き込まれることはなく、prompt injection による long-term memory の汚染を防ぎます。

### New CLI commands

| Command | Description |
| --- | --- |
| `weights` | 現在の internal staff trust weight を表示します。 |
| `weights set <role> <level> <value>` | weight を手動で上書きします。 |
| `roster` | 今日利用可能な RD/QA role（staff と vendor）を表示します。 |
| `vendors` | paid-vendor scorecard を表示します。 |
| `brain search <query>` | internal company memory を検索します。 |
| `brain prune [--keep N]` | 古い memory を削除し、最新の N 件だけを保持します。 |

### New configuration keys

`worker_weights`、`qa_vote_threshold`、`weight_adjustment_max_delta`、`vendor_scores`、`vendor_hire_min_jobs`、`brain_db`（デフォルトは `orchestrator/core/config.py` を参照）。

## テストと検証

repository root から test dependencies をインストールし、利用可能な checks を実行します。

```bash
python3 -m pip install requests pytest
python3 -m pytest -q
python3 -m pytest -q test_orchestrator.py
python3 -m pytest -q test_orchestrator.py::test_initialized_orchestrator_fixture_loads_temp_config_and_state
python3 verify_alignment.py
git diff --check
```

tests は mocks、fixtures、temporary directories を使用し、外部 AI 認証は不要です。pytest 成功時の終了コードは `0` です。`git diff --check` で基本的な空白を確認できます。

実際の workflow 実行には、選択した backend CLI と login、`ollama` roles 用にアクセス可能な Ollama server、`use_worktree` が true の場合の Git worktree support が必要です。対象プロジェクトに別の command が必要な場合は、`.ai-company/config.json` の `test_command` で test commands を変更できます。

## 範囲と制限

- Specialist selection is dynamic; no CLI command forces RA or Sales for a task.
- AGY auto selectors require `agy models` to succeed; explicit model IDs stay pinned.
- `set-backend` supports only the documented roles and backend values.
- `reset --state` は string を受け取ります。意味のある作業を再開するには有効な workflow states を使用してください。
- AI workflow commands は対象プロジェクトの Git worktree を変更する可能性があり、provider usage または料金が発生することがあります。

## Future Direction

This project aims to evolve from a deterministic multi-agent orchestration framework into an AI organization simulator capable of dynamically forming engineering teams, allocating responsibilities, and governing autonomous collaboration.
