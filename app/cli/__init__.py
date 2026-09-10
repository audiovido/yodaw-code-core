"""Native interactive YODAW agent shell.

Presentation/orchestration surface only. Task planning, repo
intelligence, execution, and learning live in the existing runtime
(app.planning, app.repo_intelligence, app.workers, app.core) and are
reused through app.cli.pipeline, never reimplemented here.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
