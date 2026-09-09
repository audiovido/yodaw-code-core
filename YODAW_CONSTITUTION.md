# YODAW Constitution v0.1

YODAW is a general-purpose autonomous execution system.

## Current priority

CODE FIRST.

No Unreal, Unity, Blender, marketing, or unrelated workers
are to be added until the Code Core is production-ready.

## Core loop

GOAL
-> OBSERVE
-> PLAN
-> ROUTE
-> EXECUTE
-> VERIFY
-> EVIDENCE
-> RECOVER
-> LEARN
-> DELIVER

## Non-negotiable principles

1. Single public API surface.
2. One configurable Base URL.
3. Every mission has structured state.
4. Every meaningful execution produces evidence.
5. No silent failure.
6. No infinite retry loops.
7. Git history is mandatory.
8. Meaningful milestones are committed.
9. Prefer proven existing open-source components before writing replacements.
10. New external code must be inspected before integration.
11. Keep the Core independent of Unreal.
12. Workers are replaceable adapters.
13. Terminal-first automation.
14. UI is a client of the API, never the Core itself.
15. Package portability is designed from day one.
16. Server migration must not require architecture changes.
17. Existing dependencies should be reused when compatible.
18. Deterministic code should remain deterministic code; LLM reasoning is used only where judgment is required.

## Current milestone

A portable Code Core that can:

- accept a coding goal,
- plan work,
- inspect repositories,
- select tools,
- work in isolated git workspaces,
- modify code,
- run tests,
- validate output,
- preserve evidence,
- recover from failures,
- commit accepted work,
- expose the full lifecycle through one API.

