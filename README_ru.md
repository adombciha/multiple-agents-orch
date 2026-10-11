# Multi-Agent Orchestrator

[English](README.md) | [繁體中文](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md) | [Русский](README_ru.md)

Рабочий процесс разработки на Python с несколькими агентами и детерминированной машиной состояний для планирования, реализации, QA, проверки кода и итогового отчёта.

## Системные требования и установка

Выполняйте эти команды из оболочки. Для рабочего процесса Git worktree по умолчанию нужен Git; проект требует Python 3. Проект использует `requests` и `litellm`, а тесты также используют `pytest`.

```bash
git clone https://github.com/adombciha/multiple-agents-orch.git
cd multiple-agents-orch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python orchestrator.py --help
```

Последняя команда проверяет загрузку CLI. Она не подключается к AI backend и не создаёт worktree. Опции `--version` нет.

Для запуска AI workflow установите и авторизуйте выбранный CLI backend (`codex` или `agy`) либо запустите Ollama для роли `ollama`. Устанавливайте `use_worktree` в `false` только если изоляция Git worktree недоступна или не нужна.

## Workflow и файлы

`start` сохраняет запрос и сбрасывает состояние в `PLANNING`. Обычный поток состояний:

```text
PLANNING → DEVELOPING_PLAN → REVIEWING_PLAN → IMPLEMENTING
→ TESTING → REVIEWING_CODE → COMPLETED
```

PM анализирует требования и при необходимости привлекает специалистов. Их вывод передаётся Architect для проверки плана. RD реализует задачи, QA запускает настроенную команду тестов и сообщает результаты, Reviewer оценивает требования, план, тесты, выводы специалистов и Git diff, а Assistant после завершения создаёт `CHANGELOG.md`.

`init` создаёт `.ai-company/`, включая `config.json`, `state.json`, `request.md`, `requirements.json`, созданный для людей файл `requirements.md`, `implementation_plan.md`, `action_items.json`, выводы агентов, `test_results.txt`, `qa_report.md`, `reviewer_output.json`, `reviewer_output.md`, `specialist_reviews.md`, `human_report.md` и `final_report.md`. Для обмена между машинами по возможности используется JSON, а человекочитаемый вывод записывается в Markdown.

## Конфигурация

Run `python3 orchestrator.py init` before editing `.ai-company/config.json`. Missing keys use the defaults in `orchestrator/core/config.py`.

| Key | Purpose and default behavior |
| --- | --- |
| `ollama_url`, `ollama_model` | Ollama endpoint and default model (`http://localhost:11434`, `gemma4:latest`). |
| `test_command`, `max_revisions` | QA command and automatic plan/code revision limit; defaults are `python3 -m pytest -q` and `2`. |
| `backends` | Backend assigned to roles. |
| `model_tiers`, `role_models`, `role_model_backends`, `role_model_tiers` | Model lists and per-role model/backend routing. |
| `use_ponytail`, `use_worktree` | Minimalist prompting and Git-worktree isolation; both default to `false` and `true` respectively. |
| `backend_escalation_path`, `staffing_limits` | Backend escalation and RD/QA staffing limits. |
| `telemetry_enabled`, `llm_usage_log` | LiteLLM token metadata logging and JSONL path; defaults to `llm_usage.jsonl` in the workflow directory and does not store prompts or outputs. |

## Automatic model selection

Codex `auto` uses the installed CLI's recommended model. AGY `auto:gemini-flash-medium` reads `agy models`, selects the newest available Gemini Flash Medium, and caches the list for one hour. Other selectors include `auto:gemini-flash-low`, `auto:gemini-pro-low`, `auto:claude-sonnet-medium`, and `auto:claude-opus-medium`. Exact AGY model IDs remain selectable, including Gemini 3.8 Flash Low, Gemini 3.7 Flash Low/Medium, Gemini 3.1 Pro, Claude Sonnet, and Claude Opus. The removed backends are standalone Claude Code and Grok, not Claude models available through AGY.

```bash
agy models
```

### Динамическая маршрутизация моделей локального Ollama

Для локального выполнения через Ollama модель `tev1:4b` используется только как маршрутизатор решений через endpoint Ollama `/v1/systemone` (она не выполняет задачи через `/api/chat`).
- **Обнаружение моделей**: установленные модели динамически определяются через `/api/tags` настроенного endpoint Ollama. Модели не загружаются и не удаляются автоматически.
- **Конфиденциальность**: маршрутизатор получает только роль, краткое описание задачи и сведения о кандидатах; полный task prompt ему не передаётся.
- **Проверка и fallback**: проверяются выбранная модель, confidence и allowlist установленных кандидатов. Если endpoint или маршрутизатор недоступен либо результат недопустим или имеет низкую confidence, используется настроенная статическая цепочка маршрутов.
- **Локальная изоляция**: динамическая маршрутизация остаётся внутри Ollama и не меняет маршруты Codex или AGY.

## Автоматическая и человеческая проверка

Автоматическая проверка и проверка владельцем используют разные имена результатов и переходы.

| Producer | Result | Effect |
| --- | --- | --- |
| QA | `PASSED` | Continue from `TESTING` to `REVIEWING_CODE`. |
| QA | `FAILED` | Create a QA fix task and return to `IMPLEMENTING`; at `max_revisions`, pause in `WAITING_FOR_OWNER`. |
| Reviewer | `APPROVED` | Continue to completion. |
| Reviewer | `REJECTED` | Create a reviewer fix task and return to `IMPLEMENTING`; at `max_revisions`, pause in `WAITING_FOR_OWNER`. |
| Architect | plan revision | Revise the plan until `max_revisions`; then continue to `IMPLEMENTING`, without creating human review. |

`max_revisions` defaults to `2`, but the active `.ai-company/config.json` controls it. QA and Reviewer failures increment `code_revisions`; their retry limit is the condition that creates owner review. An AI backend failure, non-zero test-command exit status, or other command error is an execution failure. It is not the same as the owner's explicit `reject` decision.

In `WAITING_FOR_OWNER`, use `approve` or `review`:

| Decision | Meaning | Next behavior |
| --- | --- | --- |
| `pass` | Accept the saved passing path. | Resume at the recorded `pass_state`, then optionally continue with `--run`. |
| `revise` | Request another implementation pass. | Increment `code_revisions`, add `HUMAN-REVIEW-N`, and move to `IMPLEMENTING`; it is not blocked by the previous automatic retry threshold. |
| `reject` | Reject this workflow result. | Move to `FAILED`; normal execution stops. |

`approve` is the shortcut for the paused passing path. Successful `review` output is identifiable as:

```text
Human review '<decision>' recorded; workflow moved to <STATE>.
```

Use `python3 orchestrator.py status` to see the current state and revision counts. `--run` continues the loop only when the selected decision leaves a runnable workflow; it does not run after `reject`.

## Справочник CLI

Выполните `python3 orchestrator.py --help`, чтобы получить список команд. Every command below is run from the repository root; commands other than `init`, `start`, and `--help` require an initialized `.ai-company/` directory.

| Command | Syntax | Required arguments | Optional arguments / defaults | Output, prerequisites, and example |
| --- | --- | --- | --- | --- |
| Help | `python3 orchestrator.py --help` | None | `-h`, `--help` | Prints commands and exits. `python3 orchestrator.py --help` |
| `init` | `python3 orchestrator.py init` | None | None | Creates `.ai-company/config.json` and state files. `python3 orchestrator.py init` |
| `start` | `python3 orchestrator.py start <prompt>` | `prompt`: development request | None | Initializes, saves the request, resets to `PLANNING`; it does not run agents. `python3 orchestrator.py start "Add a command and tests"` |
| `step` | `python3 orchestrator.py step` | None | None | Runs one state-machine step; selected AI CLIs, Ollama, and Git worktree support may be needed. `python3 orchestrator.py step` |
| `run` | `python3 orchestrator.py run` | None | None | Runs until `COMPLETED`, `WAITING_FOR_OWNER`, or `FAILED`. `python3 orchestrator.py run` |
| `status` | `python3 orchestrator.py status` | None | None | Prints state, revision counters, selected backends, test command, and action items. `python3 orchestrator.py status` |
| `approve` | `python3 orchestrator.py approve [--run]` | None | `--run`: continue after approval; default off | Only valid in `WAITING_FOR_OWNER`; resumes at recorded state. `python3 orchestrator.py approve --run` |
| `review` | `python3 orchestrator.py review {pass,revise,reject} [--run]` | `decision`: `pass`, `revise`, or `reject` | `--run`: continue after `pass` or `revise`; default off | Only valid in `WAITING_FOR_OWNER`; prints the recorded-decision message. `python3 orchestrator.py review revise --run` |
| `reset` | `python3 orchestrator.py reset [--state STATE]` | None | `--state STATE`; default `PLANNING`, accepts a string state value | Clears revision/runtime routing data, removes the worktree without merging it, and stores the selected state. `python3 orchestrator.py reset --state DEVELOPING_PLAN` |
| `set-backend` | `python3 orchestrator.py set-backend <role> <backend>` | `role`, `backend` | None | Updates `backends` in config. Roles: `manager`, `architect`, `developer`, `reviewer`, `qa`, `assistant`, `ra`, `security`, `sales`, `sre`. Backends: `ollama`, `codex`, `agy`. `developer` and `qa` update their senior/middle/junior routes. `python3 orchestrator.py set-backend reviewer agy` |

Each subcommand also supports `--help`, for example `python3 orchestrator.py review --help`. Invalid role/backend/decision values are rejected by argparse. `status`, `approve`, `review`, `reset`, and `set-backend` report that initialization is required when `.ai-company/` is absent; `approve` and `review` also reject states other than `WAITING_FOR_OWNER`.

### Common CLI sequences

Minimal local setup and a task:

```bash
python3 orchestrator.py init
python3 orchestrator.py start "Add contact search and tests"
python3 orchestrator.py step
python3 orchestrator.py status
```

Run the normal automated workflow (requires the configured backends and, by default, Git):

```bash
python3 orchestrator.py run
```

Set a supported backend or select an AGY model before running the workflow:

```bash
python3 orchestrator.py init
python3 orchestrator.py set-backend reviewer agy
# Edit .ai-company/config.json to pin role_model_routes.ra[0][1] if needed.
agy models
```

Resolve a paused review:

```bash
python3 orchestrator.py review pass --run
python3 orchestrator.py review revise --run
python3 orchestrator.py review reject
```

## Автоматическое взвешивание и ролевой штат

Система управляет внутренними сотрудниками (Ollama) и платными поставщиками (Codex, Agy), корректируя доверительные веса на основе результатов.

### Сотрудники и поставщики

- **Внутренние сотрудники**: роли `developer_senior/middle/junior` и `qa_senior/middle/junior`, направленные в backend `ollama`, считаются бесплатными долгосрочными сотрудниками с накапливаемым доверительным весом (`worker_weights`).
- **Платные поставщики**: CLI backend `codex` и `agy` отслеживаются отдельно в `vendor_scores`.

### Доверительные веса и события оценки

- Для каждой комбинации роли (`rd`/`qa`) и уровня (`senior`/`middle`/`junior`) задаётся доверительный вес от `0.2` до `3.0`, по умолчанию используются значения из `worker_weights`.
- Завершение задания, успешный или неуспешный QA, а также события одобрения или отклонения Reviewer запускают обновление EWMA (экспоненциально взвешенного скользящего среднего), постепенно изменяющее вес; для поставщиков вместо этого обновляется собственная карточка, поэтому эти показатели не влияют друг на друга.

### Взвешенное голосование QA

- QA больше не требует единогласия. Голос каждого уровня взвешивается его доверительным весом; запуск считается успешным, когда взвешенное соотношение достигает `qa_vote_threshold` (по умолчанию `0.6`). Ненулевой код выхода команды тестов по-прежнему безусловно означает сбой запуска, независимо от голосования.
- Если взвешенное большинство переопределяет меньшинство с голосом FAILED, это разногласие записывается в отчёт QA и передаётся Reviewer для дополнительной проверки. Если человек впоследствии отменяет неуспешное решение QA, проголосовавший FAILED уровень (или уровни) получает штраф за ложноположительный результат.

### Оценка работы Manager

- В конце каждого запуска Manager предлагает небольшие корректировки весов (±`weight_adjustment_max_delta`, по умолчанию `0.3`) на основе телеметрии этого запуска; значения ограничиваются допустимым диапазоном и автоматически записываются обратно в конфигурацию. Карточки поставщиков имеют только информационный характер и этой проверкой не изменяются.

### Ежедневный состав

- Перед назначением задания Manager читает состав на сегодня: какие роли внутренних сотрудников доступны (доступен ли Ollama), какие поставщики доступны, а также текущий вес или рейтинг каждого из них.
- Если назначенный уровень недоступен, задание автоматически передаётся ближайшему доступному уровню; при прочих равных предпочтение отдаётся более высокому весу или рейтингу.

### Срочная помощь поставщиков

- Если внутренние сотрудники продолжают испытывать трудности с заданием (заканчиваются попытки исправления, доверительный вес опустился слишком низко или Ollama недоступен), система автоматически передаёт задание лучшему доступному поставщику из `backend_escalation_path` для срочного решения. Это детерминированное решение, а не отдельный вызов модели.

### Внутренняя память компании (мозг)

- Внутренние сотрудники (только backend Ollama) имеют доступ к изолированной базе данных SQLite + FTS5 «мозг», в которой хранятся уроки из сбоев и итоги совещаний; они добавляются в соответствующие будущие запросы с использованием RAG. Запросы поставщиков не видят эту память, поэтому знания компании не покидают изолированную среду.
- Только фиксированные хуки кода записывают данные в эту базу; вывод модели никогда не записывается напрямую, что предотвращает отравление долговременной памяти через prompt injection.

### Новые команды CLI

| Команда | Описание |
| --- | --- |
| `weights` | Показать текущие доверительные веса внутренних сотрудников |
| `weights set <role> <level> <value>` | Вручную переопределить вес |
| `roster` | Показать доступные сегодня роли RD/QA (сотрудники и поставщики) |
| `vendors` | Показать карточку платных поставщиков |
| `brain search <query>` | Выполнить поиск во внутренней памяти компании |
| `brain prune [--keep N]` | Удалить старые записи, оставив только последние N |

### Новые ключи конфигурации

`worker_weights`, `qa_vote_threshold`, `weight_adjustment_max_delta`, `vendor_scores`, `vendor_hire_min_jobs`, `brain_db` (значения по умолчанию см. в `orchestrator/core/config.py`).

## Тесты и проверка

From the repository root, install test dependencies and run the available checks:

```bash
python3 -m pip install requests pytest
python3 -m pytest -q
python3 -m pytest -q test_orchestrator.py
python3 -m pytest -q test_orchestrator.py::test_initialized_orchestrator_fixture_loads_temp_config_and_state
python3 verify_alignment.py
git diff --check
```

Тесты используют mocks, fixtures и временные каталоги, поэтому внешняя AI авторизация не требуется. Успешный pytest завершается с кодом `0`; `git diff --check` проверяет пробелы.

Real workflow execution still needs the selected backend CLI and login, an accessible Ollama server for `ollama` roles, and Git worktree support when `use_worktree` is true. Test commands can be changed with `test_command` in `.ai-company/config.json` when the target project needs a different command.

## Область и ограничения

- Specialist selection is dynamic; no CLI command forces RA or Sales for a task.
- AGY auto selectors require `agy models` to succeed; explicit model IDs stay pinned.
- `set-backend` supports only the documented roles and backend values.
- `reset --state` accepts a string; use valid workflow states to resume meaningful work.
- AI workflow commands can modify the target project's Git worktree and can incur provider usage or charges.

## Дальнейшее развитие

Проект стремится развиться из детерминированной платформы оркестрации агентов в симулятор AI-организации, способный динамически формировать инженерные команды, распределять обязанности и управлять автономным сотрудничеством.
