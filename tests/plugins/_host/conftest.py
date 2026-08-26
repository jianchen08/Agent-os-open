"""合宿宿主（plugins/shared/_host/host.py）测试基建。

_HOST_DIR 注入 sys.path 使 ``import host`` 可用；_PLUGIN_SOURCE_DIRS 交给
tests/plugins/conftest.py 的裸名串扰治理 hook（每个测试执行前重置
sys.path 并踢掉 "plugin"/"server" 等裸名缓存——合成成员与真实插件同款
平铺导入，同样受串扰影响）。

合成成员刻意复刻真实轻插件结构：目录内 ``plugin.py``（平铺裸名）+
``server.py``（``from plugin import MEMBER_NAME`` + AgentOSPlugin 工具），
多个成员同名 plugin.py 正是合宿加载器要解决的串扰场景。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
HOST_DIR = _REPO_ROOT / "plugins" / "shared" / "_host"

# 供 tests/plugins/conftest.py pytest_runtest_setup 治理裸名串扰
_PLUGIN_SOURCE_DIRS = [str(HOST_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)

_MEMBER_SERVER_TEMPLATE = '''\
"""合成成员插件（测试用）：平铺 import plugin.py，暴露身份与生命周期记录。"""
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

from plugin import MEMBER_NAME

plugin = AgentOSPlugin("{plugin_name}")

lifecycle_seen: list[dict[str, Any]] = []


@plugin.on_load
async def _on_load(params: dict) -> None:
    lifecycle_seen.append(dict(params))


@plugin.tool(
    name="echo",
    schema={{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"]}},
    description="Echo member identity",
)
async def echo(text: str) -> dict:
    return {{"member": MEMBER_NAME, "text": text}}


@plugin.tool(
    name="last_lifecycle",
    schema={{"type": "object", "properties": {{}}}},
    description="Lifecycle payloads received so far",
)
async def last_lifecycle() -> dict:
    return {{"member": MEMBER_NAME, "events": list(lifecycle_seen)}}


if __name__ == "__main__":
    plugin.run()
'''


def write_member(
    shared_root: Path,
    group_rel: str,
    dir_name: str,
    manifest_id: str,
    *,
    member_name: str | None = None,
) -> Path:
    """在假 shared 树下写入一个合成成员目录（plugin.json + plugin.py + server.py）。"""
    member_dir = shared_root / group_rel / dir_name
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "plugin.json").write_text(
        json.dumps({"id": manifest_id, "host_type": "sidecar", "entry": "python server.py"}),
        encoding="utf-8",
    )
    name = member_name if member_name is not None else dir_name
    (member_dir / "plugin.py").write_text(f'MEMBER_NAME = "{name}"\n', encoding="utf-8")
    (member_dir / "server.py").write_text(_MEMBER_SERVER_TEMPLATE.format(plugin_name=name), encoding="utf-8")
    return member_dir


@pytest.fixture()
def shared_tree(tmp_path: Path) -> Path:
    """假 plugins/shared 树：三个分组根（含 pipeline 二级嵌套）各放一个合成成员。"""
    root = tmp_path / "shared"
    write_member(root, "system", "alpha", "alpha_id")
    write_member(root, "tools", "beta", "beta_id")
    write_member(root, "pipeline/input", "gamma", "gamma_id")
    return root


@pytest.fixture()
def make_member(shared_tree: Path) -> Callable[..., Path]:
    """向假 shared 树追加合成成员的工厂（返回成员目录）。"""

    def _make(group_rel: str, dir_name: str, manifest_id: str, *, member_name: str | None = None) -> Path:
        return write_member(shared_tree, group_rel, dir_name, manifest_id, member_name=member_name)

    return _make
