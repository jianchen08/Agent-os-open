# @feature: FP-0.2.通知音效 | @ci: python-coverage
"""notification_sounds 插件测试：目录存储单元 + manifest↔路由核契约 +
出厂预设面契约（sounds/*.json 全部可渲染、唯一默认项）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]


def _load(name: str, mod_name: str) -> Any:
    if str(_PKG) not in sys.path:
        sys.path.insert(0, str(_PKG))
    path = _PKG / name if name.endswith(".py") else _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sound_store = _load("sound_store", "notification_sounds_test_sound_store")


def _write_preset(directory: Path, name: str, doc: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _valid_doc(sid: str, **overrides: Any) -> dict:
    doc: dict = {
        "id": sid,
        "label": f"音效 {sid}",
        "params": {
            "partials": [{"ratio": 1.0, "gain": 1.0, "decayRatio": 1.0}],
            "notes": [{"freq": 523.25, "start": 0.0, "peak": 0.3, "decay": 0.4}],
        },
    }
    doc.update(overrides)
    return doc


class TestLoadSounds:
    def test_sorted_by_id_and_explicit_default_wins(self, tmp_path: Path) -> None:
        _write_preset(tmp_path, "b.json", _valid_doc("b"))
        _write_preset(tmp_path, "a.json", _valid_doc("a", default=True))
        _write_preset(tmp_path, "c.json", _valid_doc("c"))

        presets, default_id = sound_store.load_sounds(tmp_path)

        assert [p["id"] for p in presets] == ["a", "b", "c"]
        assert default_id == "a"

    def test_no_default_flag_falls_back_to_first_id(self, tmp_path: Path) -> None:
        _write_preset(tmp_path, "b.json", _valid_doc("b"))
        _write_preset(tmp_path, "a.json", _valid_doc("a"))

        presets, default_id = sound_store.load_sounds(tmp_path)

        assert [p["id"] for p in presets] == ["a", "b"]
        assert default_id == "a"

    def test_broken_json_skipped_valid_survive(self, tmp_path: Path) -> None:
        _write_preset(tmp_path, "good.json", _valid_doc("good"))
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        presets, default_id = sound_store.load_sounds(tmp_path)

        assert [p["id"] for p in presets] == ["good"]
        assert default_id == "good"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: {**d, "id": ""},  # 空 id
            lambda d: {k: v for k, v in d.items() if k != "label"},  # 缺 label
            lambda d: {**d, "params": {**d["params"], "partials": []}},  # 空泛音簇
            lambda d: {  # notes 缺 decay
                **d,
                "params": {
                    **d["params"],
                    "notes": [{"freq": 1.0, "start": 0.0, "peak": 0.3}],
                },
            },
            lambda d: {  # note 数值字段为字符串
                **d,
                "params": {
                    **d["params"],
                    "notes": [{"freq": "523", "start": 0.0, "peak": 0.3, "decay": 0.4}],
                },
            },
            lambda d: {**d, "params": {**d["params"], "wet": "loud"}},  # wet 非数字
            lambda d: {**d, "params": {**d["params"], "taps": [{"delay": 0.1}]}},  # taps 缺字段
            lambda d: "not_a_dict",  # 顶层非对象
        ],
    )
    def test_invalid_presets_skipped(self, tmp_path: Path, mutate: Any) -> None:
        _write_preset(tmp_path, "bad.json", mutate(_valid_doc("bad")))
        _write_preset(tmp_path, "good.json", _valid_doc("good"))

        presets, default_id = sound_store.load_sounds(tmp_path)

        assert [p["id"] for p in presets] == ["good"]
        assert default_id == "good"

    def test_optional_note_fields_accepted_when_numeric(self, tmp_path: Path) -> None:
        doc = _valid_doc("glide")
        doc["params"]["notes"][0].update(
            {"glideTo": 1200.0, "glideTime": 0.09, "vibratoHz": 38.0, "vibratoDepth": 0.03}
        )
        doc["params"]["taps"] = [{"delay": 0.083, "feedback": 0.42, "lowpass": 5200.0}]
        doc["params"]["attack"] = 0.006
        doc["params"]["wet"] = 0.55
        _write_preset(tmp_path, "glide.json", doc)

        presets, _ = sound_store.load_sounds(tmp_path)

        assert [p["id"] for p in presets] == ["glide"]

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert sound_store.load_sounds(tmp_path) == ([], "")

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert sound_store.load_sounds(tmp_path / "nope") == ([], "")


class TestShippedContracts:
    """出厂面契约：manifest 声明、路由核漂移、预设文件可渲染。"""

    def test_preset_files_all_valid_with_unique_default(self) -> None:
        presets, default_id = sound_store.load_sounds(_PKG / "sounds")

        assert len(presets) >= 2, "出厂至少内置两个音效"
        assert len({p["id"] for p in presets}) == len(presets), "预设 id 不得重复"
        assert default_id == "arpeggio", "出厂默认音效 = 大调琶音（用户拍板）"

    def test_manifest_routes_have_matching_route_core(self) -> None:
        manifest = json.loads((_PKG / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["id"] == "notification_sounds"
        server_source = (_PKG / "server.py").read_text(encoding="utf-8")

        endpoints = manifest["http_endpoints"]
        assert endpoints, "至少声明一个 HTTP 端点"
        for ep in endpoints:
            assert ep["handler_capability"] == "http.handle"
            suffix = "/" + ep["path"].rstrip("/").split("/")[-1]
            assert (
                f'path.endswith("{suffix}")' in server_source
            ), f"路由核缺少端点 {ep['path']} 的分发分支"

    def test_route_core_rejects_unknown_route(self) -> None:
        """路由核契约之外的路由必须 404（fail-closed 面收缩）。"""
        import asyncio

        server = _load("server", "notification_sounds_test_server")

        result = asyncio.run(server.http_handle(path="/ext/notification_sounds/other", method="GET"))
        assert result["data"]["status"] == 404
