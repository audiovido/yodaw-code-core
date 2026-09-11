We have set up the graduation test suite in the yodaw-graduation-tests worktree.
The test suite is in tests/test_serving_path_graduation.py.
We have run a subset of the tests and generated a report in YODAW_SERVING-PATH_GRADUATION_REPORT.md.

Since we did not find the Worker A branch (yodaw/serving-path-integration), we marked the implementation SHA as unknown and several tests as WAITING_ON_WORKER_A.

When Worker A's branch appears, we should:
1. Fetch the latest branch
2. Rebase our test-only branch onto it
3. Run the targeted graduation suite
4. Run the full pytest suite
5. Run real HTTP E2E
6. Report blockers

We have not modified any production code, only added tests and documentation.

The test suite uses the public HTTP API and temporary Git repositories as required.