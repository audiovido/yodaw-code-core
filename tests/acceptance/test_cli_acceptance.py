"""Black-box CLI acceptance: `python -m app.operations` surface only.

No app imports. Subprocess + JSON stdout assertions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Global --db must precede the subcommand for argparse.
    pre: list[str] = []
    if "--db" not in args and "--database-url" not in args:
        db = os.environ.get("YODAW_DB_PATH", "data/yodaw.db")
        pre = ["--db", db]
    cmd = [sys.executable, "-m", "app.operations", *pre, *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def parse_cli_json_docs(stdout: str) -> list:
    """The CLI prints one JSON document per section; collect them all."""
    decoder = json.JSONDecoder()
    docs = []
    idx = 0
    text = stdout.strip()
    while idx < len(text):
        doc, end = decoder.raw_decode(text, idx)
        docs.append(doc)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return docs


def test_cli_status_reports_counters():
    proc = run_cli("status")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "missions" in payload
    assert "outbox" in payload
    assert "configuration" in payload


def test_cli_audit_verify_reports_chain():
    proc = run_cli("audit-verify")
    assert proc.returncode in (0, 2), proc.stderr
    payload = json.loads(proc.stdout)
    assert "intact" in payload


def test_cli_audit_stats_reports_counts():
    proc = run_cli("audit-stats")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "events" in payload
    assert "verification" in payload


def test_cli_outbox_list_reports_pending():
    proc = run_cli("outbox-list", "--limit", "5")
    assert proc.returncode == 0, proc.stderr
    docs = parse_cli_json_docs(proc.stdout)
    assert any("pending" in d for d in docs)


def test_cli_outbox_list_dead_reports_dead():
    proc = run_cli("outbox-list", "--dead")
    assert proc.returncode == 0, proc.stderr
    docs = parse_cli_json_docs(proc.stdout)
    assert any("dead" in d for d in docs)


def test_cli_backup_produces_copy(tmp_path):
    dest = tmp_path / "backup.db"
    db = os.environ.get("YODAW_DB_PATH", "data/yodaw.db")
    # Ensure source DB exists before backup.
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    Path(db).touch(exist_ok=True)
    proc = run_cli("backup", str(dest))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["backup"] == str(dest)
    assert dest.exists()
