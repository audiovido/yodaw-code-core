"""Diff scanning for secrets, credential paths, placeholders, and artifacts.

Blocking findings (secrets, credential files) must reject a patch;
artifact warnings are advisory. Placeholder-looking values are exempt
from secret detection so templates and docs do not false-positive.
Findings never echo a full secret: matches are redacted.
"""

from __future__ import annotations

import re
from typing import Optional

_MAX_FINDINGS = 100

# --- Secret patterns -------------------------------------------------
_SECRET_PATTERNS = (
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|"
            r"private[_-]?key|access[_-]?key|client[_-]?secret)\b"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+*=<>-]{8,}"
        ),
    ),
)

# --- Credential file paths -------------------------------------------
CREDENTIAL_PATHS = (
    ".env",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
    ".npmrc",
    ".pypirc",
)
_CREDENTIAL_NAME_RE = re.compile(
    r"(^|/)(\.env|\.netrc|id_(rsa|dsa|ecdsa|ed25519)|"
    r"credentials(\.json)?|secrets\.ya?ml|service-account\.json|"
    r"\.npmrc|\.pypirc)(/|$)"
)

# --- Artifact paths (warnings) ---------------------------------------
_ARTIFACT_PATTERN = re.compile(
    r"(^|/)(dist|build|node_modules|__pycache__)(/|$)|"
    r"\.(pyc|pyo|class|o|obj|exe|dll|so|dylib|a|lib|jar|war|"
    r"min\.js|min\.css|map)$"
)

# --- Placeholder exemption -------------------------------------------
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(<[^>]*>|your[-_]?[a-z]+([-_][a-z0-9]+)*|example[a-z]*|sample[a-z]*|"
    r"placeholder|changeme|redacted|[\"']?([*xX]{5,}|…+)[\"']?|"
    r"\$\{?[A-Z_][A-Z0-9_]*\}?|env:[A-Z_][A-Z0-9_]*|"
    r"getenv\([\"'][A-Z_]+[\"']\)|\[[A-Z_]+\])$",
    re.IGNORECASE,
)

def _redact(value: str) -> str:
    stripped = value.strip().strip("\"'")
    if len(stripped) <= 8:
        return stripped[:2] + "…"
    return stripped[:4] + "…" + stripped[-2:]


def _parse_diff(diff_text: str):
    """Yield (file, line_number, added_line) for added lines in a diff."""
    current_file: Optional[str] = None
    line_number = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[len("+++ b/"):]
            line_number = 0
            continue
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        if raw.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", raw)
            line_number = int(match.group(1)) - 1 if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line_number += 1
            yield current_file, line_number, raw[1:]
        elif raw.startswith("-") or raw.startswith(" "):
            line_number += 1


def scan(diff_text: str, *, max_findings: int = _MAX_FINDINGS) -> dict:
    """Scan a unified diff and return a deterministic report dict.

    Blocking findings are secrets and credential-path writes. Artifact
    edits are warnings. Placeholder-looking secrets are exempt and
    counted separately.
    """
    blocking: list = []
    warnings: list = []
    secrets: list = []
    credential_paths: list = []
    artifacts: list = []
    placeholder_exemptions = 0
    truncated = False

    for file_path, line_no, line in _parse_diff(diff_text):
        if file_path and (
            _CREDENTIAL_NAME_RE.search(file_path)
            or file_path in CREDENTIAL_PATHS
        ):
            finding = {
                "kind": "credential_path",
                "severity": "blocking",
                "file": file_path,
                "line": None,
                "match": file_path,
                "detail": "diff writes a known credential file",
            }
            if finding not in blocking:
                blocking.append(finding)
                credential_paths.append(finding)
            continue

        if file_path or "/" in (line or ""):
            artifact_file = file_path or line
            if _ARTIFACT_PATTERN.search(artifact_file):
                finding = {
                    "kind": "artifact",
                    "severity": "warning",
                    "file": file_path,
                    "line": line_no,
                    "match": _redact(artifact_file),
                    "detail": "diff touches a build artifact",
                }
                if finding not in warnings:
                    warnings.append(finding)
                    artifacts.append(finding)

        for kind, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                if kind == "assigned_secret":
                    value = re.split(r"[:=]\s*", value, maxsplit=1)[-1]
                if _PLACEHOLDER_VALUE_RE.match(value.strip().strip("\"'")):
                    placeholder_exemptions += 1
                    continue
                finding = {
                    "kind": "secret",
                    "severity": "blocking",
                    "file": file_path,
                    "line": line_no,
                    "match": _redact(value),
                    "detail": "possible secret of kind: %s" % kind,
                }
                if finding not in blocking:
                    blocking.append(finding)
                    secrets.append(finding)
                if len(blocking) + len(warnings) >= max_findings:
                    truncated = True
                    return _build_report(
                        blocking, warnings, secrets, credential_paths,
                        artifacts, placeholder_exemptions, truncated,
                    )

    return _build_report(
        blocking, warnings, secrets, credential_paths,
        artifacts, placeholder_exemptions, truncated,
    )


def scan_paths(paths) -> dict:
    """Scan bare relative paths for credential or artifact entries."""
    blocking = []
    warnings = []
    for path in paths:
        if _CREDENTIAL_NAME_RE.search(path):
            finding = {
                "kind": "credential_path",
                "severity": "blocking",
                "file": path,
                "line": None,
                "match": path,
                "detail": "path is a known credential file",
            }
            blocking.append(finding)
        elif _ARTIFACT_PATTERN.search(path):
            finding = {
                "kind": "artifact",
                "severity": "warning",
                "file": path,
                "line": None,
                "match": _redact(path),
                "detail": "path is a build artifact",
            }
            warnings.append(finding)
    return _build_report(
        blocking,
        warnings,
        [f for f in blocking if f["kind"] == "secret"],
        blocking,
        warnings,
        0,
        False,
    )


def validate_patch(diff_text: str, *, max_findings: int = _MAX_FINDINGS) -> dict:
    """Alias for scan(): the patch is safe only when no blocking findings."""
    report = scan(diff_text, max_findings=max_findings)
    report["ok"] = not report["blocking"]
    return report


def _build_report(
    blocking, warnings, secrets, credential_paths,
    artifacts, placeholder_exemptions, truncated,
) -> dict:
    return {
        "ok": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "secrets": secrets,
        "credential_paths": credential_paths,
        "artifacts": artifacts,
        "placeholder_exemptions": placeholder_exemptions,
        "truncated": truncated,
    }