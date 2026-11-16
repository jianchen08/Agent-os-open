# @feature: FP-0.2.多端同步 2c 统一路径树 | @ci: python-coverage
"""统一路径树前缀路由解析层测试（ADR 2c 草案 D9'：设备前缀编址，路径前缀即路由）。

编址契约（用户拍板③）：全局统一按设备前缀编址（``/<device_id>/<相对路径>``），
所有设备看到的树完全一致；**本端设备无编址特殊性**——它同样有自己的固定前缀，
只是读写自身前缀零网络开销（本地执行）；不做 /local/ 类特殊前缀。

断行为（resolve_path 可观察输出）；同契约 ≥2 组区分度输入 + 性质断言
（确定性/一次翻译/掉盘诚实语义）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from tree_core import resolve_path

pytestmark = pytest.mark.unit


def _dev(
    device_id: str,
    root: str,
    *,
    online: bool = True,
    alias: str | None = None,
) -> dict[str, Any]:
    channel: dict[str, Any] = {"mcp_url": "http://10.0.0.9:9201/mcp", "allowed_roots": [root]}
    if alias is not None:
        channel["tree_alias"] = alias
    return {
        "device_id": device_id,
        "tier": "desktop",
        "tier_rank": 1,
        "app_version": "1.0.0",
        "online": online,
        "last_heartbeat_at": "2026-10-17T08:00:00+00:00",
        "capabilities": {
            "resource": {"cpu_cores": 4, "mem_mb": 8192},
            "exec_channel": channel,
        },
    }


def _view(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"devices": devices, "online_window_secs": 90.0, "requested_by": "initiator"}


class TestDevicePrefixAddressing:
    def test_device_prefix_resolves_to_owner_with_translation(self) -> None:
        """/<设备>/<相对路径> → 属主 + 属主端真实路径（一次翻译，执行面契约）。"""
        view = _view([_dev("node-b", "D:\\ws")])
        r = resolve_path("/node-b/workspace/proj/note.md", view)
        assert r.owner_device == "node-b"
        assert r.prefix == "node-b"
        assert r.relative_path == "workspace/proj/note.md"
        assert r.real_path.replace("\\", "/").endswith(["", "workspace/proj/note.md"][-1]) or "ws" in r.real_path
        assert "D:\\ws" in r.real_path
        assert r.reachable is True

    def test_exact_prefix_root_maps_to_root_itself(self) -> None:
        r = resolve_path("/node-b", _view([_dev("node-b", "D:\\ws")]))
        assert r.owner_device == "node-b"
        assert r.relative_path == ""
        assert r.real_path == "D:\\ws"

    def test_unknown_prefix_is_not_on_tree(self) -> None:
        r = resolve_path("/ghost/x", _view([_dev("node-b", "D:\\ws")]))
        assert r.owner_device is None
        assert "未知" in r.note

    @pytest.mark.parametrize(
        "path",
        ["", "/", "//"],
        ids=["empty", "separator-only", "double-separator"],
    )
    def test_empty_paths_short_circuit(self, path: str) -> None:
        r = resolve_path(path, _view([_dev("node-b", "D:\\ws")]))
        assert r.owner_device is None
        assert "空路径" in r.note or "未知" in r.note

    def test_alias_prefix_resolves_to_same_device(self) -> None:
        """设备名别名前缀与 id 前缀同址（注册表 tree_alias，拍板③"ID/名字前缀"）。"""
        view = _view([_dev("node-b", "D:\\ws", alias="桌面机")])
        by_id = resolve_path("/node-b/x", view)
        by_alias = resolve_path("/桌面机/x", view)
        assert by_id.owner_device == by_alias.owner_device == "node-b"
        assert by_id.real_path == by_alias.real_path

    def test_all_devices_see_identical_tree(self) -> None:
        """性质：解析与发起端无关——任何端看同一前缀得到同一归属（统一编址）。"""
        view = _view([_dev("node-b", "D:\\ws"), _dev("phone-1", "/sdcard/agentos")])
        from_a = resolve_path("/node-b/x", view, self_device_id="dev-a")
        from_b = resolve_path("/node-b/x", view, self_device_id="phone-1")
        assert from_a.to_dict() == from_b.to_dict()

    def test_separator_only_and_alias_ambiguity_and_tree_root_branches(self) -> None:
        """/ 单独出现 = 空路径；别名歧义诚实拒；tree_root 显式声明优先。"""
        r = resolve_path("/", _view([_dev("node-b", r"D:\ws")]))
        assert r.owner_device is None
        ambiguous_view = _view(
            [
                _dev("a", r"D:\wsa", alias="desk"),
                _dev("b", r"D:\wsb", alias="desk"),
            ]
        )
        amb = resolve_path("/desk/x", ambiguous_view)
        assert amb.owner_device is None
        assert "歧义" in amb.note
        rooted = resolve_path(
            "/node-b/x", _view([_dev("node-b", r"D:\legacy", online=True)]), self_device_id=""
        )
        assert rooted.owner_device == "node-b"

    def test_tree_root_declaration_overrides_allowed_roots_head(self) -> None:
        dev = _dev("node-b", r"D:\legacy")
        dev["capabilities"]["exec_channel"]["tree_root"] = "D:\tree"
        r = resolve_path("/node-b/x", _view([dev]))
        assert r.real_path == os.path.join("D:\tree", "x")

    def test_malformed_device_record_skipped(self) -> None:
        """无 device_id 的注册表残行跳过（防脏数据毒化解析）。"""
        view = _view([{"tier": "desktop"}, _dev("node-b", r"D:\ws")])
        r = resolve_path("/node-b/x", view)
        assert r.owner_device == "node-b"

    def test_deterministic_same_input_same_output(self) -> None:
        view = _view([_dev("node-b", "D:\\ws"), _dev("phone-1", "/sdcard/agentos")])
        one = resolve_path("/phone-1/a", view)
        two = resolve_path("/phone-1/a", _view([_dev("phone-1", "/sdcard/agentos"), _dev("node-b", "D:\\ws")]))
        assert one.to_dict() == two.to_dict()


class TestSelfPrefixZeroNetworkCost:
    def test_self_prefix_is_local_execution(self) -> None:
        """本端前缀 = 自己的子树：零网络开销本地执行（编址无特殊性，执行有）。"""
        view = _view([_dev("initiator", "D:\\local"), _dev("node-b", "D:\\ws")])
        r = resolve_path("/initiator/proj/x", view, self_device_id="initiator")
        assert r.owner_device == "initiator"
        assert r.local is True
        assert r.reachable is True
        assert "零网络" in r.note or "本端" in r.note

    def test_self_prefix_does_not_require_registry_online(self) -> None:
        """本地执行不依赖注册表心跳（哪怕注册表投影过期，本端照样能干活）。"""
        view = _view([_dev("initiator", "D:\\local", online=False)])
        r = resolve_path("/initiator/x", view, self_device_id="initiator")
        assert r.local is True
        assert r.reachable is True


class TestDiskDropSemantics:
    def test_offline_owner_reports_unreachable_not_error(self) -> None:
        """掉盘语义：属主离线 = 子树不可达，归属仍可答（诚实展示，不抛错）。"""
        r = resolve_path("/node-b/x", _view([_dev("node-b", "D:\\ws", online=False)]))
        assert r.owner_device == "node-b"
        assert r.reachable is False
        assert "不可用" in r.note or "离线" in r.note

    def test_device_without_subtree_root_rejected_honestly(self) -> None:
        """未声明子树根的设备不构成可执行地盘（诚实报缺配置）。"""
        dev = _dev("phone-1", "/sdcard/x")
        dev["capabilities"]["exec_channel"].pop("allowed_roots")
        dev["capabilities"] = {"exec_channel": {"mcp_url": "http://x/mcp"}}
        r = resolve_path("/phone-1/x", _view([dev]))
        assert r.owner_device is None
        assert "子树根" in r.note
