# Self-Improving Loop (Worker Q)

Governed loop that turns benchmark failures into versioned skill and
policy improvements. The full chain is:

```
BENCHMARK -> FAILURE MINING -> ROOT-CAUSE CLUSTER
  -> IMPROVEMENT PROPOSAL -> SKILL/POLICY CANDIDATE
  -> SANDBOX VALIDATION -> REGRESSION CHECK -> APPROVAL GATE
  -> VERSIONED ACTIVATION -> MONITOR -> ROLLBACK
```

## Non-negotiables

- **No silent self-modification.** Proposals move only through explicit
  transitions (`store.transition`). `ACTIVATED` is reachable only via
  `ActivationStore.activate` and `ROLLED_BACK` only via
  `ActivationStore.rollback`; direct transitions raise.
- **No auto-activation.** The loop (`app/improvement/loop.py`) submits
  proposals for review but never approves or activates. Approval needs
  a named reviewer; activation needs an `APPROVED` proposal.
- **Evidence-backed and reversible.** Every proposal carries expected
  impact, risk score, validation plan, benchmark before/after, and
  regression delta. Activations are versioned with lineage, and
  rollback restores the previous version.

## Modules

| Area | Module | Role |
| --- | --- | --- |
| Failure mining | `app/learning/failure_miner.py` | `mine_failures` extracts signals from clean `FAIL_*` eval results; poisoned results are quarantined |
| Clustering | `app/learning/clusters.py` | `cluster_signals` groups by signature; only recurring groups (>= 3) become clusters |
| Proposals | `app/improvement/models.py` | `ImprovementProposal` with approval states, `BenchmarkDelta`, `RegressionDelta` |
| Candidates | `app/improvement/candidates.py` | Deterministic skill/policy candidate builder with unsafe-content and risk refusal |
| Store | `app/improvement/store.py` | SQLite proposals with duplicate suppression; rejections retained |
| Validation | `app/improvement/validation.py` | Sandbox validation (after must beat before) + regression check (no new failures) |
| Approval | `app/improvement/approval.py` | Submit / approve (named reviewer) / reject (reason required) |
| Versioning | `app/improvement/versioning.py` | Monotonic activation versions, lineage, rollback |
| Audit | `app/improvement/audit.py` | Loop events in the existing tamper-evident tenant audit trail |
| Recovery | `app/improvement/recovery.py` | Quarantine corrupt history, rebuild empty stores |
| Loop | `app/improvement/loop.py` | Bounded iteration (`run_iteration`, `run` with `max_iterations`) |

## Integration with existing cores

- **Eval core** (`app/eval/models.py`, `app/eval/scoring.py`): the loop
  consumes `EvaluationResult` records and their failure taxonomy; it
  does not duplicate scoring.
- **Skills core** (`app/skills/registry.py`, `app/skills/learning.py`):
  candidates are grounded in real skill ids via
  `registry.get_by_intent`, recording `target_skill_id` and
  `base_skill_version`. Skill-learning patterns remain the execution
  feedback path; the loop is the governance path for new guidance.
- **Learning core** (`app/learning/engine.py`, `store.py`,
  `retrieval.py`): miner/cluster modules extend learning without
  changing record shapes or retrieval ranking.
- **Audit core** (`app/tenants/audit.py`): the loop reuses the
  append-only hash-chained audit store; no shadow log.

## Poisoned-benchmark protection

`detect_poisoned` quarantines results with suspicious evidence keys
(`override_score`, `inject`, `bypass`, `ignore_regression`,
`disable_validation`, `auto_approve`, `poison`), out-of-bounds scores,
suspicious case ids, conflicting duplicate outcomes, or unknown result
classes. Quarantined results never reach clustering.

## Bounded iteration

`SelfImprovingLoop.run` processes at most `max_iterations` batches
(default 5) and stops early when an iteration yields no recurring
clusters, no new proposals (all duplicates or unsafe), or a
validation/regression failure.

## Tests

- `tests/test_learning_failure_mining.py`: recurring cluster, unique
  failure ignored, pass/blocked silence, poisoned quarantine.
- `tests/test_improvement_loop.py`: proposal creation, duplicate
  suppression, unsafe rejection, validation failure, regression
  rejection, approval gate, activation after approval, rejection
  retention, rollback, version lineage, audit evidence, no silent
  mutation, corruption recovery, bounded loop.

No network in pytest. Run with Python 3.12:

```
python3.12 -m pytest tests/test_learning_failure_mining.py tests/test_improvement_loop.py -q
```
