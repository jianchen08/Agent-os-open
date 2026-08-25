"""验证已修复 bug 的行为回归测试。"""
from __future__ import annotations

from pathlib import Path

# ── BUG-4: 静默异常添加日志 ─────────────────────────────────────────────

class TestBug4SilentExceptionLogging:
    """验证关键路径的 except Exception 块有 logger.warning 日志。"""

    def test_workspace_lifecycle_no_silent_exceptions(self):
        """BUG-4: workspace_lifecycle.py 关键位置不应有静默 pass。"""
        wl_path = Path("plugins/shared/system/isolation/workspace_lifecycle.py")
        assert wl_path.exists(), f"文件不存在: {wl_path}"

        # 检查修复的关键行范围（允许非关键路径有 pass）
        violations: list[str] = []
        lines = wl_path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "except Exception:":
                for j in range(i + 1, min(i + 3, len(lines))):
                    next_stripped = lines[j].strip()
                    if next_stripped == "":
                        continue
                    if next_stripped == "pass":
                        violations.append(f"行 {j + 1}: except Exception: pass")
                    break
        assert len(violations) == 0, (
            f"workspace_lifecycle.py 仍有静默异常: {violations}"
        )
