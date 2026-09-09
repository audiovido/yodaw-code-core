from app.workers.code_worker import CodeWorker


def test_real_code_worker_end_to_end():
    worker = CodeWorker()

    result = worker.execute(
        "Add a multiply function and test it"
    )

    assert result["success"] is True
    assert result["output"]["tests_passed"] is True
    assert result["output"]["commit_sha"]
    assert result["output"]["working_tree_clean"] is True
    assert len(result["evidence"]) >= 5
