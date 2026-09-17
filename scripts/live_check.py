#!/usr/bin/env python3
"""Bounded live 9Router check for acceptance evidence.

Steps (all strictly time-bounded, never installs/starts anything):
  1. /api/health   -> daemon up?
  2. /v1/models    -> inventory
  3. certify_route -> REAL tiny chat on the primary combo
  4. chat()        -> real inference round-trip

Usage: python3 scripts/live_check.py [--base-url http://127.0.0.1:20128]
Prints one JSON verdict; exit 0 = fully healthy, 1 = degraded.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import ninerouter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=ninerouter.DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--api-key-file",
        default=None,
        help="file holding the 9Router gateway key (Bearer for /v1)",
    )
    args = parser.parse_args()
    api_key = ""
    if args.api_key_file:
        api_key = Path(args.api_key_file).read_text(encoding="utf-8").strip()

    started = time.monotonic()
    budget = args.timeout
    report: dict = {"base_url": args.base_url, "steps": {}}

    # 1. daemon health (bounded)
    healthy = ninerouter.daemon_health(
        args.base_url, timeout=min(5.0, budget)
    )
    report["steps"]["daemon"] = {"ok": healthy}
    if not healthy:
        report["verdict"] = "DEGRADED"
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        print(json.dumps(report, indent=2))
        return 1

    # 2. inventory (bounded)
    try:
        listing = ninerouter.list_models(
            args.base_url, api_key, timeout=min(10.0, budget)
        )
        report["steps"]["inventory"] = {
            "ok": True,
            "models": len(listing["models"]),
            "combos": len(listing["combos"]),
            "names": (listing["combos"] + listing["models"])[:6],
        }
    except ninerouter.NinerouterError as exc:
        report["steps"]["inventory"] = {"ok": False, "error": str(exc)[:200]}
        report["verdict"] = "DEGRADED"
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        print(json.dumps(report, indent=2))
        return 1

    # 3. health-aware certification: fall through candidates until a
    # route completes a REAL tiny chat (bounded by per-probe timeout).
    try:
        chain = ninerouter.build_fallback_chain(
            listing["models"],
            listing["combos"],
            base_url=args.base_url,
            api_key=api_key,
            wanted=1,
            timeout=30.0,
        )
    except ninerouter.NinerouterError as exc:
        report["steps"]["certify"] = {"ok": False, "error": str(exc)[:200]}
        report["verdict"] = "DEGRADED"
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        print(json.dumps(report, indent=2))
        return 1
    healthy = chain["healthy"]
    report["steps"]["certify"] = {
        "ok": bool(healthy),
        "primary": healthy[0]["model"] if healthy else None,
        "rejected": [
            f"{r['model']}={r['classification']}" for r in chain["rejected"][:6]
        ],
    }
    if not healthy:
        report["verdict"] = "DEGRADED"
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        print(json.dumps(report, indent=2))
        return 1
    model = healthy[0]["model"]

    # 4. real inference round-trip (bounded)
    try:
        answer = ninerouter.chat(
            "You are a provisioning check. Reply exactly.",
            "Reply with exactly: OK",
            model=model,
            base_url=args.base_url,
            api_key=api_key,
            timeout=30.0,
        )
        report["steps"]["inference"] = {
            "ok": answer.strip() in ("OK", "ok", "Ok"),
            "reply": answer[:120],
        }
    except ninerouter.NinerouterError as exc:
        report["steps"]["inference"] = {"ok": False, "error": str(exc)[:200]}

    report["verdict"] = (
        "HEALTHY"
        if report["steps"]["inference"]["ok"]
        else "DEGRADED"
    )
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
