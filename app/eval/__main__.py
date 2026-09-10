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
