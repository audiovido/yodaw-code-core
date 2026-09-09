from app.learning.engine import learn_from_result


def test_learning_record_created():
    record = learn_from_result(
        mission_id="test-mission",
        goal="test goal",
        worker="code-bud",
        success=True,
        evidence=[
            {
                "cmd": "pytest -q",
                "returncode": 0,
                "stdout": "1 passed",
                "stderr": "",
            }
        ],
        result={"ok": True},
    )

    assert record.outcome == "PASS"
    assert record.worker == "code-bud"
    assert "pytest" in record.tools
