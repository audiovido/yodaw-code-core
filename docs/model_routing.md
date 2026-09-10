# Model routing

The adaptive router picks a provider and model per task without
hardcoding any vendor into core orchestration. Selection reads
generic capability metadata only.

## Flow

1. `build_task_profile` turns task metadata into a `TaskProfile`
   (category, complexity, estimated context, reasoning, coding,
   tool, latency, and cost needs).
2. `ModelCapabilityRegistry` holds one `ModelCapability` per
   provider and model pair (context window, coding and reasoning
   scores, tool support, local or remote, cost, latency,
   reliability).
3. `AdaptiveRouter.select` filters by the `BudgetPolicy`, scores
   every eligible model deterministically, and returns the best
   pick plus a bounded fallback chain.
4. `execute_with_failover` walks the chain on timeout, 429, 5xx,
   auth-unavailable, and provider-unavailable failures.
   `BLOCKED_EXTERNAL` applies only after every eligible fallback
   fails.
5. `HistoryStore` folds benchmark and eval history into scoring
   only through explicit durable records. Corrupted rows are
   skipped, never applied silently.

## Provider descriptors

`app/providers` describes each reachable backend (`openai_compatible`,
`anthropic_compatible`, `ollama_local`, `generic_http`) with its
base URL, default model, cost, latency, capability scores, and
availability. Descriptors project into registry capabilities, so
new backends plug in without touching the selector.

## Policies

`BudgetPolicy` enforces `max_cost`, `max_latency_ms`,
`allowed_providers`, `denied_providers`, `local_only`, and
`remote_only` before scoring. Local-first mode (`prefer_local`)
adds a bonus to a local model that already meets the minimum
capability threshold, and otherwise falls back to remote.

## Agent runtime

`AgentRuntimeRouter` in `app/runtime/router` is the single
entrypoint: `route` returns the profile plus the routing
decision, and `run` executes the bounded fallback chain and
records each attempt to history.

## Hermetic tests

Routing tests use fake registries, injected history, and scripted
callables. No live network is used.
