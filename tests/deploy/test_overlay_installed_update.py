# @feature: FP-0.2.PACK 装机直覆盖更新（发现缓存烤制面） | @ci: python-test
"""overlay_installed_update.py 发现缓存烤制单测（2026-11-20 启动优化 A）。

覆盖：运行期/开发期写入面（.venv*（前缀匹配）/__pycache__/.audit_tmp/data/
tests）整棵子树不入摘要、真源文件照常入册、plugin_dirs 候选表与符号链接面
不受排除影响、条目级谓词的正反两组区分度输入。kernel 侧 walk_face 与
deploy/materialize_seed.py 同律（两侧同律契约）。
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__)) + "/../.."
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import overlay_installed_update as oiu  # noqa: E402


@pytest.mark.parametrize(
    ("rel", "excluded"),
    [
        # 正组：运行期/开发期写入面（装机实证形态）
        ("llm/.venv-x/bin/python.exe", True),
        ("llm/.venv/lib/site.py", True),
        ("llm/__pycache__/server.cpython-312.pyc", True),
        ("_host/.audit_tmp/audit_ledger.json", True),
        ("hindsight_memory/data/kb/chunk.bin", True),
        ("data/permission_modes.json", True),
        ("dsh_adapter/tests/test_dsh_adapter.py", True),
        ("llm/loose.pyc", True),
        # 反组：真源文件（形似但不命中——段级精确匹配非子串）
        ("llm/plugin.json", False),
        ("llm/server.py", False),
        ("llm/vendor/venv_tool.py", False),
        ("llm/database.py", False),
        ("llm/contest/helper.py", False),
        ("llm/latest_utils.py", False),
        ("llm/data_reader.py", False),
    ],
)
def test_runtime_face_artifact_predicate(rel, excluded):
    assert oiu.is_runtime_face_artifact(rel) is excluded, rel


def test_build_face_discovery_cache_excludes_runtime_trees(tmp_path):
    face = tmp_path / "plugins" / "shared"
    (face / "llm").mkdir(parents=True)
    (face / "llm" / "plugin.json").write_bytes(b'{"id": "llm_service"}')
    (face / "llm" / "server.py").write_bytes(b"print('llm')\n")
    (face / "llm_core").mkdir()
    (face / "llm_core" / "plugin.json").write_bytes(b'{"id": "pipeline_llm_core"}')
    (face / "llm" / ".venv-x" / "bin").mkdir(parents=True)
    (face / "llm" / ".venv-x" / "bin" / "python.exe").write_bytes(b"PYEXE")
    (face / "llm" / "__pycache__").mkdir()
    (face / "llm" / "__pycache__" / "server.cpython-312.pyc").write_bytes(b"\x00PYC")
    (face / "_host" / ".audit_tmp").mkdir(parents=True)
    (face / "_host" / ".audit_tmp" / "audit_ledger.json").write_bytes(b"{}")
    (face / "hindsight_memory" / "data" / "kb").mkdir(parents=True)
    (face / "hindsight_memory" / "data" / "kb" / "chunk.bin").write_bytes(b"KB")
    (face / "data").mkdir()
    (face / "data" / "permission_modes.json").write_bytes(b"{}")
    (face / "dsh_adapter" / "tests").mkdir(parents=True)
    (face / "dsh_adapter" / "tests" / "test_dsh_adapter.py").write_bytes(b"def t():\n pass\n")

    cache = oiu.build_face_discovery_cache(face)
    assert set(cache["files"]) == {
        "llm/plugin.json",
        "llm/server.py",
        "llm_core/plugin.json",
    }, "运行期/开发期写入树整棵不入摘要，真源照常"
    assert cache["plugin_dirs"] == {
        str(face): {"llm": "plugin.json", "llm_core": "plugin.json"}
    }, "候选表不受排除影响"
    assert cache["version"] == 1
    assert cache["generator"] == "overlay_installed_update.py"
    assert cache["face_root"] == str(face)


@pytest.mark.skipif(os.name == "nt", reason="Windows 无特权建符号链接（链路 Linux 车道覆盖）")
def test_build_face_discovery_cache_symlinks_outside_runtime_trees(tmp_path):
    face = tmp_path / "plugins" / "shared"
    (face / "llm").mkdir(parents=True)
    (face / "llm" / "plugin.json").write_bytes(b'{"id": "llm_service"}')
    (face / "llm" / "server.py").write_bytes(b"print('llm')\n")
    (face / "llm" / "link.py").symlink_to("server.py")
    (face / "llm" / "data").mkdir()
    (face / "llm" / "data" / "alias.py").symlink_to("server.py")

    cache = oiu.build_face_discovery_cache(face)
    assert cache["links"] == {"llm/link.py": "server.py"}, "摘要面外符号链接不入册"
    assert set(cache["files"]) == {"llm/plugin.json", "llm/server.py"}
