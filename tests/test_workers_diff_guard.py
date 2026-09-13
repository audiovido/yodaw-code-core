"""Focused tests for diff_guard: secret detection, credential paths,
placeholder exemption, and artifact warnings."""

from app.workers.diff_guard import scan, scan_paths, validate_patch


def _hunk(file, added_line):
    return "diff --git a/%s b/%s\n+++ b/%s\n@@ -1,1 +1,2 @@\n+%s\n" % (
        file,
        file,
        file,
        added_line,
    )


def test_secret_detection_blocks():
    report = scan(_hunk("app.py", 'API_KEY = "hunter2secretvalue123"'))
    assert report["blocking"]
    assert any(f["kind"] == "secret" for f in report["blocking"])
    assert report["ok"] is False


def test_aws_key_detection():
    report = scan(_hunk("config.py", "aws_access_key = AKIAIOSFODNN7EXAMPLE"))
    assert any(f["kind"] == "secret" for f in report["blocking"])


def test_github_token_detection():
    token = "ghp_" + "A" * 36
    report = scan(_hunk("ci.yml", "token: %s" % token))
    assert any(f["kind"] == "secret" for f in report["blocking"])


def test_secrets_are_redacted():
    secret = "sk-SUPERSECRETVALUE1234567890"
    report = scan(_hunk("app.py", "key = %s" % secret))
    matches = [f["match"] for f in report["blocking"]]
    assert all(secret not in m for m in matches)


def test_credential_path_detection():
    report = scan(_hunk(".env", "DATABASE_URL=postgres://u:p@localhost/db"))
    assert any(f["kind"] == "credential_path" for f in report["blocking"])
    assert report["ok"] is False


def test_credential_path_scan_paths():
    report = scan_paths(["config/credentials.json", "src/app.py"])
    assert len(report["credential_paths"]) == 1
    assert report["blocking"]


def test_placeholder_exemption():
    report = scan(_hunk("app.py", "api_key = YOUR_API_KEY_HERE"))
    assert report["blocking"] == []
    assert report["placeholder_exemptions"] >= 1
    assert report["ok"] is True


def test_placeholder_masking_exemption():
    report = scan(_hunk("app.py", 'password = "********"'))
    assert report["blocking"] == []
    assert report["placeholder_exemptions"] >= 1


def test_placeholder_docs_exemption():
    report = scan(_hunk("README.md", "token = <PASTE_TOKEN_HERE>"))
    assert report["blocking"] == []
    assert report["placeholder_exemptions"] >= 1


def test_real_secret_not_exempted_as_placeholder():
    report = scan(_hunk("app.py", "token = zz_secretvalue_zz_12345"))
    assert report["blocking"]


def test_artifact_warning():
    report = scan(_hunk("dist/bundle.js", "!function(){console.log(1)}()"))
    assert report["blocking"] == []
    assert len(report["warnings"]) == 1
    assert report["warnings"][0]["kind"] == "artifact"
    assert report["ok"] is True


def test_compile_artifact_warning():
    report = scan(_hunk("src/main.o", "x"))
    assert any(f["kind"] == "artifact" for f in report["warnings"])


def test_clean_diff_passes():
    report = validate_patch(_hunk("app.py", "answer = 42"))
    assert report["blocking"] == []
    assert report["warnings"] == []
    assert report["ok"] is True


def test_scan_paths_artifacts():
    report = scan_paths(["node_modules/pkg/index.js"])
    assert len(report["artifacts"]) == 1


def test_scan_is_deterministic():
    diff = (
        _hunk("app.py", 'secret = "secretvalue_abcdef123"')
        + _hunk("dist/b.js", "big()")
    )
    first = scan(diff)
    second = scan(diff)
    assert first == second
    assert first["blocking"] and first["warnings"]