# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""monitoring/server 补测：payload_diag 快照文件族 + query/header 解析纯函数。

按路径直接加载 server.py 为独立模块名——裸名 ``import server`` 会被先收集的
其他插件目录（bash/db_admin 等均有 server.py）的 sys.modules 缓存抢先解析。
conftest 仍负责把 monitoring 目录加进 sys.path（server.py 内部的平铺导入需要）。
AGENTOS_LOG_DIR 指向 tmp_path 隔离落盘。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring" / "server.py"
)
assert _SERVER_PY.is_file()
_spec = importlib.util.spec_from_file_location("monitoring_server_diag_under_test", _SERVER_PY)
assert _spec is not None and _spec.loader is not None
monitoring_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_server)

pytestmark = pytest.mark.unit


@pytest.fixture
def diag_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "logs" / "payload_diag"
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    return d


# ─────────────────────────── 文件名解析 ───────────────────────────


def test_parse_payload_diag_filename_accepts_canonical_form() -> None:
    meta = monitoring_server._parse_payload_diag_filename(
        "1723380000000__deepseek-v4-flash__a1b2c3d4__12msg.json"
    )
    assert meta == {
        "ts": 1723380000000,
        "model": "deepseek-v4-flash",
        "msgs_hash": "a1b2c3d4",
        "msg_count": 12,
    }


@pytest.mark.parametrize(
    "fname",
    [
        "not_json.txt",               # 非 .json
        "1723380000000__m__hash.json",  # 去掉 .json 后不以 msg 结尾
        "hash__msg.json",             # 段数不足 4
        "abc__m__hash__5msg.json",    # ts 非数字
        "123__m__hash__xmsg.json",    # msg_count 非数字
    ],
)
def test_parse_payload_diag_filename_rejects_malformed(fname: str) -> None:
    assert monitoring_server._parse_payload_diag_filename(fname) is None


def test_parse_payload_diag_filename_model_with_double_underscore() -> None:
    """model 被 sanitize 出现 __ 时回拼（防御性 join 分支）。"""
    meta = monitoring_server._parse_payload_diag_filename(
        "123__my__model__hash__7msg.json"
    )
    assert meta is not None
    assert meta["model"] == "my__model"
    assert meta["msg_count"] == 7


# ─────────────────────────── 读取 / 列出 / 清空 ───────────────────────────


def test_read_payload_diag_rejects_traversal_and_non_json(diag_dir: Path) -> None:
    assert monitoring_server._read_payload_diag("../secret.json") == {
        "error": "invalid filename"
    }
    assert monitoring_server._read_payload_diag("a\\b.json") == {"error": "invalid filename"}
    assert monitoring_server._read_payload_diag("notes.txt") == {"error": "not a json file"}
    assert monitoring_server._read_payload_diag("missing.json") == {
        "error": "file not found",
        "name": "missing.json",
    }


def test_read_payload_diag_returns_content(diag_dir: Path) -> None:
    diag_dir.mkdir(parents=True)
    f = diag_dir / "123__m__hash__2msg.json"
    f.write_text('{"model":"m"}', encoding="utf-8")

    out = monitoring_server._read_payload_diag("123__m__hash__2msg.json")

    assert out == {"name": "123__m__hash__2msg.json", "content": '{"model":"m"}'}


def test_list_payload_diag_sorted_desc_and_skips_malformed(diag_dir: Path) -> None:
    diag_dir.mkdir(parents=True)
    (diag_dir / "100__m__h1__1msg.json").write_text("{}", encoding="utf-8")
    (diag_dir / "200__m__h2__2msg.json").write_text("{}", encoding="utf-8")
    (diag_dir / "garbage.json").write_text("{}", encoding="utf-8")  # 无法解析 → 跳过

    items = monitoring_server._list_payload_diag()

    assert [i["ts"] for i in items] == [200, 100]
    assert all("size" in i and "name" in i for i in items)


def test_list_payload_diag_missing_dir_returns_empty(diag_dir: Path) -> None:
    assert monitoring_server._list_payload_diag() == []


def test_clear_payload_diag_files_counts_and_tolerates_missing(diag_dir: Path) -> None:
    # 目录不存在 → 0
    assert monitoring_server._clear_payload_diag_files() == 0

    diag_dir.mkdir(parents=True)
    (diag_dir / "1__m__h__1msg.json").write_text("{}", encoding="utf-8")
    (diag_dir / "2__m__h__2msg.json").write_text("{}", encoding="utf-8")
    (diag_dir / "keep.txt").write_text("x", encoding="utf-8")  # 非 json 不在范围

    assert monitoring_server._clear_payload_diag_files() == 2
    assert (diag_dir / "keep.txt").exists()
    assert monitoring_server._clear_payload_diag_files() == 0  # 已空


# ─────────────────────────── query / header 纯函数 ───────────────────────────


def test_qint_falls_back_on_missing_or_invalid() -> None:
    assert monitoring_server._qint({"limit": "7"}, "limit", 50) == 7
    assert monitoring_server._qint({}, "limit", 50) == 50
    assert monitoring_server._qint({"limit": "abc"}, "limit", 50) == 50
    assert monitoring_server._qint({"limit": None}, "limit", 50) == 50  # type: ignore[dict-item]


def test_authorization_is_case_insensitive_and_requires_value() -> None:
    assert monitoring_server._authorization({"authorization": "Bearer abc"}) == "Bearer abc"
    assert monitoring_server._authorization({"Authorization": "Bearer abc"}) == "Bearer abc"
    assert monitoring_server._authorization({"authorization": ""}) == ""
    assert monitoring_server._authorization({}) == ""
    assert monitoring_server._authorization(None) == ""


def test_header_value_matches_lowercase_name() -> None:
    headers = {"X-Agentos-Tenant": "t1"}
    assert monitoring_server._header_value(headers, "x-agentos-tenant") == "t1"
    assert monitoring_server._header_value(headers, "x-other") == ""


def test_payload_diag_dir_env_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    assert monitoring_server._payload_diag_dir() == str(tmp_path / "logs" / "payload_diag")


def test_tenant_conflict_short_circuits_on_empty_args() -> None:
    assert monitoring_server._pipeline_tenant_conflict("", "t1") is False
    assert monitoring_server._pipeline_tenant_conflict("p1", "") is False
