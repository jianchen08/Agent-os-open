"""soak 脚本的时序不变量门禁外壳。

tests/soak/ 下三个脚本是独立可执行程序（带 if __name__ == "__main__"），
无法直接挂 @pytest.mark.timing。本文件用 subprocess 调起它们，
按退出码判定是否回归，从而纳入 CI timing 门禁 stage（§9.4）。

断言性质（§9.5 双重性质）：
  - 脚本内部断言的是可观察行为（超时判定、资源回收、RSS 斜率、状态转换），
    非实现细节（详见各脚本的场景断言）。
  - 退出码非 0 = 不变量被破坏 → 测试红，拦截合并。
  - 内部重构不改变行为 → 退出码 0 → 测试绿，不会误报。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.timing

SOAK_DIR = Path(__file__).parent
# smoke profile：soak_runner 50 轮迭代，<1 分钟，适合 PR 阻塞门禁。
SOAK_SCRIPTS = [
    "timeout_sim.py",
    "soak_runner.py",
    "stuck_recovery_sim.py",
]


@pytest.mark.parametrize("script", SOAK_SCRIPTS)
def test_soak_script_no_regression(script: str, tmp_path: Path) -> None:
    """soak 脚本退出码非 0 即回归（阻塞合并）。

    soak_runner 用 --profile smoke（50 iters）；其余脚本无 profile 参数。
    subprocess timeout 300s 兜底，防脚本挂起阻塞 CI。
    """
    args: list[str] = [sys.executable, str(SOAK_DIR / script)]
    if script == "soak_runner.py":
        args += ["--profile", "smoke", "--json", str(tmp_path / "soak.json")]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"{script} 回归（退出码 {result.returncode}）：\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
