# @feature: FP-0.2.二 mode_personal 清单契约 | @ci: python-coverage
"""mode_personal plugin.json 回归：触发器依赖、域事件订阅、番茄配置默认值。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MANIFEST = Path(__file__).resolve().parents[1] / "plugin.json"


def _load() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_trigger_control_dependency_and_domain_event_hook() -> None:
    d = _load()
    assert "trigger-control" in d["requires_services"]
    assert "human-interaction" not in d["requires_services"]
    assert "domain_event" in d["capabilities"]["lifecycle_hooks"]


def test_focus_duration_config_default_25() -> None:
    d = _load()
    config_files = d["config_files"]
    assert config_files[0]["id"] == "mode_personal_settings"
    field = config_files[0]["fields"][0]
    assert field["name"] == "focus_duration_minutes"
    assert field["default"] == 25
    assert field["min"] == 1
    assert field["max"] == 1440


def test_pim_payload_declares_event_and_focus_ops() -> None:
    d = _load()
    pim = next(t for t in d["capabilities"]["tools"] if t["name"] == "pim")
    desc = pim["input_schema"]["properties"]["payload"]["description"]
    for token in ("event", "preference", "accept", "cancel", "focus_start", "duration_minutes"):
        assert token in desc
