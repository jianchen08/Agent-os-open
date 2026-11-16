# @feature: FP-0.2.G6退役闸 | @ci: python-coverage
"""退役清单字段静态闸：plugins/shared/*/plugin.json 禁再声明 granted_capabilities。

行为契约（断输入→输出）：
- 内核 PluginManifest deny_unknown_fields（ADR 2026-10-18-retire-g6-grants-
  service-plane-user-tenant-authz），清单残留退役键 = 整份 manifest 拒载；
- 闸扫全部共享清单，残留即失败并列出文件（fail-closed，与内核拒载同向）。

背景：debcde475 拆除刀清了 33 份清单但漏 6 份（db_admin/metrics_admin/
sync_agent/sync_relay/trace_ingestion/user_admin），pack19 装机实证六件全部
未注册（eager 的 sync_relay 路由 404）。本闸防同类漏扫再入。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parents[1] / "plugins" / "shared"

pytestmark = pytest.mark.unit


def _manifest_paths():
    return sorted(SHARED.glob("*/plugin.json"))


def test_retired_granted_capabilities_absent_from_all_manifests():
    """退役键 granted_capabilities 在全部共享清单中零残留。"""
    residue = []
    for path in _manifest_paths():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if "granted_capabilities" in manifest:
            residue.append(path.relative_to(SHARED).as_posix())
    assert residue == [], f"退役键残留（内核 deny_unknown_fields 将拒载）: {residue}"


def test_manifest_scan_covers_plugin_set():
    """扫描面非空且为插件清单形态（防 glob 路径漂移致闸空转）。"""
    paths = _manifest_paths()
    assert len(paths) >= 100
    sample = json.loads(paths[0].read_text(encoding="utf-8"))
    assert "id" in sample
