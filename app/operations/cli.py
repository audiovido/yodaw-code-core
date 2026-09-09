"""
Stage 10.9: operations CLI.

Run as:

    python -m app.operations <command> [options]

Commands (no shell-specific hacks, no manual DB edits):

- audit-verify   : walk the tamper-evident chain, report breakage
- audit-prune    : retention prune with optional JSONL archive
- audit-stats    : row count + chain head
- outbox-list    : inspect pending / dead-lettered messages
- outbox-requeue : return dead-lettered messages to the queue
- backup         : consistent SQLite online backup
- status         : runtime/status counters from the store

The CLI is intentionally read-only or explicitly destructive
(only audit-prune mutates history, with --archive recommended).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from app.config import load_config
from app.storage.sqlite_store import MissionStore
from app.tenants.audit import AuditStore


def _stores(args):
    db_path = args.db

    if getattr(args, "database_url", None):
        # Postgres-backed deployments: the stores bind through the
        # same DSN path as the runtime (URL wins over --db).
        from app.storage.pg_store import PostgresStores

        pg = PostgresStores(args.database_url)
        return pg.store, pg.audit

    return MissionStore(db_path), AuditStore(db_path)


def cmd_audit_verify(args) -> int:
    _, audit = _stores(args)
    result = audit.verify()

    print(json.dumps(result, indent=2))

    return 0 if result["intact"] else 2


def cmd_audit_stats(args) -> int:
    _, audit = _stores(args)

    print(
        json.dumps(
            {
                "events": audit.count(),
                "verification": audit.verify(),
            },
            indent=2,
        )
    )

    return 0


def cmd_audit_prune(args) -> int:
    _, audit = _stores(args)

    if args.archive and not args.yes:
        print(
            "audit-prune deletes events; pass --yes to confirm "
            "(archiving to the given path is still recommended)"
        )
        return 1

    result = audit.prune(
        keep_days=args.keep_days,
        archive_path=args.archive,
    )

    print(json.dumps(result, indent=2))

    return 0


def cmd_outbox_list(args) -> int:
    store, _ = _stores(args)

    stats = store.outbox_stats()
    print(json.dumps({"stats": stats}, indent=2))

    if args.dead:
        print(json.dumps({"dead": store.outbox_list_dead()}, indent=2))
    else:
        pending = store.outbox_pending(limit=args.limit)
        print(json.dumps({"pending": pending}, indent=2))

    return 0


def cmd_outbox_requeue(args) -> int:
    store, _ = _stores(args)

    count = store.outbox_requeue_dead(
        args.id if args.id is not None else None
    )

    print(json.dumps({"requeued": count}, indent=2))

    return 0


def cmd_backup(args) -> int:
    source = Path(args.db)

    if not source.exists():
        print(f"no database at {source}")
        return 1

    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(source)

    try:
        # sqlite3's online backup API produces a consistent copy
        # even while writers are active (WAL mode included).
        dst = sqlite3.connect(destination)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    print(json.dumps({"backup": str(destination)}, indent=2))

    return 0


def cmd_status(args) -> int:
    store, _ = _stores(args)

    try:
        cfg = load_config()
        profile = {
            "profile": cfg.profile,
            "backend": cfg.backend,
            "auth_mode": cfg.auth_mode,
            "warnings": cfg.warnings,
        }
    except Exception as exc:
        profile = {"config_error": str(exc)}

    print(
        json.dumps(
            {
                "configuration": profile,
                "missions": store.status_counts(),
                "outbox": store.outbox_stats(),
            },
            indent=2,
        )
    )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operations",
        description="YODAW operations tooling (Stage 10.9)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path (default: YODAW_DB_PATH or "
        "data/yodaw.db)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (default: YODAW_DATABASE_URL); takes "
        "precedence over --db",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("audit-verify", help="verify the audit chain")
    p.set_defaults(func=cmd_audit_verify)

    p = sub.add_parser("audit-stats", help="audit counts + head")
    p.set_defaults(func=cmd_audit_stats)

    p = sub.add_parser("audit-prune", help="retention prune/archive")
    p.add_argument("--keep-days", type=int, required=True)
    p.add_argument(
        "--archive",
        default=None,
        help="JSONL archive path for pruned events",
    )
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_audit_prune)

    p = sub.add_parser("outbox-list", help="inspect outbox messages")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--dead", action="store_true", help="list dead-lettered"
    )
    p.set_defaults(func=cmd_outbox_list)

    p = sub.add_parser(
        "outbox-requeue", help="requeue dead-lettered messages"
    )
    p.add_argument(
        "--id", type=int, default=None, help="one message (default: all)"
    )
    p.set_defaults(func=cmd_outbox_requeue)

    p = sub.add_parser("backup", help="consistent SQLite backup")
    p.add_argument("destination")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("status", help="runtime status counters")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.db is None:
        import os

        args.db = os.environ.get("YODAW_DB_PATH", "data/yodaw.db")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
