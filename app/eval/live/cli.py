"""Eval ``live`` command: real provider benchmark execution.

Usage:

    python -m app.eval live \
        --provider openai-compatible \
        --model <model> \
        --base-url <url> \
        [--case ID] [--suite python|all] [--output report.json]

The API key is read only from the environment (``--api-key-env`` names
the variable). Secrets are never printed to logs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from app.eval.benchmarks import ALL_BENCHMARKS, get_benchmarks_by_tag
from app.eval.live.executor import (
    LiveBenchmarkExecutor,
    LiveTaskOutcome,
    default_repo_code_coder,
    write_live_report,
)
from app.eval.providers.base import LiveProviderConfig
from app.eval.providers.http import HttpChatProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval live",
        description="Run live evaluation against a real provider.",
    )
    parser.add_argument(
        "--provider",
        default="openai-compatible",
        choices=["openai-compatible", "ollama"],
        help="Provider kind (default: openai-compatible).",
    )
    parser.add_argument("--model", required=True, help="Model name.")
    parser.add_argument(
        "--base-url", required=True, help="Provider base URL."
    )
    parser.add_argument(
        "--api-key-env",
        default="YODAW_EVAL_API_KEY",
        help="Environment variable holding the API key.",
    )
    parser.add_argument(
        "--case", default=None, help="Run a single benchmark case by ID."
    )
    parser.add_argument(
        "--suite",
        default="all",
        help="Tag filter for benchmark cases, or 'all'.",
    )
    parser.add_argument(
        "--output", default=None, help="Write JSON report to this path."
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Bounded provider retries (not counting the first attempt).",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Directory to preserve failed case worktrees.",
    )
    return parser


def select_cases(case: str | None, suite: str) -> list:
    cases = list(ALL_BENCHMARKS)
    if case:
        matched = [c for c in cases if c.id == case]
        if not matched:
            raise SystemExit(f"unknown benchmark case: {case}")
        return matched
    if suite and suite != "all":
        return get_benchmarks_by_tag(suite)
    return cases


def summarize(outcomes: List[LiveTaskOutcome]) -> int:
    print("\n" + "=" * 60)
    print("YODAW LIVE EVALUATION REPORT")
    print("=" * 60)
    for outcome in outcomes:
        print(
            f"{outcome.case_id}: {outcome.result_class} "
            f"(harness={'PASS' if outcome.harness_pass else 'FAIL'} "
            f"provider={'UP' if outcome.provider_available else 'DOWN'} "
            f"task={'PASS' if outcome.task_pass else 'FAIL'} "
            f"score={outcome.benchmark_score:.1f})"
        )
    print("=" * 60)
    blocked = sum(
        1 for o in outcomes if o.result_class == "BLOCKED_EXTERNAL"
    )
    task_pass = sum(1 for o in outcomes if o.task_pass)
    if blocked and not task_pass:
        return 2
    return 0 if task_pass == len(outcomes) else 1


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = LiveProviderConfig(
        kind=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        timeout_s=args.timeout_s,
        max_retries=args.max_retries,
    )
    try:
        config.validate()
    except Exception as exc:
        print(f"ERROR: invalid live provider configuration: {exc}")
        return 2

    described = config.describe()
    print(
        "Live provider: "
        f"{described['kind']} model={described['model']} "
        f"base_url={described['base_url']} "
        f"api_key_set={described['api_key_set']}"
    )

    cases = select_cases(args.case, args.suite)
    if not cases:
        print("No benchmark cases selected.")
        return 1

    provider = HttpChatProvider(config)
    executor = LiveBenchmarkExecutor(
        run_coder=default_repo_code_coder(provider),
        artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
    )
    outcomes = [executor.execute_case(case, provider) for case in cases]

    if args.output:
        write_live_report(outcomes, Path(args.output))
        print(f"\nJSON report saved to: {args.output}")

    return summarize(outcomes)


if __name__ == "__main__":
    sys.exit(main())
