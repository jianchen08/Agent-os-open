# @feature: FP-0.2.二 角色扮演成熟化 Wave A | @vision: V1 可进化 | @ci: none-local
"""character_state 插件（角色状态账本）服务端行为测试。

覆盖（对齐 plugins/shared/system/character_state/server.py 服务契约）：
1. init：建账铺底 initial + 幂等（二次 init 不覆盖已有 values）+ 非法 schema 拒绝
2. update：生效 + history 含 from/to；未知 key / 类型不符报错且不落半套（写面原子）
3. history：最近 N 条（seq 升序尾部）+ limit 边界
4. rollback：values 恢复 + 追加 rollback 记录 + 后续 update seq 递增
5. render：模板渲染 + 未 init 返回空串 + 无 template 键跳过
6. delete：删文件 + 缺账本报错
7. 多卡隔离；card_id 路径穿越防护；_storage_dir 注入缝

存储走真实文件读写（tmp_path 经 AGENTOS_USER_CONFIG_DIR 环境变量注入，
user_space 全链路真实解析）；不 mock 文件 I/O。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 路径兜底（agentos_plugin_sdk 未 editable 安装时）
_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

_MODULE_NAME = "character_state_server_test"


def _load_server() -> Any:
    """动态加载 server.py（每次新建，隔离模块级 plugin 注册状态）。"""
    if _MODULE_NAME in sys.modules:
        del sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """user_config 指向 tmp（AGENTOS_USER_CONFIG_DIR，user_space 真实解析链）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    return _load_server()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """账本落盘目录（与 server fixture 的环境变量注入同源）。"""
    return tmp_path / "user_config" / "character_state"


_LUNA_SCHEMA: dict[str, dict[str, Any]] = {
    "affinity": {"type": "number", "initial": 0, "template": "好感度 {value}"},
    "location": {"type": "string", "initial": "", "template": "当前位置：{value}"},
}


async def _init_luna(server: Any, card_id: str = "card_luna") -> dict[str, Any]:
    return await server.state_init(card_id, json.loads(json.dumps(_LUNA_SCHEMA)))


def _read_card_file(state_dir: Path, card_id: str) -> dict[str, Any]:
    """直接读盘上的账本文件（存储形态实测，非经服务面）。"""
    path = state_dir / f"{card_id}.json"
    assert path.exists(), f"账本文件应存在: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════
# init：建账 + 幂等
# ═══════════════════════════════════════════════════════════


async def test_init_seeds_values_and_writes_file(server: Any, state_dir: Path) -> None:
    result = await _init_luna(server)
    assert result["created"] is True
    assert result["values"] == {"affinity": 0, "location": ""}
    on_disk = _read_card_file(state_dir, "card_luna")
    # 存储形态契约：顶层四键 + history 起点为空
    assert set(on_disk) == {"card_id", "schema", "values", "history"}
    assert on_disk["card_id"] == "card_luna"
    assert on_disk["values"] == {"affinity": 0, "location": ""}
    assert on_disk["history"] == []
    assert on_disk["schema"]["affinity"] == {
        "type": "number",
        "initial": 0,
        "template": "好感度 {value}",
    }


async def test_init_fills_missing_initial_and_template(server: Any, state_dir: Path) -> None:
    """条目缺 initial/template 时按类型补缺省（归一形态落盘，渲染跳过空模板）。"""
    await server.state_init("card_min", {"mood": {"type": "string"}, "awake": {"type": "bool"}})
    on_disk = _read_card_file(state_dir, "card_min")
    assert on_disk["values"] == {"mood": "", "awake": False}
    assert on_disk["schema"]["mood"] == {"type": "string", "initial": "", "template": ""}


async def test_init_idempotent_does_not_overwrite(server: Any) -> None:
    await _init_luna(server)
    await server.state_update("card_luna", {"affinity": 5})
    # 二次 init 携不同 schema/initial：不覆盖既有账本（状态真值不被重置）
    again = await server.state_init(
        "card_luna",
        {"affinity": {"type": "number", "initial": 999, "template": "x {value}"}},
    )
    assert again["created"] is False
    assert again["values"] == {"affinity": 5, "location": ""}  # 已有 values 原样
    got = await server.state_get("card_luna")
    assert got["schema"]["affinity"]["initial"] == 0  # 原 schema 未被换掉
    assert got["values"]["affinity"] == 5


@pytest.mark.parametrize(
    "bad_schema",
    [
        {},  # 空表
        {"affinity": {"type": "float", "initial": 0}},  # 未登记类型
        {"affinity": "number"},  # 条目非 dict
    ],
    ids=["empty", "unknown_type", "decl_not_dict"],
)
async def test_init_rejects_invalid_schema(server: Any, bad_schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="schema"):
        await server.state_init("card_bad", bad_schema)


# ═══════════════════════════════════════════════════════════
# update：生效 + history from/to + 校验拒绝
# ═══════════════════════════════════════════════════════════


async def test_update_applies_and_records_from_to(server: Any, state_dir: Path) -> None:
    await _init_luna(server)
    result = await server.state_update("card_luna", {"affinity": 5, "location": "月光神殿"})
    assert result["values"] == {"affinity": 5, "location": "月光神殿"}
    got = await server.state_get("card_luna")
    assert got["values"] == {"affinity": 5, "location": "月光神殿"}
    on_disk = _read_card_file(state_dir, "card_luna")
    (entry,) = on_disk["history"]
    assert entry["seq"] == 1
    assert entry["ts"]  # ISO8601 时间戳在位
    assert entry["changes"] == {
        "affinity": {"from": 0, "to": 5},
        "location": {"from": "", "to": "月光神殿"},
    }


async def test_update_accepts_int_and_float_for_number(server: Any) -> None:
    """number 键收 int/float 皆可（同契约两组区分度输入）。"""
    await _init_luna(server)
    await server.state_update("card_luna", {"affinity": 3})
    await server.state_update("card_luna", {"affinity": 2.5})
    got = await server.state_get("card_luna")
    assert got["values"]["affinity"] == 2.5
    assert len(await server.state_history("card_luna")) == 2


async def test_update_rejects_unknown_key_without_partial_write(server: Any) -> None:
    await _init_luna(server)
    with pytest.raises(ValueError, match="未知状态键"):
        await server.state_update("card_luna", {"affinity": 5, "mood": "happy"})
    # 全量校验先行：非法批次整体拒绝，values/history 不落半套
    got = await server.state_get("card_luna")
    assert got["values"] == {"affinity": 0, "location": ""}
    assert await server.state_history("card_luna") == []


@pytest.mark.parametrize(
    ("key", "value", "declared"),
    [
        ("affinity", "high", "number"),  # number ← str
        ("affinity", True, "number"),  # number ← bool（bool 是 int 子类，须排除）
        ("location", 42, "string"),  # string ← int
        ("location", None, "string"),  # string ← null
    ],
    ids=["num_lt_str", "num_lt_bool", "str_lt_int", "str_lt_null"],
)
async def test_update_rejects_type_mismatch(server: Any, key: str, value: Any, declared: str) -> None:
    await _init_luna(server)
    with pytest.raises(ValueError, match=f"状态键 '{key}' 类型不符"):
        await server.state_update("card_luna", {key: value})
    got = await server.state_get("card_luna")
    assert isinstance(got, dict)
    assert got["values"][key] == _LUNA_SCHEMA[key]["initial"]


async def test_update_accepts_bool_and_rejects_int_for_bool_key(server: Any) -> None:
    """bool 键：True/False 收，1/0 拒（类型词表第三支路的两侧区分度）。"""
    await server.state_init("card_flag", {"awake": {"type": "bool", "initial": False}})
    await server.state_update("card_flag", {"awake": True})
    got = await server.state_get("card_flag")
    assert got["values"]["awake"] is True
    with pytest.raises(ValueError, match="类型不符"):
        await server.state_update("card_flag", {"awake": 1})


async def test_update_requires_existing_card_and_nonempty_changes(server: Any) -> None:
    with pytest.raises(ValueError, match="未初始化"):
        await server.state_update("card_ghost", {"affinity": 1})
    await _init_luna(server)
    with pytest.raises(ValueError, match="changes 不能为空"):
        await server.state_update("card_luna", {})


# ═══════════════════════════════════════════════════════════
# history：最近 N 条
# ═══════════════════════════════════════════════════════════


async def test_history_returns_recent_n_in_seq_order(server: Any) -> None:
    await _init_luna(server)
    for value in (1, 2, 3, 4):
        await server.state_update("card_luna", {"affinity": value})
    tail = await server.state_history("card_luna", 2)
    assert [entry["seq"] for entry in tail] == [3, 4]  # 最近 N 条，seq 升序尾部
    assert tail[0]["changes"]["affinity"] == {"from": 2, "to": 3}
    full = await server.state_history("card_luna")  # 缺省 limit=20 全量
    assert [entry["seq"] for entry in full] == [1, 2, 3, 4]
    short = await server.state_history("card_luna", 100)  # limit 超长不补空
    assert len(short) == 4


async def test_history_limit_must_be_positive(server: Any) -> None:
    await _init_luna(server)
    for bad_limit in (0, -1):
        with pytest.raises(ValueError, match="limit"):
            await server.state_history("card_luna", bad_limit)


# ═══════════════════════════════════════════════════════════
# rollback：恢复 + 追加记录 + seq 递增
# ═══════════════════════════════════════════════════════════


async def test_rollback_restores_values_and_appends_record(server: Any) -> None:
    await _init_luna(server)
    await server.state_update("card_luna", {"affinity": 5})  # seq 1
    await server.state_update("card_luna", {"location": "月光神殿"})  # seq 2
    await server.state_update("card_luna", {"affinity": 10, "location": "赤色荒原"})  # seq 3
    result = await server.state_rollback("card_luna", 1)
    # 恢复到 seq 1 后快照：seq 1 的 to 保留，其后变更回退到 initial
    assert result["values"] == {"affinity": 5, "location": ""}
    history = await server.state_history("card_luna")
    assert [entry["seq"] for entry in history] == [1, 2, 3, 4]  # 原史保留 + 追加一条
    rollback_entry = history[-1]
    assert rollback_entry["action"] == "rollback"
    assert rollback_entry["target_seq"] == 1
    assert rollback_entry["changes"] == {
        "affinity": {"from": 10, "to": 5},
        "location": {"from": "赤色荒原", "to": ""},
    }


async def test_rollback_then_update_seq_keeps_increasing(server: Any) -> None:
    """回滚不改写序号轴：其后 update 接最大 seq 递增（版本账单调性）。"""
    await _init_luna(server)
    await server.state_update("card_luna", {"affinity": 5})  # seq 1
    await server.state_update("card_luna", {"affinity": 10})  # seq 2
    await server.state_rollback("card_luna", 1)  # seq 3（rollback 记录）
    result = await server.state_update("card_luna", {"affinity": 8})  # seq 4
    assert result["values"]["affinity"] == 8
    history = await server.state_history("card_luna")
    seqs = [entry["seq"] for entry in history]
    assert seqs == sorted(seqs)  # 单调
    assert len(set(seqs)) == len(seqs)  # 唯一
    assert seqs == [1, 2, 3, 4]
    assert history[-1]["changes"]["affinity"] == {"from": 5, "to": 8}  # 从回滚后状态起算


async def test_rollback_rejects_unknown_seq(server: Any) -> None:
    await _init_luna(server)
    await server.state_update("card_luna", {"affinity": 5})
    for bad_seq in (99, 0):
        with pytest.raises(ValueError, match="seq 不存在"):
            await server.state_rollback("card_luna", bad_seq)
    history = await server.state_history("card_luna")
    assert len(history) == 1  # 拒绝路径不追加记录


# ═══════════════════════════════════════════════════════════
# render：表现层投影
# ═══════════════════════════════════════════════════════════


async def test_render_templates_with_current_values(server: Any) -> None:
    assert await server.state_render("card_luna") == ""  # 未 init 返回空串
    await _init_luna(server)
    assert await server.state_render("card_luna") == "好感度 0\n当前位置："
    await server.state_update("card_luna", {"affinity": 5, "location": "月光神殿"})
    assert await server.state_render("card_luna") == "好感度 5\n当前位置：月光神殿"


async def test_render_skips_keys_without_template(server: Any) -> None:
    await server.state_init(
        "card_mixed",
        {
            "affinity": {"type": "number", "initial": 3, "template": "好感度 {value}"},
            "mood": {"type": "string", "initial": "平静", "template": ""},  # 无模板不展示
        },
    )
    assert await server.state_render("card_mixed") == "好感度 3"


# ═══════════════════════════════════════════════════════════
# delete / get 前提 / card_id 防线
# ═══════════════════════════════════════════════════════════


async def test_delete_removes_file_then_reports_missing(server: Any, state_dir: Path) -> None:
    await _init_luna(server)
    assert (state_dir / "card_luna.json").exists()
    result = await server.state_delete("card_luna")
    assert result == {"card_id": "card_luna", "deleted": True}
    assert not (state_dir / "card_luna.json").exists()
    with pytest.raises(ValueError, match="未初始化"):
        await server.state_get("card_luna")
    with pytest.raises(ValueError, match="不存在"):
        await server.state_delete("card_luna")  # 二次删除如实报错


async def test_get_uninitialized_raises(server: Any) -> None:
    with pytest.raises(ValueError, match="未初始化"):
        await server.state_get("card_ghost")


@pytest.mark.parametrize(
    "bad_id",
    ["../evil", "a/b", "a\\b", "", ".hidden"],
    ids=["dotdot", "slash", "backslash", "empty", "leading_dot"],
)
async def test_card_id_path_traversal_guard(server: Any, bad_id: str) -> None:
    with pytest.raises(ValueError, match="非法 card_id"):
        await server.state_get(bad_id)


# ═══════════════════════════════════════════════════════════
# 多卡隔离
# ═══════════════════════════════════════════════════════════


async def test_cards_are_isolated_by_namespace(server: Any, state_dir: Path) -> None:
    await _init_luna(server)
    await server.state_init("card_iris", {"affinity": {"type": "number", "initial": 50}})
    await server.state_update("card_luna", {"affinity": 5})
    luna = await server.state_get("card_luna")
    iris = await server.state_get("card_iris")
    assert luna["values"]["affinity"] == 5
    assert iris["values"]["affinity"] == 50  # 他卡初始值不受扰
    assert iris["schema"].keys() == {"affinity"}  # 他卡 schema 独立
    assert (state_dir / "card_luna.json").exists()
    assert (state_dir / "card_iris.json").exists()
    # 回滚也只作用于本卡
    await server.state_rollback("card_luna", 1)
    assert (await server.state_get("card_iris"))["values"]["affinity"] == 50
    assert len(await server.state_history("card_iris")) == 0


# ═══════════════════════════════════════════════════════════
# _storage_dir 注入缝与落盘失败面
# ═══════════════════════════════════════════════════════════


def test_storage_dir_explicit_base_dir_wins(server: Any, tmp_path: Path) -> None:
    injected = tmp_path / "anywhere"
    assert server._storage_dir(injected) == injected
    assert server._storage_dir(str(injected)) == injected


def test_storage_dir_derives_from_user_config_env(server: Any, tmp_path: Path) -> None:
    """无显式 base_dir 时 = <user_config_dir>/character_state（fixture 环境变量链）。"""
    assert server._storage_dir() == tmp_path / "user_config" / "character_state"


def test_storage_dir_raises_when_user_config_unavailable(server: Any, monkeypatch: Any) -> None:
    # user_config_dir 不可得属外部环境边界（OS 目录解析失败），stub 该边界断言 fail-closed
    monkeypatch.setattr(server, "user_config_dir", lambda: None)
    with pytest.raises(RuntimeError, match="用户配置目录不可得"):
        server._storage_dir()
