# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""uploads_path 单元测试——上传目录三方对齐解析（ADR 2026-08-21）。

单一解析点：channel_api 上传落盘、内核 /uploads 静态服务、
preprocessor/llm_core 引用解析共用同一目录语义：
``UPLOADS_DIR`` 环境变量 > ``tenant_data_root(tenant, "uploads")``
（默认租户 = ``data/default/uploads``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from uploads_path import resolve_uploads_dir, resolve_uploads_url  # noqa: E402


def test_default_dir_is_tenant_data_root():
    # 无 env 覆盖：落 data/default/uploads（与内核静态服务硬编码一致）
    d = resolve_uploads_dir()
    assert d == Path("data/default/uploads") or d.is_absolute()
    assert d.parts[-3:] == ("data", "default", "uploads") or d.parts[-2:] == ("default", "uploads")


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    assert resolve_uploads_dir() == tmp_path


def test_explicit_tenant_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("UPLOADS_DIR", raising=False)
    d = resolve_uploads_dir("tenant_x")
    assert "tenant_x" in d.parts


def test_resolve_uploads_url_joins_env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    p = resolve_uploads_url("/uploads/cat.png")
    assert p is not None
    assert p == tmp_path / "cat.png"


def test_resolve_uploads_url_traversal_stays_inside_dir(tmp_path, monkeypatch):
    # basename 剥目录段：穿越形态仍落上传目录内（天然拒绝逃逸）
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    p = resolve_uploads_url("/uploads/../../etc/passwd.png")
    assert p is not None
    assert p.parent == tmp_path


def test_resolve_uploads_url_non_uploads_shape_returns_none():
    assert resolve_uploads_url("https://a.com/x.png") is None
    assert resolve_uploads_url("data:image/png;base64,xxx") is None
    assert resolve_uploads_url("/api/v1/other") is None
    assert resolve_uploads_url("/uploads/") is None  # 空 basename
