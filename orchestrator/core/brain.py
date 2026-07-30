from __future__ import annotations
import sqlite3
import time
from pathlib import Path

MAX_CONTENT_CHARS = 4000
MAX_INJECT_CHARS = 1500
TOP_K = 3


def _db_path(orchestrator) -> Path:
    return orchestrator.workspace / orchestrator.config.get("brain_db", "brain.db")


def _connect(orchestrator) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(orchestrator))
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(kind UNINDEXED, content, created_at UNINDEXED)"
    )
    return conn


def remember(orchestrator, kind: str, content: str) -> None:
    """Write a lesson/summary to the internal company memory (staff only, sandboxed to
    this workspace). Called only from fixed orchestrator hooks; model output never
    reaches this directly, so a prompt cannot poison long-term memory."""
    content = content.strip()[:MAX_CONTENT_CHARS]
    if not content:
        return
    try:
        conn = _connect(orchestrator)
        with conn:
            conn.execute(
                "INSERT INTO memories (kind, content, created_at) VALUES (?, ?, ?)",
                (kind, content, time.time()),
            )
        conn.close()
    except sqlite3.Error:
        return  # memory is best-effort and must never block the workflow


def recall(orchestrator, query: str, limit: int = TOP_K) -> list[str]:
    terms = " OR ".join(f'"{term}"' for term in query.strip().split()[:12] if term)
    if not terms:
        return []
    try:
        conn = _connect(orchestrator)
        rows = conn.execute(
            "SELECT content FROM memories WHERE memories MATCH ? ORDER BY created_at DESC LIMIT ?",
            (terms, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except sqlite3.Error:
        return []


def inject_prompt(orchestrator, task_description: str) -> str:
    """RAG-style recall for internal staff prompts only; vendor prompts never call this."""
    memories = recall(orchestrator, task_description)
    if not memories:
        return ""
    body = "\n---\n".join(memories)[:MAX_INJECT_CHARS]
    return f"[COMPANY_MEMORY]\n{body}\n[END_COMPANY_MEMORY]"


def prune(orchestrator, keep_last: int = 200) -> int:
    """Delete memories beyond the most recent `keep_last`; returns rows removed."""
    try:
        conn = _connect(orchestrator)
        with conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE rowid NOT IN "
                "(SELECT rowid FROM memories ORDER BY created_at DESC LIMIT ?)",
                (keep_last,),
            )
            deleted = cursor.rowcount
        conn.close()
        return max(deleted, 0)
    except sqlite3.Error:
        return 0
