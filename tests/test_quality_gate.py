from app.reuse.quality_gate import score_candidate


def test_good_candidate_scores_high():
    candidate = {
        "name": "good",
        "full_name": "org/good",
        "stars": 5000,
        "forks": 500,
        "archived": False,
        "license": "MIT",
        "pushed_at": "2099-01-01T00:00:00Z",
    }

    result = score_candidate(candidate)

    assert result["decision"] in {"STRONG", "REVIEW"}
    assert result["quality_score"] > 0
    assert not result["blockers"]


def test_archived_candidate_rejected():
    candidate = {
        "name": "old",
        "full_name": "org/old",
        "stars": 10000,
        "forks": 1000,
        "archived": True,
        "license": "MIT",
        "pushed_at": "2099-01-01T00:00:00Z",
    }

    result = score_candidate(candidate)

    assert result["decision"] == "REJECT"
    assert "repository is archived" in result["blockers"]


def test_unapproved_license_rejected():
    candidate = {
        "name": "bad-license",
        "full_name": "org/bad-license",
        "stars": 5000,
        "forks": 500,
        "archived": False,
        "license": "GPL-3.0",
        "pushed_at": "2099-01-01T00:00:00Z",
    }

    result = score_candidate(candidate)

    assert result["decision"] == "REJECT"
