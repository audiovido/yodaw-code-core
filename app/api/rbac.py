"""
Stage 10.3: role-based access control matrix.

A single explicit permission matrix drives every authorization
decision. Roles:

- superadmin: everything, including managing admins
- operator:   runtime inspection + operational retry/cancel
- auditor:    read-only audit/event visibility
- client:     own missions only (isolation enforced separately
              by the tenancy filter, not by this matrix)
- anonymous:  only the open health endpoint

Add a permission by adding it to one role's set; unknown roles
fail authorization closed.
"""

from __future__ import annotations

PERMISSIONS = {
    "superadmin": {
        "admins.manage",       # create/rotate/disable admins
        "clients.manage",      # create/enable/disable/quota clients
        "missions.create",     # submit missions (as the admin self)
        "missions.read.all",   # read any mission
        "missions.cancel",     # cancel any mission
        "missions.retry",      # operational retry
        "runtime.control",     # start/stop runtime surfaces
        "audit.read",          # full audit trail
        "outbox.manage",       # inspect/requeue/dead-letter outbox
        "tasks.create",        # submit background coding tasks
        "tasks.read.all",      # read any background task
        "tasks.cancel",        # cancel any background task
    },
    "operator": {
        "missions.create",
        "missions.read.all",
        "missions.cancel",
        "missions.retry",
        "audit.read",          # operators may inspect but not mutate
        "outbox.manage",
        "tasks.create",
        "tasks.read.all",
        "tasks.cancel",
    },
    "auditor": {
        "audit.read",
        "missions.read.all",   # read-only observability
        "tasks.read.all",
    },
    "client": {
        # isolation: client reads are scoped to own missions in
        # the API layer; this role has no administrative surface
        "missions.create",
        "missions.cancel.own",  # own missions only (isolation-scoped)
        "tasks.create",
        "tasks.read.all",
        "tasks.cancel.own",
    },
}

ROLE_LEVELS = {
    "superadmin": 3,
    "operator": 2,
    "auditor": 1,
    "client": 0,
}


def role_can(role: str, permission: str) -> bool:
    """True only when the role explicitly holds the permission."""
    return permission in PERMISSIONS.get(role, set())


# Background-task permissions share one prefix convention: `tasks.*`
# gates the Task API, and the `*.own` variants exist for tenant-scoped
# cancellation. They live in the same matrix as everything else so an
# unknown role still fails closed.
TASK_PERMISSIONS = frozenset(
    permission
    for role in PERMISSIONS.values()
    for permission in role
    if permission.startswith("tasks.")
)


def known_role(role: str) -> bool:
    return role in PERMISSIONS
