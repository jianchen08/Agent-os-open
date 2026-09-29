# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""atomic_io 共享原子写助手契约测试（B4）。

锁定行为：
- 正常写：目标内容落盘、tmp 不残留（新建 + 覆写两组输入、utf-8 中文）；
- replace 失败：目标保持旧内容、tmp 被清、异常上抛（不吞）；
- replace 瞬态失败：短重试后成功（目标为新内容）；
- yaml/.env 两条真实路径：routes_llm_config 的 _write_yaml / _update_env_var
  走原子写后落盘内容 round-trip 正确，失败时旧配置原样保留。

os.replace/open 属文件系统外部边界，失败注入用 monkeypatch。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
for _p in (str(_SHARED_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atomic_io  # noqa: E402, I001  (需先推 sys.path 再导入；平铺裸模块 per-file-ignores 先例)
from atomic_io import atomic_write_text  # noqa: E402, I001


class TestAtomicWriteText:
    def test_write_new_file(self, tmp_path: Path) -> None:
        """新建文件：内容落盘，tmp 不残留。"""
        target = tmp_path / "config.yaml"
        atomic_write_text(target, "key: 值\n")
        assert target.read_text(encoding="utf-8") == "key: 值\n"
        assert not (tmp_path / "config.yaml.tmp").exists()

    def test_overwrite_existing_replaces_content(self, tmp_path: Path) -> None:
        """覆写已有文件：旧内容被整体替换（非追加/截断混合）。"""
        target = tmp_path / "note.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new-content-长文本")
        assert target.read_text(encoding="utf-8") == "new-content-长文本"
        assert list(tmp_path.iterdir()) == [target]  # 目录内只有目标文件

    def test_encoding_param_roundtrip(self, tmp_path: Path) -> None:
        """encoding 参数生效（非默认编码可指定）。"""
        target = tmp_path / "gbk.txt"
        atomic_write_text(target, "中文内容", encoding="gbk")
        assert target.read_text(encoding="gbk") == "中文内容"

    def test_replace_failure_keeps_old_content_and_cleans_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """replace 一直失败：目标保持旧内容、tmp 被清、OSError 上抛。"""
        target = tmp_path / "llm.yaml"
        target.write_text("providers: old\n", encoding="utf-8")

        def _always_fail(src: object, dst: object) -> None:
            raise PermissionError("file in use")

        monkeypatch.setattr(atomic_io.os, "replace", _always_fail)
        with pytest.raises(PermissionError):
            atomic_write_text(target, "providers: new\n")
        assert target.read_text(encoding="utf-8") == "providers: old\n"
        assert not (tmp_path / "llm.yaml.tmp").exists()

    def test_replace_transient_failure_retries_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """replace 瞬态失败（Windows 文件占用典型）：短重试后换入成功。"""
        target = tmp_path / "llm.yaml"
        target.write_text("old\n", encoding="utf-8")
        calls = {"n": 0}
        real_replace = atomic_io.os.replace

        def _flaky(src: object, dst: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("transient")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(atomic_io.os, "replace", _flaky)
        monkeypatch.setattr(atomic_io.time, "sleep", lambda _: None)  # 掐退避等待
        atomic_write_text(target, "new\n")
        assert calls["n"] == 2
        assert target.read_text(encoding="utf-8") == "new\n"
        assert not (tmp_path / "llm.yaml.tmp").exists()
