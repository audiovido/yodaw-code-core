"""
Universal Coding Capability Registry.

A clean, extensible taxonomy of software-engineering capabilities:
frontend, backend languages, databases, mobile, systems, cloud/devops,
and software-engineering disciplines.

Design:
- Capabilities are declared data, not hard-coded behavior.
- New languages/frameworks are added by registering a Capability
  descriptor (no code changes to consumers).
- The registry integrates with the existing app.skills registry:
  each capability maps to SkillIds for the classic intent skills
  where they overlap, so existing planning/execution keeps working.

Phase 2 of the Elite Coding Intelligence layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.skills.models import Intent

# Well-known capability families (Phase 2 taxonomy).
FRONTEND = "frontend"
BACKEND = "backend"
DATABASE = "database"
MOBILE = "mobile"
SYSTEMS = "systems"
CLOUD = "cloud"
DEVOPS = "devops"
ENGINEERING = "engineering"


@dataclass(frozen=True)
class Capability:
    """One coding capability in the registry."""

    key: str                      # stable machine id, e.g. "typescript.react"
    family: str                   # taxonomy family above
    label: str                    # human label, e.g. "React"
    languages: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    skill_ids: Tuple[str, ...] = ()   # existing app.skills ids this maps to
    description: str = ""
    risk_hint: str = "low"        # "low" | "medium" | "high"


def _cap(
    key: str,
    family: str,
    label: str,
    *,
    languages=(),
    tags=(),
    skills=(),
    description="",
    risk="low",
) -> Capability:
    return Capability(
        key=key,
        family=family,
        label=label,
        languages=tuple(languages),
        tags=tuple(tags),
        skill_ids=tuple(skills),
        description=description,
        risk_hint=risk,
    )


# ---------------------------------------------------------------------------
# The universal taxonomy. Declarative: extend this list to add coverage.
# ---------------------------------------------------------------------------

DEFAULT_CAPABILITIES: Tuple[Capability, ...] = (
    # ---- FRONTEND --------------------------------------------------------
    _cap("html.css", FRONTEND, "HTML/CSS", languages=("html", "css"),
         tags=("layout", "responsive"), skills=("feature",),
         description="Semantic HTML, CSS layout, responsive design, accessibility."),
    _cap("javascript", FRONTEND, "JavaScript", languages=("javascript",),
         tags=("ecmascript", "dom"), skills=("feature", "bugfix"),
         description="Core JavaScript: modern ECMAScript, browser APIs, DOM."),
    _cap("typescript", FRONTEND, "TypeScript", languages=("typescript",),
         tags=("types", "strictness"), skills=("feature", "refactor", "bugfix"),
         description="TypeScript typing, generics, strict-mode correctness."),
    _cap("react", FRONTEND, "React", languages=("typescript", "javascript"),
         tags=("hooks", "components", "state"), skills=("feature", "bugfix"),
         description="React components, hooks, state management patterns."),
    _cap("nextjs", FRONTEND, "Next.js", languages=("typescript", "javascript"),
         tags=("ssr", "app-router", "api-routes"), skills=("feature",),
         description="Next.js App/Pages router, SSR/ISR, API routes."),
    _cap("vue", FRONTEND, "Vue", languages=("typescript", "javascript"),
         tags=("composition-api", "sfc"), skills=("feature", "bugfix"),
         description="Vue 3 composition API and single-file components."),
    _cap("svelte", FRONTEND, "Svelte", languages=("typescript", "javascript"),
         tags=("stores", "reactivity"), skills=("feature",),
         description="Svelte reactivity, stores, transitions."),
    _cap("angular", FRONTEND, "Angular", languages=("typescript",),
         tags=("modules", "rxjs", "dependency-injection"), skills=("feature", "bugfix"),
         description="Angular modules, DI, RxJS, signals."),
    _cap("accessibility", FRONTEND, "Accessibility", languages=("html", "javascript"),
         tags=("a11y", "wcag", "aria"), skills=("review", "feature"),
         description="WCAG, ARIA, keyboard navigation, semantic roles."),
    _cap("responsive_ui", FRONTEND, "Responsive UI", languages=("css", "html"),
         tags=("media-queries", "mobile-first"), skills=("feature",),
         description="Responsive and adaptive layout across viewports."),
    _cap("state_management", FRONTEND, "State Management", languages=("typescript", "javascript"),
         tags=("redux", "zustand", "context"), skills=("feature",),
         description="Client state management: redux, zustand, context, server state."),
    _cap("browser_apis", FRONTEND, "Browser APIs", languages=("javascript", "typescript"),
         tags=("fetch", "websocket", "localstorage"), skills=("feature",),
         description="Browser platform APIs: fetch, storage, workers, sensors."),
    _cap("frontend_performance", FRONTEND, "Frontend Performance", languages=("javascript", "typescript"),
         tags=("lcp", "bundling", "lazy"), skills=("performance", "review"),
         description="Core Web Vitals, code-splitting, caching, bundle size."),
    _cap("frontend_testing", FRONTEND, "Frontend Testing", languages=("typescript", "javascript"),
         tags=("vitest", "jest", "playwright"), skills=("test",),
         description="Component/unit/E2E testing: vitest, jest, playwright, testing-library."),
    # ---- BACKEND ---------------------------------------------------------
    _cap("python", BACKEND, "Python", languages=("python",),
         tags=("stdlib", "async"), skills=("feature", "bugfix", "refactor"),
         description="Python idioms, typing, async, packaging."),
    _cap("nodejs", BACKEND, "Node.js", languages=("javascript", "typescript"),
         tags=("event-loop", "streams"), skills=("feature", "bugfix"),
         description="Node.js runtime, event loop, streams, workers."),
    _cap("typescript_backend", BACKEND, "TypeScript Backend", languages=("typescript",),
         tags=("node", "nest"), skills=("feature", "bugfix"),
         description="TypeScript server code, NestJS, tRPC, API typing."),
    _cap("go", BACKEND, "Go", languages=("go",),
         tags=("goroutines", "interfaces"), skills=("feature", "bugfix"),
         description="Go: concurrency, stdlib, testing, tooling."),
    _cap("rust", BACKEND, "Rust", languages=("rust",),
         tags=("ownership", "traits"), skills=("feature", "bugfix", "refactor"),
         description="Rust: ownership, traits, error handling, async."),
    _cap("java", BACKEND, "Java", languages=("java",),
         tags=("jvm", "spring"), skills=("feature", "bugfix"),
         description="Java: JVM, Spring, concurrency, packaging."),
    _cap("kotlin", BACKEND, "Kotlin", languages=("kotlin",),
         tags=("jvm", "coroutines"), skills=("feature", "bugfix"),
         description="Kotlin: coroutines, null-safety, JVM interop."),
    _cap("csharp", BACKEND, "C# / .NET", languages=("csharp",),
         tags=("dotnet", "aspnet"), skills=("feature", "bugfix"),
         description="C# / .NET: ASP.NET Core, EF Core, LINQ, async."),
    _cap("php", BACKEND, "PHP", languages=("php",),
         tags=("composer", "laravel"), skills=("feature", "bugfix"),
         description="PHP: modern 8.x, Composer, Laravel/Symfony."),
    _cap("ruby", BACKEND, "Ruby", languages=("ruby",),
         tags=("rails", "gems"), skills=("feature", "bugfix"),
         description="Ruby: Rails, gems, metaprogramming, testing."),
    _cap("c_cpp_backend", BACKEND, "C/C++ Backend", languages=("c", "cpp"),
         tags=("pointers", "memory"), skills=("feature", "bugfix"),
         description="C/C++ servers, memory management, build systems."),
    _cap("django", BACKEND, "Django", languages=("python",),
         tags=("orm", "admin", "migrations"), skills=("feature", "bugfix"),
         description="Django: ORM, admin, migrations, middleware, templates."),
    _cap("flask", BACKEND, "Flask", languages=("python",),
         tags=("blueprints", "wsgi"), skills=("feature",),
         description="Flask: blueprints, extensions, WSGI deployment."),
    _cap("fastapi", BACKEND, "FastAPI", languages=("python",),
         tags=("pydantic", "async", "openapi"), skills=("feature", "bugfix"),
         description="FastAPI: pydantic models, async, dependency injection, OpenAPI."),
    _cap("express", BACKEND, "Express", languages=("javascript",),
         tags=("middleware", "routes"), skills=("feature", "bugfix"),
         description="Express: middleware chain, routing, error handling."),
    _cap("nestjs", BACKEND, "NestJS", languages=("typescript",),
         tags=("decorators", "modules", "di"), skills=("feature",),
         description="NestJS: modules, DI, decorators, guards, pipes."),
    _cap("api_design", BACKEND, "API Design", languages=(),
         tags=("rest", "graphql", "openapi", "contracts"), skills=("feature", "review"),
         description="REST/GraphQL design, OpenAPI contracts, versioning, idempotency."),
    # ---- DATABASE --------------------------------------------------------
    _cap("postgresql", DATABASE, "PostgreSQL", languages=("sql",),
         tags=("sql", "transactions", "jsonb"), skills=("migration", "bugfix"),
         description="PostgreSQL: SQL, indexes, transactions, JSONB, tuning."),
    _cap("mysql", DATABASE, "MySQL", languages=("sql",),
         tags=("sql", "innodb"), skills=("migration",),
         description="MySQL/MariaDB: schema, indexing, replication."),
    _cap("sqlite", DATABASE, "SQLite", languages=("sql",),
         tags=("embedded", "transactions"), skills=("migration", "bugfix"),
         description="SQLite: constraints, WAL, migrations, locking model."),
    _cap("redis", DATABASE, "Redis", languages=(),
         tags=("cache", "pubsub", "lua"), skills=("feature", "performance"),
         description="Redis: caching, pub/sub, Lua scripts, durability."),
    _cap("mongodb", DATABASE, "MongoDB", languages=("javascript",),
         tags=("document", "aggregation"), skills=("feature", "migration"),
         description="MongoDB: document modeling, aggregation, indexes."),
    _cap("database_migrations", DATABASE, "DB Migrations", languages=("sql",),
         tags=("alembic", "prisma", "flyway"), skills=("migration",),
         description="Migration tooling and safe schema evolution."),
    _cap("schema_design", DATABASE, "Schema Design", languages=("sql",),
         tags=("normalization", "keys"), skills=("migration", "review"),
         description="Relational modeling, keys, constraints, denormalization."),
    _cap("indexing", DATABASE, "Indexing", languages=("sql",),
         tags=("b-tree", "covering"), skills=("performance", "migration"),
         description="Index strategy: B-tree, covering, partial, expression."),
    _cap("query_optimization", DATABASE, "Query Optimization", languages=("sql",),
         tags=("explain", "plans"), skills=("performance",),
         description="EXPLAIN plans, N+1 elimination, join strategies."),
    # ---- MOBILE ----------------------------------------------------------
    _cap("swift_ios", MOBILE, "Swift / iOS", languages=("swift",),
         tags=("uikit", "swiftui"), skills=("feature", "bugfix"),
         description="Swift, UIKit/SwiftUI, async/await, App Store release."),
    _cap("kotlin_android", MOBILE, "Kotlin / Android", languages=("kotlin",),
         tags=("android", "compose", "lifecycle"), skills=("feature", "bugfix"),
         description="Android: Compose, lifecycle, permissions, build variants."),
    _cap("flutter", MOBILE, "Flutter / Dart", languages=("dart",),
         tags=("widgets", "state"), skills=("feature",),
         description="Flutter: widget trees, state management, platform channels."),
    _cap("react_native", MOBILE, "React Native", languages=("javascript", "typescript"),
         tags=("bridge", "native-modules"), skills=("feature", "bugfix"),
         description="React Native: bridge, native modules, hot reload."),
    # ---- SYSTEMS ---------------------------------------------------------
    _cap("c", SYSTEMS, "C", languages=("c",),
         tags=("pointers", "memory"), skills=("feature", "bugfix"),
         description="C: memory model, undefined behavior, toolchains."),
    _cap("cpp", SYSTEMS, "C++", languages=("cpp",),
         tags=("raii", "templates"), skills=("feature", "bugfix", "refactor"),
         description="C++: RAII, templates, STL, modern C++."),
    _cap("concurrency", SYSTEMS, "Concurrency", languages=(),
         tags=("threads", "locks", "async", "races"), skills=("bugfix", "performance"),
         description="Threads, locks, async models, race conditions, deadlocks."),
    _cap("networking", SYSTEMS, "Networking", languages=(),
         tags=("tcp", "http", "dns"), skills=("bugfix", "feature"),
         description="TCP/UDP, HTTP semantics, sockets, packet behavior."),
    _cap("memory_management", SYSTEMS, "Memory Management", languages=("c", "cpp", "rust"),
         tags=("allocators", "gc"), skills=("performance", "bugfix"),
         description="Allocators, GC tuning, leaks, fragmentation."),
    _cap("systems_performance", SYSTEMS, "Systems Performance", languages=(),
         tags=("profiling", "syscall"), skills=("performance",),
         description="Profiling, syscall overhead, cache behavior."),
    # ---- CLOUD / DEVOPS --------------------------------------------------
    _cap("docker", CLOUD, "Docker", languages=(),
         tags=("containers", "images", "compose"), skills=("migration",),
         description="Dockerfiles, images, compose, layer caching, multi-stage."),
    _cap("kubernetes", CLOUD, "Kubernetes", languages=("yaml",),
         tags=("pods", "deployments", "helm"), skills=("migration",),
         description="K8s manifests, Helm, operators, RBAC, probes."),
    _cap("terraform", CLOUD, "Terraform", languages=("hcl",),
         tags=("iac", "providers", "state"), skills=("migration",),
         description="Terraform: HCL, state, providers, modules."),
    _cap("github_actions", DEVOPS, "GitHub Actions", languages=("yaml",),
         tags=("ci", "workflows"), skills=("migration",),
         description="GH Actions workflows, matrix builds, caches, artifacts."),
    _cap("linux", DEVOPS, "Linux", languages=("shell",),
         tags=("shell", "systemd"), skills=("feature", "bugfix"),
         description="Linux: shell, systemd, permissions, filesystems."),
    _cap("shell", DEVOPS, "Shell Scripting", languages=("shell", "bash"),
         tags=("bash", "posix"), skills=("feature",),
         description="Bash/POSIX scripting, error handling, portability."),
    _cap("deployment", DEVOPS, "Deployment", languages=(),
         tags=("release", "rollback"), skills=("migration",),
         description="Release pipelines, blue/green, canary, rollbacks."),
    _cap("observability", DEVOPS, "Observability", languages=(),
         tags=("logging", "metrics", "tracing"), skills=("feature", "performance"),
         description="Logging, metrics, tracing, alerting."),
    # ---- SOFTWARE ENGINEERING --------------------------------------------
    _cap("architecture", ENGINEERING, "Architecture", languages=(),
         tags=("design", "patterns"), skills=("refactor", "review"),
         description="System design, layering, boundaries, dependency rules."),
    _cap("debugging", ENGINEERING, "Debugging", languages=(),
         tags=("hypotheses", "bisect"), skills=("bugfix",),
         description="Systematic debugging: hypotheses, bisection, isolates."),
    _cap("refactoring", ENGINEERING, "Refactoring", languages=(),
         tags=("behavior-preserving", "incremental"), skills=("refactor",),
         description="Safe incremental refactor under test."),
    _cap("testing", ENGINEERING, "Testing", languages=(),
         tags=("unit", "integration", "e2e"), skills=("test",),
         description="Test design: unit, integration, E2E, fixtures, mocks."),
    _cap("security", ENGINEERING, "Security", languages=(),
         tags=("owasp", "cwe"), skills=("security", "review"),
         description="OWASP/CWE: injection, auth, secrets, supply chain."),
    _cap("performance", ENGINEERING, "Performance", languages=(),
         tags=("profiling", "latency"), skills=("performance",),
         description="Profiling, benchmarks, latency/memory optimization."),
    _cap("dependency_upgrades", ENGINEERING, "Dependency Upgrades", languages=(),
         tags=("breaking-changes", "lockfiles"), skills=("dependency", "migration"),
         description="Dependency upgrades with breaking-change analysis."),
    _cap("legacy_modernization", ENGINEERING, "Legacy Modernization", languages=(),
         tags=("strangler", "porting"), skills=("migration", "refactor"),
         description="Strangler migrations, porting, dead-code removal."),
    _cap("code_review", ENGINEERING, "Code Review", languages=(),
         tags=("correctness", "regressions"), skills=("review",),
         description="Adversarial review: correctness, edge cases, risk."),
    _cap("documentation", ENGINEERING, "Documentation", languages=(),
         tags=("readme", "docs"), skills=("documentation",),
         description="Clear, accurate, example-driven documentation."),
)


class CapabilityRegistry:
    """Extensible registry of coding capabilities."""

    def __init__(self, capabilities: Optional[Tuple[Capability, ...]] = None):
        self._capabilities: List[Capability] = list(capabilities or DEFAULT_CAPABILITIES)
        self._by_key = {c.key: c for c in self._capabilities}

    # -- lookup ------------------------------------------------------------

    def register(self, capability: Capability) -> None:
        if capability.key in self._by_key:
            raise ValueError(f"capability already registered: {capability.key}")
        self._capabilities.append(capability)
        self._by_key[capability.key] = capability

    def get(self, key: str) -> Optional[Capability]:
        return self._by_key.get(key)

    def all(self) -> List[Capability]:
        return list(self._capabilities)

    def by_family(self, family: str) -> List[Capability]:
        return [c for c in self._capabilities if c.family == family]

    def families(self) -> List[str]:
        seen: List[str] = []
        for c in self._capabilities:
            if c.family not in seen:
                seen.append(c.family)
        return seen

    def by_language(self, language: str) -> List[Capability]:
        lang = language.lower()
        return [c for c in self._capabilities if lang in c.languages]

    def count(self) -> int:
        return len(self._capabilities)


def capabilities_for_language(language: str) -> List[str]:
    """Keys of capabilities that target a language."""
    return [c.key for c in DEFAULT_CAPABILITIES if language.lower() in c.languages]


def capabilities_for_repo(profile: dict) -> List[Capability]:
    """Capabilities implicated by a repo profile (languages + frameworks)."""
    selected: List[Capability] = []
    primary = (profile.get("primary_language") or "").lower()
    secondary = [l.lower() for l in profile.get("secondary_languages") or []]
    for cap in DEFAULT_CAPABILITIES:
        langs = {l.lower() for l in cap.languages}
        if primary in langs or langs & set(secondary):
            selected.append(cap)
    for fw in profile.get("frameworks") or []:
        fw_lower = fw.lower()
        for cap in DEFAULT_CAPABILITIES:
            if (
                cap.key == fw_lower
                or fw_lower in {t.lower() for t in cap.tags}
                or fw_lower == cap.label.lower()
            ):
                if cap not in selected:
                    selected.append(cap)
    return selected