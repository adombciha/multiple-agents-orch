# Multi-Agent Orchestrator

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

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

## Grok specialists について

Grok は PM、Architect、Reviewer、RA、Sales が利用できる外部 CLI backend です。`orchestrator/core/grok.py` が Grok CLI mode、JSON envelope の抽出、single-turn 制限を一元管理します。PM、Architect、Reviewer は strict JSON machine output を使用し、Python が人間向け Markdown を生成します。Developer が Grok に routed された場合は plan、subagent、memory、web search、tools を無効化し、指定された file/section blocks だけを返します。RA と Sales は通常の research mode を維持します。

workflow がいずれかの role を選ぶ前に、動作し認証済みの `grok` CLI をインストールしてください。プロジェクトが使用する直接の interface は次のとおりです。

```bash
grok -p "<prompt>" -m grok-4.5
```

設定でサポートされる Grok model は `grok-4.5` です。`.ai-company/config.json` で `backends.ra` / `backends.sales`、`role_models.ra` / `role_models.sales`、`role_model_backends.ra` / `role_model_backends.sales`、`role_model_tiers.ra` / `role_model_tiers.sales`、`model_tiers.grok` を使い backend と model を設定します。role model は `role_model_tiers`、`role_models`、`grok-4.5` の順に解決されます。

`set-backend` では `grok` backend を直接設定できます。Grok が利用できない場合は、その role の `role_model_routes` に従って fallback します。strict JSON contract の失敗時は、別の有料 model を呼び出さず停止します。RA output は model review であり、検証済みの法律調査ではありません。specialist は引き続き PM が task に応じて選択します。

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
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`、`backend` | None | config の `backends` を更新します。roles: `manager`、`architect`、`developer`、`reviewer`、`qa`、`assistant`、`ra`、`security`、`sales`、`sre`。backends: `ollama`、`codex`、`claude`、`agy`、`grok`。`developer` と `qa` は senior/middle/junior routes を更新します。`python3 orchestrator.py set-backend reviewer agy` |

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

サポートされる backend を設定するか、workflow 実行前に config entries を編集して specialist 用の Grok を設定します。

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json: keep backends.ra or backends.sales as "grok".
grok -p "Summarize the regulatory risks" -m grok-4.5
```

停止した review を処理します。

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

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

tests は mocks、fixtures、temporary directories を使用するため、実際の Grok やその他の external AI credential は不要です。pytest が成功すると passing summary と exit code `0` で終了します。`verify_alignment.py` はすべての翻訳ファイルが構造的に一致すると報告し、`0` で終了します。repository には個別に設定された lint、type-check、formatter command がないため、記載しません。`git diff --check` は利用可能な基本 whitespace/patch check です。

実際の workflow 実行には、選択した backend CLI と login、`ollama` roles 用にアクセス可能な Ollama server、`use_worktree` が true の場合の Git worktree support が必要です。対象プロジェクトに別の command が必要な場合は、`.ai-company/config.json` の `test_command` で test commands を変更できます。

## 範囲と制限

- Specialist selection は動的です。タスクに対して Grok、RA、Sales を強制する CLI command はありません。
- Grok の設定済み model list には `grok-4.5` があり、2 番目に設定された Grok-model fallback はありません。
- `set-backend` がサポートするのは記載済みの roles と backend values です。Grok CLI の認証後は `grok` を設定できます。
- `reset --state` は string を受け取ります。意味のある作業を再開するには有効な workflow states を使用してください。
- AI workflow commands は対象プロジェクトの Git worktree を変更する可能性があり、provider usage または料金が発生することがあります。
