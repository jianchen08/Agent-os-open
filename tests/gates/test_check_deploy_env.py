# @feature: FP-0.2.PACK 打包/部署 CI 门 | @ci: python-test
"""check_deploy_env.py 单测（部署环境检测门的解析与收集层）。

行为契约（断输入→输出，不触真实 systemd/nginx）：
- shell 脚本收集：deploy 三目录 *.sh 平铺收集、非 .sh 不收；
- 单元解析：ExecStart 系取首个空白前 token、剥引号与 systemd 前缀修饰符
  （-/+/!/@）、相对路径不收；EnvironmentFile '-' 可缺省前缀剥除仍入桩清单；
  WorkingDirectory 入目录清单；
- nginx harness：server 片段被 events+http 包裹且 include 的是解析后的
  绝对路径（-c 只吃全量配置，片段直喂必假红）；
- 平台口径：Windows 缺 systemd/nginx = SKIP（CI ubuntu 执法），缺 bash = FAIL。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_deploy_env", SCRIPTS / "check_deploy_env.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass + future annotations 需模块在册
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class TestCollectShellScripts:
    def test_collects_three_deploy_dirs_only_sh(self, tmp_path: Path):
        (tmp_path / "deploy").mkdir()
        (tmp_path / "deploy" / "multi_device").mkdir()
        (tmp_path / "deploy" / "remote").mkdir()
        (tmp_path / "deploy" / "a.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (tmp_path / "deploy" / "b.txt").write_text("x", encoding="utf-8")
        (tmp_path / "deploy" / "multi_device" / "c.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (tmp_path / "deploy" / "remote" / "d.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        got = {p.name for p in mod.collect_shell_scripts(tmp_path)}
        assert got == {"a.sh", "c.sh", "d.sh"}

    def test_repo_deploy_dir_has_scripts(self):
        # 真仓负控制：收集面非空（部署脚本群在位）
        assert len(mod.collect_shell_scripts(REPO_ROOT)) >= 5


class TestParseUnit:
    def test_exec_extracts_first_token_and_strips_prefixes(self):
        text = "\n".join([
            "[Service]",
            "ExecStart=/opt/agentos/kernel/target/release/agentos-kernel --port 9100",
            "ExecStartPre=-/usr/bin/touch /tmp/x",
            "ExecStop=!+/usr/bin/kill $MAINPID",
            "ExecStartPost=/usr/bin/env \"A=B\" extra",
        ])
        spec = mod.parse_unit(text)
        assert spec.execs == [
            "/opt/agentos/kernel/target/release/agentos-kernel",
            "/usr/bin/touch",
            "/usr/bin/kill",
            "/usr/bin/env",
        ]

    def test_quoted_exec_and_relative_paths(self):
        spec = mod.parse_unit('ExecStart="/opt/a b/tool" run\n')
        assert spec.execs == ["/opt/a b/tool"]
        assert mod.parse_unit("ExecStart=agentos-kernel\n").execs == []

    def test_envfile_and_workdir(self):
        text = "\n".join([
            "EnvironmentFile=/etc/agentos/agentos.env",
            "EnvironmentFile=-/etc/agentos/optional.env",
            "WorkingDirectory=/opt/agentos",
        ])
        spec = mod.parse_unit(text)
        assert spec.env_files == ["/etc/agentos/agentos.env", "/etc/agentos/optional.env"]
        assert spec.work_dirs == ["/opt/agentos"]

    def test_repo_kernel_unit_parses(self):
        unit = REPO_ROOT / "deploy" / "agentos-kernel.service"
        spec = mod.parse_unit(unit.read_text(encoding="utf-8"))
        assert "/opt/agentos/kernel/target/release/agentos-kernel" in spec.execs
        assert "/etc/agentos/agentos.env" in spec.env_files
        assert "/opt/agentos" in spec.work_dirs


class TestNginxHarness:
    def test_wraps_fragment_with_events_http_and_absolute_include(self, tmp_path: Path):
        conf = tmp_path / "nginx.conf"
        conf.write_text("server { listen 80; }\n", encoding="utf-8")
        text = mod.nginx_harness_text(conf, tmp_path / "p.pid", tmp_path / "e.log")
        assert "events {" in text
        assert "http {" in text
        assert f"include {conf.resolve().as_posix()};" in text  # nginx 配置吃反斜杠路径（转义坑）
        # 片段正文留在被 include 的文件里（harness 只引用路径），断言文件未被改写
        assert conf.read_text(encoding="utf-8") == "server { listen 80; }\n"


class TestPlatformContract:
    def test_windows_missing_tools_are_skip_not_fail(self):
        r = mod.check_systemd_units([], None, REPO_ROOT)
        if sys.platform == "win32":
            assert r.status == "SKIP"
        else:
            assert r.status == "FAIL"

    def test_windows_missing_bash_is_fail(self):
        r = mod.check_shell_syntax([], None)
        assert r.status == "FAIL"


class TestSandboxStubbing:
    def test_stub_creates_execs_envfiles_workdirs_under_root(self, tmp_path: Path):
        spec = mod.UnitSpec(
            execs=["/opt/agentos/kernel/target/release/agentos-kernel"],
            env_files=["/etc/agentos/agentos.env"],
            work_dirs=["/opt/agentos"],
        )
        root = tmp_path / "sb"
        mod._stub_unit_sandbox(root, spec)
        exe = root / "opt/agentos/kernel/target/release/agentos-kernel"
        assert exe.is_file()
        assert exe.read_text(encoding="utf-8").startswith("#!")
        if sys.platform != "win32":  # Windows 无 exec 位语义（仅读写位）
            assert exe.stat().st_mode & 0o111
        assert (root / "etc/agentos/agentos.env").is_file()
        assert (root / "opt/agentos").is_dir()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
