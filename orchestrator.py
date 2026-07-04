#!/usr/bin/env python3
"""
AI Multi-Agent Orchestrator
Coordinates Ollama (Manager/Orchestrator), Codex (Developer), and Claude Code (Reviewer/Architect)
to manage software development workflows inside WSL.
"""

from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
import requests

# Default Configuration
DEFAULT_CONFIG = {
    "language": "en",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:latest",
    "test_command": "git diff --stat",  # Runs a simple check if no test suite exists
    "max_revisions": 2,
    "backends": {
        "manager": "ollama",
        "developer": "ollama",
        "reviewer": "ollama",  # Default to ollama; user can change to 'claude' when ready
        "qa": "ollama",        # Default to ollama QA backend
        "assistant": "ollama"  # Default to ollama assistant
    },
    "use_ponytail": False,  # Enforces minimalist senior developer/reviewer principles (YAGNI)
    "use_worktree": True,   # Enforces isolated git worktrees for agent roles
    "backend_escalation_path": ["ollama", "agy", "codex"],
    "model_tiers": {
        "ollama": ["gemma4:latest", "gemma2:2b", "gemma2:9b"],
        "agy": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "codex": ["gpt-4o-mini", "o3-mini", "gpt-5.5"],
        "claude": ["claude-3-5-haiku", "claude-3-7-sonnet"]
    }
}

TRANSLATIONS = {
    "en": {
        "status.header": "ORCHESTRATOR STATUS",
        "status.current_state": "Current State",
        "status.current_state_is": "Current state is: {state}",
        "status.plan_revisions": "Plan Revisions",
        "status.code_revisions": "Code Revisions",
        "status.developer_backend": "Developer Backend",
        "status.reviewer_backend": "Reviewer Backend",
        "status.qa_backend": "QA Backend",
        "status.test_command": "Test Command",
        "status.action_items": "Action Items",
        "status.no_tasks": "No tasks parsed yet.",
        "phase.planning": "1. PLANNING (Ollama Manager)",
        "phase.developing_plan": "2. DEVELOPING PLAN (Developer)",
        "phase.reviewing_plan": "3. REVIEWING PLAN (Architect / Reviewer)",
        "phase.implementing_code": "4. IMPLEMENTING CODE (Developer)",
        "phase.testing_verification": "5. TESTING & VERIFICATION (QA Agent)",
        "phase.reviewing_code": "6. REVIEWING CODE (Architect / Reviewer)",
        "phase.generating_summary": "7. GENERATING SUMMARY (Ollama Manager)",
        "manager.requirements_system": (
            "You are the Project Manager of an AI software company. Your job is to analyze the user's request "
            "and write a detailed, clear requirements document in Markdown format.\n"
            "Requirements must contain:\n"
            "1. Project Overview & Context\n"
            "2. Specific Functional Requirements\n"
            "3. Technical Specifications & Stack constraints\n"
            "4. Scope boundaries (what is NOT included)\n\n"
            "Output ONLY the Markdown content for requirements.md. Do not add any greeting, preamble, or conversational introduction."
        ),
        "manager.parse_tasks_prompt": (
            "Read this implementation plan:\n\n"
            "{plan}\n\n"
            "Extract a flat JSON array of tasks representing the steps to be coded.\n"
            "Each task must be a JSON object with fields:\n"
            "- 'id': unique string ID (e.g. 'TASK-001', 'TASK-002')\n"
            "- 'description': concise description of the coding step\n"
            "- 'status': 'pending'\n\n"
            "Respond ONLY with a valid JSON array. Do not include markdown code block syntax (like ```json) or any other text."
        ),
        "manager.parse_tasks_system": "You are a Project Manager. Output only raw JSON lists of tasks.",
        "manager.final_report_prompt": (
            "We have successfully completed the tasks.\n"
            "Original Request:\n{request}\n\n"
            "Requirements:\n{requirements}\n\n"
            "Git Diff Stat:\n{diff_stat}\n\n"
            "Please generate a Final Report in Markdown. Summarize what was built, files modified, and verify how requirements were met."
        ),
        "manager.final_report_system": "You are a Project Manager. Write a beautiful project final report.",
        "reviewer.plan_prompt": (
            "Review the implementation plan against the requirements.\n\n"
            "Requirements:\n{requirements}\n\n"
            "Implementation Plan:\n{plan}\n\n"
            "Check for architectural issues, gaps in requirements, and safety.\n"
            "If acceptable, start your response with 'APPROVED'.\n"
            "If issues exist, start your response with 'REJECTED' followed by detailed feedback.\n\n"
            "Format:\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.plan_system": "You are a Senior Software Architect. Review the implementation plan.",
        "qa.report_prompt": (
            "You are the QA Engineer. Analyze the test execution results for our changes.\n\n"
            "Requirements:\n{requirements}\n\n"
            "Implementation Plan:\n{plan}\n\n"
            "Git Diff:\n{git_diff}\n\n"
            "Raw Test Output:\n{output}\n"
            "Test Exit Code: {code}\n\n"
            "Generate a detailed QA test report in Markdown. "
            "If all tests pass and the implementation looks correct and safe, your report MUST start with 'PASSED'. "
            "If there are any test failures, errors, unhandled exceptions, or missing deliverables, your report MUST start with 'FAILED' followed by the details of the issues and suggested fixes."
        ),
        "qa.report_system": "You are a Senior Quality Assurance Engineer. Generate a QA report.",
        "reviewer.code_prompt": (
            "Review the code changes made. Here is the context:\n\n"
            "Requirements:\n{requirements}\n\n"
            "Plan:\n{plan}\n\n"
            "Test Results:\n{test_results}\n\n"
            "Git Diff:\n{git_diff}\n\n"
            "Verify if the implementation matches requirements and plan, and if the tests pass.\n"
            "If acceptable, start your response with 'APPROVED'.\n"
            "If there are bugs, logic errors, style issues, or failures, start your response with 'REJECTED' followed by detailed feedback.\n\n"
            "Format:\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.code_system": "You are a Senior Code Reviewer. Review the git diff and test results.",
        "assistant.changelog_prompt": "Please generate a CHANGELOG entry for the following completed task.\n\nSummary:\n{summary}\n\nDiff:\n{diff_patch}",
        "assistant.changelog_system": "You are the project Assistant. You write concise, professional markdown CHANGELOG entries.",
    },
    "zh-TW": {
        "status.header": "協調器狀態",
        "status.current_state": "目前狀態",
        "status.current_state_is": "目前狀態：{state}",
        "status.plan_revisions": "計畫修訂次數",
        "status.code_revisions": "程式碼修訂次數",
        "status.developer_backend": "開發者後端",
        "status.reviewer_backend": "審查者後端",
        "status.qa_backend": "QA 後端",
        "status.test_command": "測試命令",
        "status.action_items": "待辦項目",
        "status.no_tasks": "尚未解析任何任務。",
        "phase.planning": "1. 規劃（Ollama 專案經理）",
        "phase.developing_plan": "2. 制定計畫（開發者）",
        "phase.reviewing_plan": "3. 審查計畫（架構師 / 審查者）",
        "phase.implementing_code": "4. 實作程式碼（開發者）",
        "phase.testing_verification": "5. 測試與驗證（QA 代理）",
        "phase.reviewing_code": "6. 審查程式碼（架構師 / 審查者）",
        "phase.generating_summary": "7. 產生摘要（Ollama 專案經理）",
        "manager.requirements_system": (
            "你是 AI 軟體公司的專案經理。你的工作是分析使用者需求，"
            "並以 Markdown 格式撰寫詳細且清楚的需求文件。\n"
            "需求必須包含：\n"
            "1. 專案概觀與背景\n"
            "2. 具體功能需求\n"
            "3. 技術規格與技術限制\n"
            "4. 範圍邊界（不包含哪些內容）\n\n"
            "只輸出 requirements.md 的 Markdown 內容。不要加入問候、前言或對話式介紹。"
        ),
        "manager.parse_tasks_prompt": (
            "閱讀以下實作計畫：\n\n"
            "{plan}\n\n"
            "擷取一個扁平 JSON array，代表需要撰寫程式碼的步驟。\n"
            "每個任務必須是 JSON object，並包含欄位：\n"
            "- 'id': 唯一字串 ID（例如 'TASK-001'、'TASK-002'）\n"
            "- 'description': 精簡描述此程式碼步驟\n"
            "- 'status': 'pending'\n\n"
            "只回覆有效的 JSON array。不要包含 markdown code block 語法（例如 ```json）或任何其他文字。"
        ),
        "manager.parse_tasks_system": "你是專案經理。只輸出 raw JSON 任務列表。",
        "manager.final_report_prompt": (
            "我們已成功完成這些任務。\n"
            "原始需求：\n{request}\n\n"
            "需求文件：\n{requirements}\n\n"
            "Git Diff 統計：\n{diff_stat}\n\n"
            "請以 Markdown 產生最終報告，摘要說明完成內容、修改檔案，並驗證需求如何被滿足。"
        ),
        "manager.final_report_system": "你是專案經理。撰寫一份精美的專案最終報告。",
        "reviewer.plan_prompt": (
            "請依需求審查實作計畫。\n\n"
            "需求：\n{requirements}\n\n"
            "實作計畫：\n{plan}\n\n"
            "檢查架構問題、需求缺口與安全性。\n"
            "若可接受，回覆必須以 'APPROVED' 開頭。\n"
            "若有問題，回覆必須以 'REJECTED' 開頭，後面接詳細回饋。\n\n"
            "格式：\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.plan_system": "你是資深軟體架構師。請審查實作計畫。",
        "qa.report_prompt": (
            "你是 QA 工程師。請分析本次變更的測試執行結果。\n\n"
            "需求：\n{requirements}\n\n"
            "實作計畫：\n{plan}\n\n"
            "Git Diff：\n{git_diff}\n\n"
            "原始測試輸出：\n{output}\n"
            "測試結束代碼：{code}\n\n"
            "請產生詳細的 Markdown QA 測試報告。"
            "若所有測試通過且實作看起來正確安全，報告必須以 'PASSED' 開頭。"
            "若有任何測試失敗、錯誤、未處理例外或缺少交付項目，報告必須以 'FAILED' 開頭，後面接問題細節與建議修正。"
        ),
        "qa.report_system": "你是資深品質保證工程師。請產生 QA 報告。",
        "reviewer.code_prompt": (
            "請審查已完成的程式碼變更。以下是背景：\n\n"
            "需求：\n{requirements}\n\n"
            "計畫：\n{plan}\n\n"
            "測試結果：\n{test_results}\n\n"
            "Git Diff：\n{git_diff}\n\n"
            "確認實作是否符合需求與計畫，以及測試是否通過。\n"
            "若可接受，回覆必須以 'APPROVED' 開頭。\n"
            "若有 bug、邏輯錯誤、風格問題或失敗，回覆必須以 'REJECTED' 開頭，後面接詳細回饋。\n\n"
            "格式：\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.code_system": "你是資深程式碼審查者。請審查 git diff 與測試結果。",
        "assistant.changelog_prompt": "請為以下已完成的任務生成一段 CHANGELOG 紀錄。\n\n總結：\n{summary}\n\n差異：\n{diff_patch}",
        "assistant.changelog_system": "你是專案助理。請撰寫簡潔專業的 Markdown 格式 CHANGELOG 紀錄。",
    },
    "ja": {
        "status.header": "オーケストレーター状態",
        "status.current_state": "現在の状態",
        "status.current_state_is": "現在の状態: {state}",
        "status.plan_revisions": "計画の修正回数",
        "status.code_revisions": "コードの修正回数",
        "status.developer_backend": "開発者バックエンド",
        "status.reviewer_backend": "レビュアーバックエンド",
        "status.qa_backend": "QA バックエンド",
        "status.test_command": "テストコマンド",
        "status.action_items": "アクション項目",
        "status.no_tasks": "解析済みのタスクはまだありません。",
        "phase.planning": "1. 計画（Ollama マネージャー）",
        "phase.developing_plan": "2. 計画作成（開発者）",
        "phase.reviewing_plan": "3. 計画レビュー（アーキテクト / レビュアー）",
        "phase.implementing_code": "4. コード実装（開発者）",
        "phase.testing_verification": "5. テストと検証（QA エージェント）",
        "phase.reviewing_code": "6. コードレビュー（アーキテクト / レビュアー）",
        "phase.generating_summary": "7. サマリー生成（Ollama マネージャー）",
        "manager.requirements_system": (
            "あなたは AI ソフトウェア会社のプロジェクトマネージャーです。ユーザーの依頼を分析し、"
            "Markdown 形式で詳細かつ明確な要件定義書を書くことが仕事です。\n"
            "要件には必ず以下を含めてください:\n"
            "1. プロジェクト概要と背景\n"
            "2. 具体的な機能要件\n"
            "3. 技術仕様と技術スタック上の制約\n"
            "4. スコープ境界（含まないもの）\n\n"
            "requirements.md の Markdown 内容だけを出力してください。挨拶、前置き、会話的な導入は追加しないでください。"
        ),
        "manager.parse_tasks_prompt": (
            "次の実装計画を読んでください:\n\n"
            "{plan}\n\n"
            "コーディングする手順を表すフラットな JSON array を抽出してください。\n"
            "各タスクは次のフィールドを持つ JSON object である必要があります:\n"
            "- 'id': 一意の文字列 ID（例: 'TASK-001', 'TASK-002'）\n"
            "- 'description': コーディング手順の簡潔な説明\n"
            "- 'status': 'pending'\n\n"
            "有効な JSON array だけを返してください。markdown code block 構文（```json など）やその他の文字は含めないでください。"
        ),
        "manager.parse_tasks_system": "あなたはプロジェクトマネージャーです。raw JSON のタスクリストだけを出力してください。",
        "manager.final_report_prompt": (
            "タスクは正常に完了しました。\n"
            "元の依頼:\n{request}\n\n"
            "要件:\n{requirements}\n\n"
            "Git Diff 統計:\n{diff_stat}\n\n"
            "Markdown で最終レポートを生成してください。構築した内容、変更したファイル、要件がどのように満たされたかを要約してください。"
        ),
        "manager.final_report_system": "あなたはプロジェクトマネージャーです。読みやすいプロジェクト最終レポートを書いてください。",
        "reviewer.plan_prompt": (
            "要件に照らして実装計画をレビューしてください。\n\n"
            "要件:\n{requirements}\n\n"
            "実装計画:\n{plan}\n\n"
            "アーキテクチャ上の問題、要件の抜け、安全性を確認してください。\n"
            "問題なければ、回答は必ず 'APPROVED' で始めてください。\n"
            "問題がある場合、回答は必ず 'REJECTED' で始め、その後に詳細なフィードバックを書いてください。\n\n"
            "形式:\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.plan_system": "あなたはシニアソフトウェアアーキテクトです。実装計画をレビューしてください。",
        "qa.report_prompt": (
            "あなたは QA エンジニアです。今回の変更に対するテスト実行結果を分析してください。\n\n"
            "要件:\n{requirements}\n\n"
            "実装計画:\n{plan}\n\n"
            "Git Diff:\n{git_diff}\n\n"
            "生のテスト出力:\n{output}\n"
            "テスト終了コード: {code}\n\n"
            "Markdown で詳細な QA テストレポートを生成してください。"
            "すべてのテストが通り、実装が正しく安全に見える場合、レポートは必ず 'PASSED' で始めてください。"
            "テスト失敗、エラー、未処理例外、または不足した成果物がある場合、レポートは必ず 'FAILED' で始め、その後に問題の詳細と修正案を書いてください。"
        ),
        "qa.report_system": "あなたはシニア品質保証エンジニアです。QA レポートを生成してください。",
        "reviewer.code_prompt": (
            "実施されたコード変更をレビューしてください。背景は次のとおりです:\n\n"
            "要件:\n{requirements}\n\n"
            "計画:\n{plan}\n\n"
            "テスト結果:\n{test_results}\n\n"
            "Git Diff:\n{git_diff}\n\n"
            "実装が要件と計画に合っているか、テストが通っているかを確認してください。\n"
            "問題なければ、回答は必ず 'APPROVED' で始めてください。\n"
            "バグ、論理エラー、スタイル問題、失敗がある場合、回答は必ず 'REJECTED' で始め、その後に詳細なフィードバックを書いてください。\n\n"
            "形式:\n"
            "[APPROVED or REJECTED]\n"
            "[Feedback details]"
        ),
        "reviewer.code_system": "あなたはシニアコードレビュアーです。git diff とテスト結果をレビューしてください。",
        "assistant.changelog_prompt": "以下の完了したタスクのCHANGELOGエントリを生成してください。\n\n概要：\n{summary}\n\n差分：\n{diff_patch}",
        "assistant.changelog_system": "あなたはプロジェクトアシスタントです。簡潔で専門的なMarkdown形式のCHANGELOGエントリを作成してください。",
    },
}

def normalize_language(language: str | None) -> str:
    return language if language in TRANSLATIONS else DEFAULT_CONFIG["language"]

def tr(key: str, lang: str | None = None, **kwargs) -> str:
    text = TRANSLATIONS.get(normalize_language(lang), {}).get(key, TRANSLATIONS["en"].get(key, key))
    if kwargs:
        for k, v in kwargs.items():
            text = text.replace(f"{{{k}}}", str(v))
    return text

PONYTAIL_PROMPT = (
    "\n\n[PONYTAIL RULE ACTIVE]\n"
    "Enforce the laziest solution that actually works, simplest, shortest, most minimal. "
    "Channel a senior developer who has seen everything:\n"
    "- Climb the ladder:\n"
    "  1. Does this need to exist at all? (YAGNI)\n"
    "  2. Already in this codebase? Reuse it. Look before you write.\n"
    "  3. Stdlib does it? Use it.\n"
    "  4. Native platform feature covers it? Use it.\n"
    "  5. Already-installed dependency solves it? Use it.\n"
    "  6. Can it be one line? One line.\n"
    "  7. Only then: the minimum code that works.\n"
    "- No unrequested abstractions: no interface with one implementation, no factory for one product, no config for a value that never changes.\n"
    "- Deletion over addition. Shortest working diff wins.\n"
    "- Non-trivial logic must leave one runnable check behind."
)

# ANSI Escape Colors for prettier logging
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_info(msg: str):
    print(f"{Colors.BLUE}[*] {msg}{Colors.ENDC}")

def log_success(msg: str):
    print(f"{Colors.GREEN}[+] {msg}{Colors.ENDC}")

def log_warning(msg: str):
    print(f"{Colors.WARNING}[!] {msg}{Colors.ENDC}")

def log_error(msg: str):
    print(f"{Colors.FAIL}[-] {msg}{Colors.ENDC}")

def log_header(msg: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}=== {msg} ==={Colors.ENDC}")

class AgentOrchestrator:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.ai_dir = workspace / ".ai-company"
        self.config_path = self.ai_dir / "config.json"
        self.state_path = self.ai_dir / "state.json"
        
        # Files for data sharing
        self.request_path = self.ai_dir / "request.md"
        self.requirements_path = self.ai_dir / "requirements.md"
        self.plan_path = self.ai_dir / "implementation_plan.md"
        self.action_items_path = self.ai_dir / "action_items.json"
        self.reviewer_output_path = self.ai_dir / "reviewer_output.md"
        self.developer_output_path = self.ai_dir / "developer_output.md"
        self.test_results_path = self.ai_dir / "test_results.txt"
        self.qa_report_path = self.ai_dir / "qa_report.md"
        self.final_report_path = self.ai_dir / "final_report.md"

        self.config = DEFAULT_CONFIG
        self.state = {
            "state": "PLANNING",
            "plan_revisions": 0,
            "code_revisions": 0,
            "model_tier_indices": {
                "developer": 0,
                "reviewer": 0,
                "manager": 0,
                "qa": 0
            },
            "tasks": []
        }

    def init_project(self):
        """Initializes the .ai-company folder and configuration files."""
        self.ai_dir.mkdir(exist_ok=True)
        
        # Write config if not exists
        if not self.config_path.exists():
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            log_success(f"Created config: {self.config_path}")
        else:
            log_info(f"Config already exists: {self.config_path}")

        # Write initial state if not exists
        if not self.state_path.exists():
            self.save_state()
            log_success(f"Created state file: {self.state_path}")
        else:
            log_info(f"State file already exists: {self.state_path}")

        # Suggest Windows Host IP for Ollama setup
        host_ip = self.get_windows_host_ip()
        log_info(f"Suggested Windows Host IP: {host_ip}")
        log_info(f"If Ollama is running on Windows, set 'ollama_url' in config.json to 'http://{host_ip}:11434'")

    def load_config_and_state(self):
        """Loads configuration and state files from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError("Project not initialized. Please run 'init' first.")
            
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
            
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.state = json.load(f)

    def save_state(self):
        """Saves current state to state.json."""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def get_windows_host_ip(self) -> str:
        """Finds the default route gateway (usually the Windows Host IP in WSL)."""
        try:
            result = subprocess.run(
                ["ip", "route", "show"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            for line in result.stdout.splitlines():
                if "default via" in line:
                    parts = line.split()
                    if len(parts) > 2:
                        return parts[2]
        except Exception:
            pass
        return "127.0.0.1"

    def run_command(self, cmd: list[str], timeout: int = 1800, capture: bool = True, cwd: Path | None = None) -> tuple[int, str]:
        """Runs a subprocess command safety and returns (returncode, output)."""
        exec_cwd = cwd or self.workspace
        
        # If use_worktree is active and we are executing in implementing/testing states,
        # redirect default cwd to the worktree path
        if self.config.get("use_worktree", True) and not cwd:
            current_state = self.state.get("state", "PLANNING")
            if current_state in ["IMPLEMENTING", "TESTING", "REVIEWING_CODE"]:
                wt_path = self.ai_dir / "worktree"
                if wt_path.exists():
                    exec_cwd = wt_path

        try:
            result = subprocess.run(
                cmd,
                cwd=exec_cwd,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=True,
                timeout=timeout,
                check=False
            )
            output = ""
            if capture:
                output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            return result.returncode, output
        except subprocess.TimeoutExpired as e:
            return -1, f"Timeout expired: {e}"
        except Exception as e:
            return -1, f"Error running command: {e}"

    # Agent backends callers
    def call_ollama(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        url = f"{self.config['ollama_url']}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        model = self.get_active_model_for_role(role, "ollama") or self.config.get("ollama_model", "gemma2:2b")
        log_info(f"Ollama calling model: {model}")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False
        }
        
        try:
            response = requests.post(url, json=payload, timeout=600)
            response.raise_for_status()
            return response.json()["message"]["content"]
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to communicate with Ollama at {url}.")
            log_error(f"Error detail: {e}")
            raise RuntimeError(f"Ollama connection failed: {e}")

    def call_codex(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        # Prepend system prompt to the user prompt if present
        full_prompt = ""
        if system_prompt:
            full_prompt += f"System Instructions:\n{system_prompt}\n\n"
        full_prompt += prompt

        # Write prompt to a temp file to avoid shell argument length limits
        temp_prompt_file = self.ai_dir / "temp_codex_prompt.txt"
        with open(temp_prompt_file, "w", encoding="utf-8") as f:
            f.write(full_prompt)

        cmd = [
            "codex", "exec", 
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check"
        ]
        
        model = self.get_active_model_for_role(role, "codex")
        if model:
            cmd.extend(["-m", model])
        cmd.append("-")
        
        log_info(f"Running Codex: {' '.join(cmd)}")
        try:
            # Send prompt via stdin
            with open(temp_prompt_file, "r", encoding="utf-8") as pf:
                result = subprocess.run(
                    cmd,
                    cwd=self.workspace,
                    stdin=pf,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=1800,
                    check=False
                )
            
            # Clean up temp file
            if temp_prompt_file.exists():
                temp_prompt_file.unlink()

            if result.returncode != 0:
                raise RuntimeError(f"Codex failed with code {result.returncode}:\n{result.stderr}")
            return result.stdout
        except Exception as e:
            if temp_prompt_file.exists():
                temp_prompt_file.unlink()
            raise e

    def call_claude(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        full_prompt = ""
        if system_prompt:
            full_prompt += f"{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["claude", "--print", "--dangerously-skip-permissions"]
        model = self.get_active_model_for_role(role, "claude")
        if model:
            cmd.extend(["--model", model])
        cmd.append(full_prompt)
        
        log_info(f"Running Claude Code: {' '.join(cmd[:-1])} ...")
        result = subprocess.run(
            cmd,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI failed with code {result.returncode}:\n{result.stderr}")
        return result.stdout


    def call_agy(self, prompt: str, system_prompt: str | None = None, role: str = "developer") -> str:
        full_prompt = ""
        if system_prompt:
            full_prompt += f"System Instructions:\n{system_prompt}\n\n"
        full_prompt += prompt

        cmd = ["agy"]
        model = self.get_active_model_for_role(role, "agy")
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--print", full_prompt])
        
        log_info(f"Running agy: {' '.join(cmd[:-1])} ...")
        result = subprocess.run(
            cmd,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"agy CLI failed with code {result.returncode}:\n{result.stderr}")
        return result.stdout

    def call_manager(self, prompt: str, system_prompt: str | None = None) -> str:
        backend = self.config["backends"].get("manager", "ollama")
        log_info(f"Requesting Agent 'manager' (Backend: {backend})...")
        
        # Inject ponytail prompt if enabled
        if self.config.get("use_ponytail", False):
            if system_prompt:
                system_prompt += PONYTAIL_PROMPT
            else:
                system_prompt = PONYTAIL_PROMPT.strip()

        if backend == "codex":
            try:
                return self.call_codex(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"Codex manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        elif backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"Claude manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt, role="manager")
            except Exception as e:
                log_warning(f"agy manager backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_ollama(prompt, system_prompt, role="manager")
        else:
            return self.call_ollama(prompt, system_prompt, role="manager")

    def call_agent_ollama_fallback(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        """Helper to call Ollama and append format instructions if missing for the developer role."""
        if role == "developer" and "[FILE_START:" not in prompt:
            prompt += (
                "\n\nCRITICAL: You do not have access to terminal tools. "
                "To modify files, you must output your changes using this exact block format:\n"
                "[FILE_START: path/to/file.ext]\n"
                "full file content here\n"
                "[FILE_END: path/to/file.ext]\n\n"
                "Any modifications outside this format will be ignored. Write only complete file contents inside the blocks."
            )
        return self.call_ollama(prompt, system_prompt, role=role)

    def call_agent(self, role: str, prompt: str, system_prompt: str | None = None) -> str:
        backend = self.config["backends"].get(role, "ollama")
        log_info(f"Requesting Agent '{role}' (Backend: {backend})...")
        
        # Inject ponytail prompt if enabled and role is developer or reviewer
        if self.config.get("use_ponytail", False) and role in ["developer", "reviewer"]:
            if system_prompt:
                system_prompt += PONYTAIL_PROMPT
            else:
                system_prompt = PONYTAIL_PROMPT.strip()

        if backend == "claude":
            try:
                return self.call_claude(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"Claude backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "codex":
            try:
                return self.call_codex(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"Codex backend failed: {e}")
                log_warning("Falling open to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        elif backend == "agy":
            try:
                return self.call_agy(prompt, system_prompt, role=role)
            except Exception as e:
                log_warning(f"agy backend failed: {e}")
                log_warning("Falling back to Ollama backend.")
                return self.call_agent_ollama_fallback(role, prompt, system_prompt)
        else:
            return self.call_agent_ollama_fallback(role, prompt, system_prompt)

    # Workflow Steps
    def setup_worktree(self):
        """Creates an isolated git worktree for development and QA testing."""
        if not self.config.get("use_worktree", True):
            return
            
        wt_path = self.ai_dir / "worktree"
        
        # 1. Clean up any existing worktree/branch first
        self.cleanup_worktree()
        
        # 2. Add worktree
        log_info(f"Setting up isolated Git worktree at {wt_path} on branch 'ai-feature-branch'...")
        wt_path.parent.mkdir(exist_ok=True, parents=True)
        
        # Run git worktree add
        code, output = self.run_command(["git", "worktree", "add", "-b", "ai-feature-branch", str(wt_path)], cwd=self.workspace)
        if code != 0:
            log_error(f"Failed to create git worktree: {output}")
            raise RuntimeError("Git worktree setup failed.")
        log_success("Git worktree successfully established.")

    def cleanup_worktree(self, merge=False):
        """Cleans up the git worktree, optionally merging the changes back first."""
        if not self.config.get("use_worktree", True):
            return
            
        wt_path = self.ai_dir / "worktree"
        
        # If merge is requested, merge the branch in root
        if merge:
            log_info("Merging 'ai-feature-branch' back into master...")
            
            # Commit any changes inside the worktree first if they are not committed
            # We want to add and commit
            self.run_command(["git", "add", "."], cwd=wt_path)
            self.run_command(["git", "commit", "-m", "AI Auto-commit before merge"], cwd=wt_path)
            
            code, output = self.run_command(["git", "merge", "ai-feature-branch"], cwd=self.workspace)
            if code != 0:
                log_error(f"Failed to merge feature branch due to conflict: {output}")
                log_warning("ABORTING WORKTREE CLEANUP! Please resolve the git merge conflict manually in your root workspace.")
                log_warning("The 'ai-feature-branch' and your worktree are preserved.")
                return
            else:
                log_success("Successfully merged changes to master!")
                
        # Remove worktree
        wt_list = self.run_command(["git", "worktree", "list"], capture=True)[1]
        if "worktree" in wt_list:
            log_info("Removing Git worktree...")
            self.run_command(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.workspace)
            
        # Delete branch
        self.run_command(["git", "branch", "-D", "ai-feature-branch"], cwd=self.workspace)

    def step_planning(self):
        lang = self.config.get("language")
        log_header(tr("phase.planning", lang))
        
        # Setup clean worktree
        self.setup_worktree()
        
        if not self.request_path.exists():
            log_error(f"No request file found at {self.request_path}. Please run 'start' command first.")
            sys.exit(1)
            
        with open(self.request_path, "r", encoding="utf-8") as f:
            request = f.read()

        system_prompt = tr("manager.requirements_system", lang)
        
        requirements = self.call_manager(request, system_prompt)
        
        # Save requirements
        with open(self.requirements_path, "w", encoding="utf-8") as f:
            f.write(requirements)
            
        log_success(f"Requirements generated and saved to {self.requirements_path}")
        self.state["state"] = "DEVELOPING_PLAN"
        self.save_state()

    def step_developing_plan(self):
        lang = self.config.get("language")
        log_header(tr("phase.developing_plan", lang))
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()

        prompt = (
            f"Please read the following project requirements:\n\n"
            f"{requirements}\n\n"
            f"Draft a step-by-step implementation plan in Markdown. The plan should include:\n"
            f"1. Target files to create or modify.\n"
            f"2. Specific changes or logic details for each file.\n"
            f"3. Sequence of work (action items).\n"
            f"4. Testing strategy.\n\n"
            f"Write ONLY the Markdown implementation plan. Do not include any conversational preamble or postscript."
        )

        if self.state["plan_revisions"] > 0 and self.reviewer_output_path.exists():
            with open(self.reviewer_output_path, "r", encoding="utf-8") as f:
                feedback = f.read()
            prompt = (
                f"Your previous implementation plan was REJECTED by the reviewer with feedback:\n\n"
                f"{feedback}\n\n"
                f"Please revise the implementation plan to address all reviewer comments.\n"
                f"Write the complete updated implementation plan in Markdown. Only output the plan content."
            )

        system_prompt = "You are a Lead Software Developer. Generate a clear step-by-step implementation plan."
        plan = self.call_agent("developer", prompt, system_prompt)

        with open(self.plan_path, "w", encoding="utf-8") as f:
            f.write(plan)
        log_success(f"Implementation plan generated and saved to {self.plan_path}")

        # Now parse plan into tasks (Action Items) using Manager (Ollama)
        log_info("Parsing implementation plan into structured action items...")
        parse_prompt = tr("manager.parse_tasks_prompt", lang, plan=plan)
        
        parsed_items_raw = self.call_manager(parse_prompt, tr("manager.parse_tasks_system", lang))
        
        # Clean potential markdown wrapping
        clean_json = parsed_items_raw.strip()
        if clean_json.startswith("```"):
            lines = clean_json.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_json = "\n".join(lines).strip()

        try:
            tasks = json.loads(clean_json)
            # Retain previously completed tasks if we are revising
            existing_completed = {t["id"] for t in self.state["tasks"] if t.get("status") == "completed"}
            for t in tasks:
                if t["id"] in existing_completed:
                    t["status"] = "completed"
            self.state["tasks"] = tasks
            self.save_state()
            with open(self.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)
            log_success(f"Saved {len(tasks)} tasks to {self.action_items_path}")
        except json.JSONDecodeError as e:
            log_warning(f"Could not parse tasks as JSON. Saving raw output. Error: {e}")
            log_warning(f"Raw Ollama output was: {parsed_items_raw}")
            # Write a fallback task
            fallback_tasks = [{"id": "TASK-001", "description": "Implement overall implementation plan", "status": "pending"}]
            self.state["tasks"] = fallback_tasks
            self.save_state()

        self.state["state"] = "REVIEWING_PLAN"
        self.save_state()

    def get_active_model_for_role(self, role: str, backend: str) -> str | None:
        """Returns the specific active model name for a role/backend based on the current scaling tier index."""
        # Ensure model_tier_indices exist in state
        indices = self.state.setdefault("model_tier_indices", {})
        if role == "assistant" and "assistant" not in indices:
            idx = indices.setdefault(role, 1)  # Default to index 1 (gemma2:2b) for assistant
        else:
            idx = indices.setdefault(role, 0)
        
        # Look up model tiers for this backend
        tiers = self.config.get("model_tiers", {}).get(backend, [])
        if not tiers:
            return None
            
        # Bound index to tiers list
        if idx < len(tiers):
            return tiers[idx]
        return tiers[-1]

    def escalate_developer_backend(self):
        """Escalates the developer dynamically (vertical model upgrade first, then horizontal backend upgrade)."""
        current_dev = self.config["backends"].get("developer", "codex")
        
        # Ensure tracking variables exist
        indices = self.state.setdefault("model_tier_indices", {})
        idx = indices.get("developer", 0)
        
        # Check if we can upgrade the model within the current backend (vertical scaling)
        tiers = self.config.get("model_tiers", {}).get(current_dev, [])
        if idx + 1 < len(tiers):
            # Upgrade model tier
            indices["developer"] = idx + 1
            new_model = tiers[idx + 1]
            log_warning(f"[!] Dynamic Vertical Scale: Upgraded developer model on '{current_dev}' to '{new_model}' (Tier {idx + 1})")
            self.save_state()
            return
            
        # If we cannot upgrade model anymore, perform horizontal escalation (switch backend)
        escalation_path = self.config.get("backend_escalation_path", ["ollama", "agy", "codex"])
        if current_dev in escalation_path:
            curr_idx = escalation_path.index(current_dev)
            if curr_idx + 1 < len(escalation_path):
                new_dev = escalation_path[curr_idx + 1]
                log_warning(f"[!] Dynamic Horizontal Scale: Escalated Developer backend from '{current_dev}' to '{new_dev}'")
                self.config["backends"]["developer"] = new_dev
                # Reset model tier for the new backend
                indices["developer"] = 0
                
                # Save updated config and state
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=2)
                self.save_state()
                return

        log_warning("[!] Already at highest Developer model and backend escalation tier.")

    def step_reviewing_plan(self):
        lang = self.config.get("language")
        log_header(tr("phase.reviewing_plan", lang))
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()

        prompt = tr("reviewer.plan_prompt", lang, requirements=requirements, plan=plan)

        system_prompt = tr("reviewer.plan_system", lang)
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
        log_info(f"Reviewer response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().startswith("APPROVED")
        
        if is_approved:
            log_success("Implementation plan APPROVED by Reviewer!")
            self.state["state"] = "IMPLEMENTING"
            self.save_state()
        else:
            log_warning("Implementation plan REJECTED by Reviewer.")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["plan_revisions"] < max_rev:
                self.state["plan_revisions"] += 1
                self.state["state"] = "DEVELOPING_PLAN"
                self.save_state()
                log_info(f"Revising plan (Revision {self.state['plan_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max plan revisions. Proceeding to implementation anyway (Owner override).")
                self.state["state"] = "IMPLEMENTING"
                self.save_state()

    def parse_and_write_files(self, text: str) -> list[str]:
        import re
        pattern = re.compile(r'\[FILE_START:\s*(.*?)\](.*?)\[FILE_END:\s*\1\]', re.DOTALL)
        matches = pattern.findall(text)
        
        written_files = []
        for filepath_str, content in matches:
            filepath_str = filepath_str.strip()
            content = content.strip()
            
            # Strip potential leading/trailing markdown code block wrappers
            if content.startswith("```"):
                lines = content.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
                
            # Determine base directory
            base_dir = self.workspace
            if self.config.get("use_worktree", True):
                wt_path = self.ai_dir / "worktree"
                if wt_path.exists():
                    base_dir = wt_path

            target_path = (base_dir / filepath_str).resolve()
            # Safety check: ensure it is inside base_dir
            if base_dir not in target_path.parents and target_path != base_dir:
                log_warning(f"Skipping file write outside target directory: {filepath_str}")
                continue
                
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            written_files.append(filepath_str)
            log_success(f"Developer wrote file: {filepath_str}")
        return written_files

    def step_implementing(self):
        lang = self.config.get("language")
        log_header(tr("phase.implementing_code", lang))
        if not self.requirements_path.exists() or not self.plan_path.exists():
            log_error("Requirements or Plan missing. Cannot implement.")
            sys.exit(1)

        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()

        tasks = self.state.get("tasks", [])
        pending_tasks = [t for t in tasks if t["status"] == "pending"]

        if not pending_tasks:
            log_success("All tasks are already marked completed.")
            self.state["state"] = "TESTING"
            self.save_state()
            return

        log_info(f"Found {len(pending_tasks)} pending tasks out of {len(tasks)} total tasks.")
        
        # We can implement task by task
        developer_logs = []
        backend = self.config["backends"].get("developer", "codex")
        for task in pending_tasks:
            log_info(f"Implementing Task {task['id']}: {task['description']}")
            
            prompt = (
                f"We are implementing the project in the workspace root. Here are the requirements:\n"
                f"```markdown\n{requirements}\n```\n\n"
                f"And here is our implementation plan:\n"
                f"```markdown\n{plan}\n```\n\n"
                f"Please implement the following task:\n"
                f"Task ID: {task['id']}\n"
                f"Description: {task['description']}\n\n"
            )
            
            if backend in ["ollama", "gemini", "agy"]:
                prompt += (
                    "Please write the code for any files that need to be created or modified. "
                    "You MUST wrap the code for each file exactly inside the following file-marker blocks:\n"
                    "[FILE_START: path/to/file.ext]\n"
                    "// code contents here\n"
                    "[FILE_END: path/to/file.ext]\n\n"
                    "Make sure the path is relative to the project root. "
                    "Only output files wrapped in this format will be modified in the repository. "
                    "Explain your changes briefly outside these blocks."
                )
            else:
                prompt += "Modify the code files directly in the repository. Provide details of the changes you make."
            
            if self.state["code_revisions"] > 0:
                feedback = ""
                if self.qa_report_path.exists():
                    with open(self.qa_report_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- QA Feedback ---\n{f.read()}\n"
                if self.reviewer_output_path.exists():
                    with open(self.reviewer_output_path, "r", encoding="utf-8") as f:
                        feedback += f"\n--- Code Review Feedback ---\n{f.read()}\n"
                if feedback:
                    prompt += f"\n\nNote: The previous implementation had issues. Feedback:\n{feedback}\nPlease fix these issues."

            # We use Developer backend to make modifications
            system_prompt = "You are an expert AI Developer. Write and edit code to fulfill the task."
            dev_output = self.call_agent("developer", prompt, system_prompt)
            
            # If text-based API backend, parse and write files
            if backend in ["ollama", "gemini", "agy"]:
                written = self.parse_and_write_files(dev_output)
                if written:
                    log_success(f"Successfully processed files written by Developer: {', '.join(written)}")
                else:
                    log_warning("No files were parsed from Developer response. Ensure they used [FILE_START: path] blocks.")
            
            developer_logs.append(f"--- Task {task['id']} implementation output ---\n{dev_output}\n")
            
            # Mark task completed
            task["status"] = "completed"
            self.save_state()
            
            # Save step output
            with open(self.action_items_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2)

        # Write cumulative developer logs
        with open(self.developer_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(developer_logs))

        log_success("All pending tasks processed.")
        self.state["state"] = "TESTING"
        self.save_state()

    def step_testing(self):
        lang = self.config.get("language")
        log_header(tr("phase.testing_verification", lang))
        test_cmd = self.config.get("test_command", "git diff --stat")
        log_info(f"Running test command: {test_cmd}")
        
        # split command safely (assuming bash execution for custom command)
        code, output = self.run_command(["bash", "-c", test_cmd], timeout=600, capture=True)
        
        with open(self.test_results_path, "w", encoding="utf-8") as f:
            f.write(f"Command: {test_cmd}\nExit Code: {code}\nOutput:\n{output}")
            
        log_info(f"Test exit code: {code}")
        
        # Now run QA agent analysis
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        # get git diff
        _, git_diff = self.run_command(["git", "diff", "master"], capture=True)
        if not git_diff.strip():
            _, git_diff = self.run_command(["git", "diff"], capture=True)

        qa_prompt = tr("qa.report_prompt", lang, requirements=requirements, plan=plan, git_diff=git_diff, output=output, code=code)
        
        system_prompt = tr("qa.report_system", lang)
        qa_report = self.call_agent("qa", qa_prompt, system_prompt)
        
        with open(self.qa_report_path, "w", encoding="utf-8") as f:
            f.write(qa_report)
        log_success(f"QA report generated and saved to {self.qa_report_path}")
        
        is_passed = "PASSED" in qa_report.upper()[:1000]
        
        if is_passed:
            log_success("QA verification PASSED!")
            self.state["state"] = "REVIEWING_CODE"
            self.save_state()
        else:
            log_warning("QA verification FAILED!")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Mark tasks as pending to trigger re-implementation with QA report feedback
                for t in self.state["tasks"]:
                    t["status"] = "pending"
                self.save_state()
                log_info(f"Revising code based on QA report (Revision {self.state['code_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max code revisions. Proceeding to final architect review.")
                self.state["state"] = "REVIEWING_CODE"
                self.save_state()

    def step_reviewing_code(self):
        lang = self.config.get("language")
        log_header(tr("phase.reviewing_code", lang))
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        with open(self.plan_path, "r", encoding="utf-8") as f:
            plan = f.read()
        with open(self.test_results_path, "r", encoding="utf-8") as f:
            test_results = f.read()

        # Get git diff
        _, git_diff = self.run_command(["git", "diff", "master"], capture=True)
        if not git_diff.strip():
            _, git_diff = self.run_command(["git", "diff"], capture=True)

        prompt = tr("reviewer.code_prompt", lang, requirements=requirements, plan=plan, test_results=test_results, git_diff=git_diff)

        system_prompt = tr("reviewer.code_system", lang)
        review = self.call_agent("reviewer", prompt, system_prompt)

        with open(self.reviewer_output_path, "w", encoding="utf-8") as f:
            f.write(review)
            
        log_info(f"Code Review response saved. Preview:\n{review[:200]}...")

        is_approved = review.strip().upper().startswith("APPROVED")
        
        if is_approved:
            log_success("Code changes APPROVED by Reviewer!")
            self.state["state"] = "COMPLETED"
            self.save_state()
        else:
            log_warning("Code changes REJECTED by Reviewer.")
            self.escalate_developer_backend()
            max_rev = self.config.get("max_revisions", 2)
            if self.state["code_revisions"] < max_rev:
                self.state["code_revisions"] += 1
                self.state["state"] = "IMPLEMENTING"
                # Instead of marking all original tasks as pending, append a new task containing Reviewer's feedback
                fix_task_id = f"FIX-REV-{self.state['code_revisions']}"
                self.state["tasks"].append({
                    "id": fix_task_id,
                    "description": f"Fix Code Review Issues. Feedback from Reviewer:\n{review[:2000]}",
                    "status": "pending"
                })
                self.save_state()
                log_info(f"Revising implementation (Revision {self.state['code_revisions']}/{max_rev})...")
            else:
                log_warning("Reached max code review revisions. Pausing for human review.")
                self.state["state"] = "WAITING_FOR_OWNER"
                self.save_state()

    def step_completed(self):
        lang = self.config.get("language")
        log_header(tr("phase.generating_summary", lang))
        
        with open(self.request_path, "r", encoding="utf-8") as f:
            request = f.read()
        with open(self.requirements_path, "r", encoding="utf-8") as f:
            requirements = f.read()
        
        # Get final git diff stat
        wt_path = self.ai_dir / "worktree"
        if self.config.get("use_worktree", True) and wt_path.exists():
            _, diff_stat = self.run_command(["git", "diff", "--stat", "master"], cwd=wt_path)
            _, diff_patch = self.run_command(["git", "diff", "master"], cwd=wt_path)
        else:
            _, diff_stat = self.run_command(["git", "diff", "--stat"])
            _, diff_patch = self.run_command(["git", "diff"])

        prompt = tr("manager.final_report_prompt", lang, request=request, requirements=requirements, diff_stat=diff_stat)

        system_prompt = tr("manager.final_report_system", lang)
        summary = self.call_manager(prompt, system_prompt)

        with open(self.final_report_path, "w", encoding="utf-8") as f:
            f.write(summary)
            
        log_success(f"Final project report generated at {self.final_report_path}")
        
        # Assistant generates CHANGELOG
        log_info("Asking Assistant to generate CHANGELOG.md...")
        changelog_prompt = tr("assistant.changelog_prompt", lang, summary=summary, diff_patch=diff_patch[:5000])
        changelog_system = tr("assistant.changelog_system", lang)
        changelog = self.call_agent("assistant", changelog_prompt, changelog_system)
        
        with open(self.workspace / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write("\n\n" + changelog)
            
        log_success("CHANGELOG.md updated successfully!")
        
        # Merge and clean up isolated worktree
        self.cleanup_worktree(merge=True)
        
        log_success("Multi-agent workflow process has finished successfully!")

    # Flow Controller
    def step(self):
        self.load_config_and_state()
        current_state = self.state["state"]
        log_info(f"Current state is: {current_state}")

        if current_state == "PLANNING":
            self.step_planning()
        elif current_state == "DEVELOPING_PLAN":
            self.step_developing_plan()
        elif current_state == "REVIEWING_PLAN":
            self.step_reviewing_plan()
        elif current_state == "IMPLEMENTING":
            self.step_implementing()
        elif current_state == "TESTING":
            self.step_testing()
        elif current_state == "REVIEWING_CODE":
            self.step_reviewing_code()
        elif current_state == "COMPLETED":
            self.step_completed()
        elif current_state == "WAITING_FOR_OWNER":
            log_warning("Currently waiting for owner approval. Edit state.json to resume.")
        elif current_state == "FAILED":
            log_error("Workflow failed. Please reset or start a new task.")
        else:
            log_error(f"Unknown state: {current_state}")

    def run_to_end(self):
        self.load_config_and_state()
        while self.state["state"] not in ["COMPLETED", "WAITING_FOR_OWNER", "FAILED"]:
            self.step()
            self.load_config_and_state()
        if self.state["state"] == "COMPLETED":
            self.step()  # Generates summary report

def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser("init", help="Initialize the .ai-company folder and configuration")
    
    start_parser = subparsers.add_parser("start", help="Start a new multi-agent task")
    start_parser.add_argument("prompt", type=str, help="The development task prompt or request")

    subparsers.add_parser("step", help="Run the next step in the state machine")
    subparsers.add_parser("run", help="Run the agent loop to completion")
    subparsers.add_parser("status", help="Get current orchestrator status and logs info")
    
    reset_parser = subparsers.add_parser("reset", help="Reset the orchestrator state")
    reset_parser.add_argument("--state", type=str, default="PLANNING", help="Reset state to specific value (default: PLANNING)")

    backend_parser = subparsers.add_parser("set-backend", help="Set the agent backend for a role")
    backend_parser.add_argument("role", choices=["manager", "developer", "reviewer", "qa"], help="The agent role")
    backend_parser.add_argument("backend", choices=["ollama", "codex", "claude", "gemini", "agy"], help="The backend to use")

    args = parser.parse_args()

    workspace = Path(os.getcwd())
    orchestrator = AgentOrchestrator(workspace)

    if args.command == "init":
        orchestrator.init_project()
    elif args.command == "start":
        orchestrator.init_project()
        orchestrator.load_config_and_state()
        
        # Save request prompt
        with open(orchestrator.request_path, "w", encoding="utf-8") as f:
            f.write(args.prompt)
        log_success(f"Saved request to {orchestrator.request_path}")
        
        # Reset state to PLANNING
        orchestrator.state = {
            "state": "PLANNING",
            "plan_revisions": 0,
            "code_revisions": 0,
            "tasks": []
        }
        orchestrator.save_state()
        log_success("Orchestrator initialized and ready to run. Run 'python3 orchestrator.py run' to execute.")
    elif args.command == "step":
        orchestrator.step()
    elif args.command == "run":
        orchestrator.run_to_end()
    elif args.command == "status":
        try:
            orchestrator.load_config_and_state()
            lang = orchestrator.config.get("language")
            log_header(tr("status.header", lang))
            print(f"{tr('status.current_state', lang) + ':':<20}{Colors.BOLD}{orchestrator.state['state']}{Colors.ENDC}")
            print(f"{tr('status.plan_revisions', lang) + ':':<20}{orchestrator.state['plan_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"{tr('status.code_revisions', lang) + ':':<20}{orchestrator.state['code_revisions']}/{orchestrator.config['max_revisions']}")
            print(f"Ollama Model:       {orchestrator.config['ollama_model']}")
            print(f"{tr('status.developer_backend', lang) + ':':<20}{orchestrator.config['backends']['developer']}")
            print(f"{tr('status.reviewer_backend', lang) + ':':<20}{orchestrator.config['backends']['reviewer']}")
            print(f"{tr('status.qa_backend', lang) + ':':<20}{orchestrator.config['backends'].get('qa', 'ollama')}")
            print(f"{tr('status.test_command', lang) + ':':<20}{orchestrator.config['test_command']}")
            
            tasks = orchestrator.state.get("tasks", [])
            if tasks:
                print(f"\n{tr('status.action_items', lang)} ({len(tasks)} total):")
                for t in tasks:
                    status_color = Colors.GREEN if t['status'] == 'completed' else Colors.WARNING
                    print(f" - [{status_color}{t['status']}{Colors.ENDC}] {t['id']}: {t['description']}")
            else:
                print(f"\n{tr('status.no_tasks', lang)}")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "reset":
        try:
            orchestrator.load_config_and_state()
            orchestrator.state["state"] = args.state
            orchestrator.state["plan_revisions"] = 0
            orchestrator.state["code_revisions"] = 0
            orchestrator.state["model_tier_indices"] = {
                "developer": 0,
                "reviewer": 0,
                "manager": 0,
                "qa": 0
            }
            orchestrator.cleanup_worktree(merge=False)
            orchestrator.save_state()
            log_success(f"State reset to {args.state}")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    elif args.command == "set-backend":
        try:
            orchestrator.load_config_and_state()
            orchestrator.config["backends"][args.role] = args.backend
            with open(orchestrator.config_path, "w", encoding="utf-8") as f:
                json.dump(orchestrator.config, f, indent=2)
            log_success(f"Successfully configured '{args.role}' backend to '{args.backend}'")
        except FileNotFoundError:
            log_error("Project not initialized. Please run 'python3 orchestrator.py init' first.")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
