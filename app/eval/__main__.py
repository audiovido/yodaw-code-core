"""
CLI interface for YODAW evaluation framework.
"""
import sys
import argparse
from pathlib import Path

from app.eval.benchmarks import ALL_BENCHMARKS, get_benchmark_by_id, get_benchmarks_by_tag
from app.eval.runner import BenchmarkRunner, ComparisonRunner, print_report_summary


def main():
    parser = argparse.ArgumentParser(
        description="YODAW Coder Evaluation Framework"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Run command
    run_parser = subparsers.add_parser("run", help="Run benchmark suite")
    run_parser.add_argument(
        "--case",
        type=str,
        help="Run specific benchmark case by ID",
    )
    run_parser.add_argument(
        "--tag",
        type=str,
        help="Run benchmarks with specific tag",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        help="Use live YODAW Coder (default: hermetic)",
    )
    run_parser.add_argument(
        "--output",
        type=str,
        help="Output JSON report to file",
    )
    
    # Compare command
    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare two revisions"
    )
    compare_parser.add_argument("base_sha", help="Base revision SHA")
    compare_parser.add_argument("candidate_sha", help="Candidate revision SHA")
    compare_parser.add_argument(
        "--output",
        type=str,
        help="Output comparison report to file",
    )
    
    # Live command (real provider)
    live_parser = subparsers.add_parser(
        "live",
        help="Run live benchmark with a real provider",
    )
    live_parser.add_argument(
        "--provider",
        default="openai-compatible",
        choices=["openai-compatible", "ollama"],
        help="Provider kind",
    )
    live_parser.add_argument("--model", required=True, help="Model name")
    live_parser.add_argument(
        "--base-url", required=True, help="Provider base URL"
    )
    live_parser.add_argument(
        "--api-key-env",
        default="YODAW_EVAL_API_KEY",
        help="Environment variable holding the API key",
    )
    live_parser.add_argument(
        "--case",
        type=str,
        help="Run specific benchmark case by ID",
    )
    live_parser.add_argument(
        "--suite",
        type=str,
        default="all",
        help="Tag filter for benchmark cases, or 'all'",
    )
    live_parser.add_argument(
        "--output",
        type=str,
        help="Output JSON report to file",
    )
    live_parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds",
    )
    live_parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Bounded provider retries",
    )
    live_parser.add_argument(
        "--artifact-dir",
        type=str,
        default=None,
        help="Directory to preserve failed case worktrees",
    )

    # List command
    list_parser = subparsers.add_parser("list", help="List available benchmarks")
    list_parser.add_argument(
        "--tag",
        type=str,
        help="Filter by tag",
    )
    
    args = parser.parse_args()
    
    if args.command == "run":
        return run_benchmarks(args)
    elif args.command == "live":
        return run_live(args)
    elif args.command == "compare":
        return compare_revisions(args)
    elif args.command == "list":
        return list_benchmarks(args)
    else:
        parser.print_help()
        return 1


def run_benchmarks(args):
    """Execute benchmark run."""
    hermetic = not args.live
    
    if args.live:
        print("ERROR: Live evaluation mode not yet implemented")
        print("Use hermetic mode (default) for now")
        return 1
    
    runner = BenchmarkRunner(hermetic=hermetic)
    
    # Run suite
    report = runner.run_suite(
        ALL_BENCHMARKS,
        case_filter=args.case,
        tag_filter=args.tag,
    )
    
    # Print summary
    print_report_summary(report)
    
    # Save JSON if requested
    if args.output:
        Path(args.output).write_text(report.to_json())
        print(f"\nJSON report saved to: {args.output}")
    
    # Exit code based on acceptance gates
    if report.failed > 0:
        return 1
    return 0


def run_live(args):
    """Execute live benchmark run via the live CLI module."""
    from app.eval.live.cli import main as live_main

    argv = [
        "--provider", args.provider,
        "--model", args.model,
        "--base-url", args.base_url,
        "--api-key-env", args.api_key_env,
        "--suite", args.suite or "all",
        "--timeout-s", str(args.timeout_s),
        "--max-retries", str(args.max_retries),
    ]
    if args.case:
        argv += ["--case", args.case]
    if args.output:
        argv += ["--output", args.output]
    if args.artifact_dir:
        argv += ["--artifact-dir", args.artifact_dir]
    return live_main(argv)


def compare_revisions(args):
    """Compare two revisions."""
    runner = BenchmarkRunner(hermetic=True)
    comparator = ComparisonRunner(runner)
    
    comparison = comparator.compare_revisions(
        args.base_sha,
        args.candidate_sha,
        ALL_BENCHMARKS,
    )
    
    print("\n" + "=" * 60)
    print("REVISION COMPARISON")
    print("=" * 60)
    print(f"Base: {comparison['base_sha']}")
    print(f"Candidate: {comparison['candidate_sha']}")
    print(f"Score Delta: {comparison['score_delta']:.2f}")
    print()
    print(f"Newly Passed: {len(comparison['newly_passed'])}")
    print(f"Newly Failed: {len(comparison['newly_failed'])}")
    print(f"Unchanged Failures: {len(comparison['unchanged_failures'])}")
    print("=" * 60)
    
    if args.output:
        import json
        Path(args.output).write_text(json.dumps(comparison, indent=2))
        print(f"\nComparison report saved to: {args.output}")
    
    return 0


def list_benchmarks(args):
    """List available benchmarks."""
    benchmarks = ALL_BENCHMARKS
    if args.tag:
        benchmarks = get_benchmarks_by_tag(args.tag)
    
    print(f"\nAvailable Benchmarks ({len(benchmarks)}):")
    print("=" * 60)
    for bench in benchmarks:
        print(f"{bench.id}")
        print(f"  Title: {bench.title}")
        print(f"  Intent: {bench.intent.value}")
        print(f"  Difficulty: {bench.difficulty}")
        print(f"  Tags: {', '.join(bench.tags)}")
        print()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
