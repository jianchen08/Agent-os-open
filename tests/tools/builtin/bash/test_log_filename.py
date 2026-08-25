"""日志文件名 pid 派生测试。

文件名固定为 bash_<pid>.log：进程结束后 read_log 凭 pid 即可定位日志。
"""

from __future__ import annotations

import pytest
from process_manager import ProcessManager

pytestmark = pytest.mark.unit


def test_filename_uses_pid_not_hash(tmp_path):
    """_generate_log_filename 应返回 bash_<pid>.log，不含 timestamp/hash。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    name = pm._generate_log_filename("cargo build", pid=12345)
    assert name == "bash_12345.log"


def test_filename_different_pids_different_files(tmp_path):
    """不同 pid 应得到不同文件名。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    name1 = pm._generate_log_filename("ls", pid=100)
    name2 = pm._generate_log_filename("ls", pid=200)
    assert name1 == "bash_100.log"
    assert name2 == "bash_200.log"
    assert name1 != name2


def test_filename_independent_of_command(tmp_path):
    """文件名只由 pid 决定，与命令无关（pid 唯一即可）。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    name_a = pm._generate_log_filename("echo a", pid=999)
    name_b = pm._generate_log_filename("cargo build --release", pid=999)
    assert name_a == name_b == "bash_999.log"
