# Remote Worker Pool (Worker R)

Production-safe remote worker abstraction for YODAW job dispatch
to local or remote execution nodes. Transport/runtime contract
first: no cloud vendor deployment, no Docker/Kubernetes required.

## Layout

- `app/remote/protocol.py` — versioned handshake and job messages
  (`PROTOCOL_VERSION`, `negotiate_protocol`, `RegisterRequest`,
  `HeartbeatMessage`, `JobSpec`, `JobAssignment`, `JobResult`,
  `CancelRequest`).
- `app/remote/auth.py` — `TokenAuthenticator` interface plus
  `AllowAllAuthenticator` (dev) and `StaticTokenAuthenticator`
  (shared-secret placeholder for tests/local dev). Production
  plugs HMAC/JWT/mTLS behind `verify()`.
- `app/remote/transport.py` — `DispatcherTransport` remote
  dispatch interface (`send_assignment`, `send_cancel`) and
  `InMemoryTransport` (synchronous, hermetic, no sockets).
- `app/remote/pool.py` — `RemoteWorkerPool`: registration,
  identity, capability/capacity routing, heartbeat, drain,
  stale eviction, reconnect, reassignment, idempotent results.
- `app/remote/dispatcher.py` — `RemoteDispatcher`: submit goals,
  read results, cancel, reassign orphans.
- `app/remote/fake.py` — `FakeRemoteWorker` for tests.
- `app/workers/local_adapter.py` — `LocalWorkerAdapter`: run an
  in-process `Worker` through the pool with remote-equivalent
  semantics (same result path, same idempotency).
- `app/workers/pool_registry.py` — `RemoteWorkerRegistry`:
  existing local `find()`/`status()` plus `dispatch_remote()`.
- `app/workers/remote_worker.py` — re-export of the pool-side
  `WorkerRecord` / `WorkerState` node record.

## Lifecycle

1. Worker registers with id, capabilities, capacity, protocol
   version, auth token. Duplicate live identity is rejected;
   stale or offline identity may reconnect.
2. Pool routes each `JobSpec` to the least-loaded eligible
   worker: capability match, free slot, healthy, not draining,
   and lease-compatible when `repo_key` is set.
3. Heartbeats refresh liveness. Workers past
   `heartbeat_timeout_seconds` are evicted to OFFLINE and their
   pending jobs move to the requeue for reassignment.
4. Drained workers finish in-flight jobs but receive nothing new.
5. `submit_result` is idempotent: first write wins
   (`accepted`), replays return `duplicate`, unknown ids return
   `unknown`. Evidence is stored verbatim.
6. `cancel_job` propagates a `CancelRequest` over the transport;
   `FakeRemoteWorker` and `LocalWorkerAdapter` observe it.

## Protocol negotiation

`negotiate_protocol(client_version)` accepts versions in
`[MIN_PROTOCOL_VERSION, PROTOCOL_VERSION]` (currently `[1, 1]`)
and returns the agreed version. Anything else raises
`ProtocolMismatchError` and registration never happens.

## Lease compatibility

Jobs carrying `repo_key` require a worker advertising
`lease_compatible=True` (registration metadata). This mirrors
the coordinator's per-repository exclusion: repo-mutating work
only lands on nodes that honor the lease contract.

## Testing

Hermetic suites, no network:

- `tests/test_remote_worker_pool.py` — register, duplicate
  identity, capability routing, capacity, heartbeat, stale
  eviction, reconnect, drain, reassignment.
- `tests/test_remote_worker_results.py` — cancel propagation,
  idempotent results, protocol mismatch, auth rejection,
  evidence roundtrip.
- `tests/test_remote_worker_adapters.py` — lease gating, local
  adapter parity, health/drain states, registry surface.
