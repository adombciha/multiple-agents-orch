# Multi-Agent オーケストレーター (Orchestrator)

[繁體中文](README.md) | [English](README_en.md) | [日本語](README_ja.md) | [简体中文](README_zh-CN.md)

本プロジェクトは、Pythonで書かれた軽量なマルチエージェント・オーケストレーターです。ローカルのOllamaモデル（ManagerおよびReviewer）、Codex CLI（Developer）、Claude Codeを統合し、決定論的な状態遷移マシン（State Machine）を用いて、要件定義、実装、テスト、コードレビューという閉ループの開発プロセスを自動実行します。

---

## システムアーキテクチャ

```text
               ユーザー入力
                   ↓
         [ Python Orchestrator ]
                   ↓
         [ Manager (要件分析、タスク分解) ]
                   ↓
  ┌────────────────┬────────────────┐
  │   Developer    │    Reviewer    │
  │  (コード実装)  │(コードレビュー)│
  └────────────────┴────────────────┘
                   ↓
         [ QA Agent (自動検証) ]
                   ↓
         [ Reviewer (コードレビュー) ]
          ├── 承認 (APPROVED) → ブランチをマージし最終レポートを生成
          └── 却下 (REJECTED) → 修正タスク (FIX-TASK) を生成して Developer に差し戻し
                   ↓
         [ Assistant (CHANGELOG.md を自動生成) ]
```

---

## クイックスタート

### 1. 環境の初期化
```bash
python3 orchestrator.py init
```

### 2. 新しいタスクの開始
```bash
python3 orchestrator.py start "連絡先検索機能を追加し、search.py にテストを記述する"
```

### 3. ステップ実行（デバッグや段階的な確認に推奨）
```bash
python3 orchestrator.py step
```

### 4. 全自動実行
```bash
python3 orchestrator.py run
```

### 5. ステータスの確認
```bash
python3 orchestrator.py status
```
