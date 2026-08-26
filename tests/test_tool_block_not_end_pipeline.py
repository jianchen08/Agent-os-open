# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-test
from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("core", "tool_core")
"""工具级拦截应转为「工具失败结果」而非终结整个管道。

历史 Bug:
  0.1 的 config/pipelines/default.yaml 曾有三条 input 路由（security_blocked /
  level_blocked / isolation_blocked）用 target=end，把工具级权限/隔离/安全
  拦截放大成整个管道终结——拦截原因被写进 RAW_RESULT 当成最终输出，
  导致「权限不足」变成任务最终结果、任务被错误标记完成/失败、停止后重发
  消息时 agent 身份丢失。

修复:
  1. 删除这三条 target=end 路由
  2. tool_core 新增 _check_tool_blocked：执行工具前统一检查 level/isolation/
     security 三类拦截决策，被拦截的工具转为 success=False 的失败结果返回
     给 LLM，让 LLM 自行调整策略，管道继续流转。

2026-08-21 更新：旧 default.yaml 等 0.1 过渡期管道配置已整体删除，
现役唯一管道为 autonomous.yaml（G10 DSL）。原 TestCheckToolBlocked 六个
Python 行为用例随 ToolCore Python 实现退役（0.2 迁移为 Rust native 插件，
拦截逻辑在 plugins/shared/pipeline/core/tool_core/src/types.rs::check_tool_blocked）
一并删除，契约由 Rust 侧单测承接。
"""
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# P0: 现役管道（autonomous.yaml，G10 DSL）不得有 target=end 的工具拦截
# ═══════════════════════════════════════════════════════════════


class TestNoEndRouteForToolBlock:
    """P0: 工具级拦截路由不得用 target=end 终结管道。

    旧 default.yaml（0.1 扁平 input_routes 格式）曾有三条 target=end 拦截
    路由，已于 2026-08-21 连同过渡期配置文件一并删除；现役唯一管道
    autonomous.yaml 为 G10 DSL（loop_bodies/next），不存在 input_routes。
    本契约由 TestCheckToolBlocked 六个行为用例持续锁定。
    """

    def test_no_legacy_pipeline_configs_remain(self):
        """config/pipelines/ 下不得再有旧格式管道文件（default/l1/l2）。"""
        import yaml
        pipes_dir = Path(__file__).resolve().parent.parent / "config" / "pipelines"
        for legacy in ("default.yaml", "l1-main.yaml", "l2-evaluator.yaml", "l2-subtask.yaml"):
            assert not (pipes_dir / legacy).exists(), (
                f"{legacy} 是 0.1 过渡期旧格式（input_routes/inherit），已退役，请删除"
            )
        # 现役管道必须是 G10 DSL 格式（loop_bodies），不得回退扁平路由
        active = pipes_dir / "autonomous.yaml"
        assert active.exists(), "现役管道 autonomous.yaml 必须存在"
        cfg = yaml.safe_load(active.read_text(encoding="utf-8"))
        assert "loop_bodies" in cfg, "autonomous.yaml 必须是 G10 loop_bodies 格式"
        assert "input_routes" not in cfg, "autonomous.yaml 不得含 0.1 input_routes"
