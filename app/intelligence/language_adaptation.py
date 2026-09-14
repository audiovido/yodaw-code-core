"""
Language / Framework Adaptation.

YODAW inspects repository conventions rather than imposing generic
style. Given a repo profile, produce:

- an authoritative toolchain summary (build/test/lint commands)
- language-specific guidance for the Coder Brain prompt
- framework-specific gotchas

Phase 7 of the Elite Coding Intelligence layer.
"""

from __future__ import annotations

from typing import Dict, List, Optional

LANG_ADAPTATION: Dict[str, Dict] = {
    "python": {
        "toolchain": {
            "test": ["pytest", "unittest"],
            "lint": ["ruff", "flake8", "pylint"],
            "type_check": ["mypy", "pyright"],
            "format": ["black", "ruff format"],
            "build": ["pip", "poetry", "uv", "hatch"],
        },
        "guidance": [
            "Prefer stdlib and installed deps over new packages.",
            "Respect Python version in pyproject/requires-python (avoid unreleased syntax).",
            "Use type hints where the repo uses them; match annotations.",
            "Async: do not call blocking IO inside async code paths.",
            "Never use bare except; catch explicit exceptions.",
        ],
    },
    "javascript": {
        "toolchain": {
            "test": ["jest", "mocha", "vitest"],
            "lint": ["eslint"],
            "type_check": [],
            "format": ["prettier"],
            "build": ["npm", "yarn", "pnpm"],
        },
        "guidance": [
            "Respect the repo's module system (ESM vs CJS) exactly.",
            "Prefer existing helpers/utilities before new code.",
            "Watch for Node version mismatches in engines/package.json.",
            "Async: never swallow promise rejections; propagate errors.",
        ],
    },
    "typescript": {
        "toolchain": {
            "test": ["jest", "vitest"],
            "lint": ["eslint"],
            "type_check": ["tsc"],
            "format": ["prettier"],
            "build": ["npm", "yarn", "pnpm", "tsup", "vite"],
        },
        "guidance": [
            "Keep types strictness matching tsconfig (no `any` escapes unless the file already uses them).",
            "Respect paths aliases in tsconfig.json (do not invent relative imports that break resolution).",
            "Update types alongside implementation; type test coverage matters.",
            "Prefer satisfies/const assertion idioms already used in the repo.",
        ],
    },
    "go": {
        "toolchain": {
            "test": ["go test ./..."],
            "lint": ["golangci-lint", "go vet"],
            "type_check": ["go vet"],
            "format": ["gofmt", "goimports"],
            "build": ["go build", "go mod"],
        },
        "guidance": [
            "Run gofmt/goimports conventions; never hand-align imports.",
            "Error handling: return errors, do not panic in library code.",
            "Prefer stdlib and small deps; check go.mod for existing require.",
            "Concurrency: prefer channels over shared mutable state where idiomatic.",
        ],
    },
    "rust": {
        "toolchain": {
            "test": ["cargo test"],
            "lint": ["clippy"],
            "type_check": ["cargo check"],
            "format": ["rustfmt"],
            "build": ["cargo build"],
        },
        "guidance": [
            "Respect ownership: plan borrow lifetimes before writing.",
            "Use thiserror/anyhow only if already in Cargo.toml.",
            "Handle errors with ? and explicit types; avoid unwrap in library code.",
            "Run cargo clippy; fix warnings that the repo treats as errors.",
        ],
    },
    "java": {
        "toolchain": {
            "test": ["mvn test", "gradle test"],
            "lint": ["checkstyle", "pmd"],
            "type_check": [],
            "format": [],
            "build": ["mvn", "gradle"],
        },
        "guidance": [
            "Match the build system (Maven vs Gradle) already present.",
            "Respect package structure; imports must resolve via build file.",
            "Prefer existing Spring components/DI wiring over new instantiation.",
            "Watch for Java version features vs target bytecode level.",
        ],
    },
    "kotlin": {
        "toolchain": {
            "test": ["gradle test"],
            "lint": ["ktlint"],
            "type_check": [],
            "format": ["ktlint"],
            "build": ["gradle"],
        },
        "guidance": [
            "Null-safety: use nullable types deliberately; no !! shortcuts.",
            "Coroutines: match repo scope/provider idioms.",
            "Respect existing extension functions and conventions.",
        ],
    },
    "csharp": {
        "toolchain": {
            "test": ["dotnet test"],
            "lint": ["dotnet format"],
            "type_check": [],
            "format": ["dotnet format"],
            "build": ["dotnet build"],
        },
        "guidance": [
            "Respect target framework (net8.0 etc.) and C# language version.",
            "Async: use async/await end to end; avoid .Result/.Wait() blocking.",
            "Match DI registration patterns in the repo (Program.cs / Startup).",
        ],
    },
    "php": {
        "toolchain": {
            "test": ["phpunit", "pest"],
            "lint": ["phpcs"],
            "type_check": ["phpstan", "psalm"],
            "format": ["php-cs-fixer", "pint"],
            "build": ["composer"],
        },
        "guidance": [
            "Respect composer autoload (PSR-4) namespaces.",
            "Typed properties and strict_types only if the repo uses them.",
            "Prefer existing framework conventions (Laravel/Symfony) over raw PHP.",
        ],
    },
    "ruby": {
        "toolchain": {
            "test": ["rspec", "minitest"],
            "lint": ["rubocop"],
            "type_check": [],
            "format": ["rubocop"],
            "build": ["bundle"],
        },
        "guidance": [
            "Respect Gemfile; bundle exec for commands.",
            "Prefer existing Rails conventions: migrations, models, services.",
            "Symbol vs string keys: match what the repo already uses.",
        ],
    },
    "c": {
        "toolchain": {
            "test": ["ctest", "make test"],
            "lint": [],
            "type_check": [],
            "format": ["clang-format"],
            "build": ["cmake", "make"],
        },
        "guidance": [
            "Compiler flags: respect existing -Werror/-Wall build config.",
            "Memory: prefer existing allocator abstractions; no unchecked malloc.",
            "Boundary checks on all buffer/string ops.",
        ],
    },
    "cpp": {
        "toolchain": {
            "test": ["ctest", "make test"],
            "lint": ["clang-tidy"],
            "type_check": [],
            "format": ["clang-format"],
            "build": ["cmake", "make"],
        },
        "guidance": [
            "Match the C++ standard the build uses (C++17/20).",
            "RAII over raw new/delete; unique_ptr/shared_ptr idiomatically.",
            "Respect existing namespaces and include paths.",
        ],
    },
    "swift": {
        "toolchain": {
            "test": ["swift test"],
            "lint": ["swiftlint"],
            "type_check": ["swift build"],
            "format": ["swift-format"],
            "build": ["swift build", "xcodebuild"],
        },
        "guidance": [
            "Respect Package.swift platforms and dependencies.",
            "Async/await or completion handlers: match the repo's existing pattern.",
            "Prefer value types where the repo does.",
        ],
    },
    "shell": {
        "toolchain": {
            "test": ["bash -n"],
            "lint": ["shellcheck"],
            "type_check": [],
            "format": [],
            "build": [],
        },
        "guidance": [
            "Quote all variable expansions; set -euo pipefail only if repo uses it.",
            "Portability: prefer POSIX sh unless the repo is bash-only.",
            "Never parse ls output; use globs or find -print0.",
        ],
    },
    "sql": {
        "toolchain": {
            "test": ["sqlite3 syntax check"],
            "lint": ["sqlfluff"],
            "type_check": [],
            "format": [],
            "build": [],
        },
        "guidance": [
            "Match the dialect in play (PostgreSQL/MySQL/SQLite).",
            "Idempotent migrations: IF NOT EXISTS / guards.",
            "Indexes and keys: respect existing conventions and naming.",
        ],
    },
}

FRAMEWORK_GOTCHAS: Dict[str, tuple] = {
    "react": (
        "Never mutate props or state directly; use setState idioms.",
        "Keys on list items must be stable and unique.",
        "Effects: match dependency arrays; avoid stale closures.",
    ),
    "nextjs": (
        "Client vs Server Components: 'use client' boundary matters.",
        "API routes vs route handlers: match the app's router version.",
        "SSR/ISR: avoid window/document at build time.",
    ),
    "fastapi": (
        "Use Pydantic models for request/response; never trust raw dicts.",
        "Dependency injection via Depends for shared logic.",
        "Async endpoints: avoid blocking calls in the event loop.",
    ),
    "django": (
        "Use the ORM, never raw SQL unless unavoidable and escaped.",
        "Migrations must be generated, not hand-written SQL.",
        "Never disable CSRF or auth checks to make tests pass.",
    ),
    "flask": (
        "Use SQLAlchemy/app context correctly; guard teardown.",
        "Template escaping on user input (autoescape on).",
        "Session/secret key handling: no hard-coded secrets.",
    ),
    "express": (
        "Error handling middleware must be last and call next(err).",
        "Async handlers need try/catch or wrapper; unhandled rejections crash.",
        "Validate request bodies; never trust req.body directly.",
    ),
    "spring": (
        "Controllers thin; services hold logic; repositories data access.",
        "Lazy vs eager fetch: avoid N+1; respect transaction boundaries.",
        "Config via properties, not hard-coded values.",
    ),
    "vue": (
        "Composition API: watch/computed deps explicit.",
        "Props one-way; emit events for parent updates.",
    ),
    "svelte": (
        "Store mutations trigger reactivity; avoid direct store writes outside actions.",
        "Reactive declarations: mind dependency order.",
    ),
    "angular": (
        "Change detection: immutable updates for OnPush.",
        "Async pipe for subscriptions; unsubscribe on destroy.",
    ),
}


def adaptation_for_language(language: str) -> Optional[Dict]:
    """Return the adaptation block for a language (None if unknown)."""
    return LANG_ADAPTATION.get((language or "").lower())


def gotchas_for_frameworks(frameworks: List[str]) -> List[str]:
    """Collect framework gotchas for a repo's framework list."""
    gotchas: List[str] = []
    for fw in frameworks or []:
        fw_lower = fw.lower()
        for key, items in FRAMEWORK_GOTCHAS.items():
            if fw_lower == key or fw_lower in key:
                gotchas.extend(items)
                break
    return gotchas


def format_language_guidance(profile: dict) -> str:
    """Render language/framework guidance for prompt injection."""
    lang = (profile.get("primary_language") or "").lower()
    adapt = adaptation_for_language(lang)
    parts: List[str] = []

    if adapt:
        toolchain = adapt["toolchain"]
        test_tools = " / ".join(toolchain.get("test") or ())
        lint_tools = " / ".join(toolchain.get("lint") or ())
        if test_tools:
            parts.append(f"  expected test tools: {test_tools}")
        if lint_tools:
            parts.append(f"  expected lint tools: {lint_tools}")

    gotchas = gotchas_for_frameworks(profile.get("frameworks") or [])
    for g in gotchas:
        parts.append(f"  {g}")

    if adapt:
        for g in adapt["guidance"][:4]:
            parts.append(f"  {g}")

    if not parts:
        return ""
    return "\n".join(["LANGUAGE / FRAMEWORK CONVENTIONS (detected):", *parts])