"""Legacy 0.1 compatibility shim package.

Provides the minimal subset of the 0.1 ``core``/``tools``/``utils``/``tasks``/
``triggers``/``agents`` modules that the four MCP sidecar tools
(``trigger_setup``, ``resource_search``, ``task_manage``, ``task_submit``)
actually import. This package is intended to be placed on ``sys.path`` so the
``from core.X`` / ``from tools.X`` / ... imports resolve to *these* trimmed
copies rather than the (no-longer-present) 0.1 source tree.

Scope is deliberately narrow: only the modules/classes referenced by the four
sidecar ``tool.py`` files are present. Deep subsystems (pipeline, db,
infrastructure, channels, isolation, evaluation) are NOT mirrored here; tools
that need them must lazy-import and tolerate their absence.
"""
