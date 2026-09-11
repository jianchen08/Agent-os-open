# @feature: FP-0.2.一 插件协议（H2 契约基建） | @ci: python-coverage
"""plugin-manifest.schema.json 漂移闸（D13/H2）。

双向钉住：
1. 全量真实 manifest 必须通过 schema——schema 比现实严（误伤存量）即红；
2. 故意违反 G2 必填契约的 manifest 必须被 schema 拒绝——schema 比现实松（丢契约）即红。

schema 不可表达的过程式规则（steps 名唯一、native 产物存在等）归 G2
（kernel/crates/plugin-loader/src/loader.rs validate_manifest_internal），见 schema description。
"""

import json
from pathlib import Path

import jsonschema
import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _REPO / "docs" / "schemas" / "plugin-manifest.schema.json"
_PLUGINS_DIR = _REPO / "plugins" / "shared"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _iter_manifests():
    """定点扫描装载面 manifest，不递归——plugins/shared 下存在循环 junction
    （dsh_adapter runtime 的 cordis node_modules 自嵌套），无界 rglob 会挂死。
    布局：system/tools 一层、pipeline 按 组/插件 两级（组 = pipeline_role）、
    shared 顶层为内核配套系统件（db_admin/user_admin 等）。"""
    for category in ("system", "tools"):
        yield from (_PLUGINS_DIR / category).glob("*/plugin.json")
    for group in ("core", "input", "output"):
        yield from (_PLUGINS_DIR / "pipeline" / group).glob("*/plugin.json")
    yield from _PLUGINS_DIR.glob("*/plugin.json")


def test_schema_file_exists_and_is_draft07(schema):
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert schema["type"] == "object"


def test_all_real_manifests_validate_against_schema(schema):
    """全量真实 manifest 漂移闸：schema 严于现实即红（防误伤存量）。"""
    manifests = list(_iter_manifests())
    assert len(manifests) >= 50, f"manifest 清单异常（{len(manifests)} 个），扫描面可能被移动"
    broken = []
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(manifest, schema)
        except jsonschema.ValidationError as exc:
            broken.append(f"{path.relative_to(_REPO)}: {exc.message}")
    assert not broken, "以下 manifest 未通过结构契约 schema:\n" + "\n".join(broken)


def test_schema_rejects_manifest_missing_g2_required_fields(schema):
    """丢 G2 必填字段（id/name/version/language/host_type/entry/capabilities）必被拒。"""
    for missing in ["id", "name", "version", "language", "host_type", "entry", "capabilities"]:
        manifest = {
            "id": "x",
            "name": "x",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "server.py",
            "capabilities": {},
        }
        manifest.pop(missing)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(manifest, schema)


def test_schema_allows_empty_entry_only_for_composite_note(schema):
    """entry 空串在 schema 层放行（组合插件 ADR ⑥），非组合判定归 G2 过程式执法。"""
    manifest = {
        "id": "composite-demo",
        "name": "composite-demo",
        "version": "1.0.0",
        "plugin_type": "composite",
        "language": "yaml",
        "host_type": "sidecar",
        "entry": "",
        "capabilities": {},
    }
    jsonschema.validate(manifest, schema)  # 不抛即过
