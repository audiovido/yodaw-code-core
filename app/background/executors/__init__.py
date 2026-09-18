"""Normalized executor adapters for the background Task API."""

from app.background.executors.base import (
    ExecutionContext,
    Executor,
    StreamResult,
    build_executor_prompt,
    changed_files,
    stream_command,
)
from app.background.executors.claude_code import ClaudeCodeExecutor
from app.background.executors.codex import CodexExecutor
from app.background.executors.grok_cli import GrokCliExecutor
from app.background.executors.kodgar_native import KodgarNativeExecutor
from app.background.executors.registry import (
    DEFAULT_BY_TASK_TYPE,
    FALLBACK_ORDER,
    ExecutorRegistry,
    default_registry,
)

__all__ = [
    "ClaudeCodeExecutor",
    "CodexExecutor",
    "DEFAULT_BY_TASK_TYPE",
    "ExecutionContext",
    "Executor",
    "ExecutorRegistry",
    "FALLBACK_ORDER",
    "GrokCliExecutor",
    "KodgarNativeExecutor",
    "StreamResult",
    "build_executor_prompt",
    "changed_files",
    "default_registry",
    "stream_command",
]
