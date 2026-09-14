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


# ═══════════════════════════════════════════════════════════════
# P0: post 出口路由契约——任务管道纯文本轮不得直接终局（D2 一次性终局修复）
# 2026-09-14：兜底 end 使第 1 轮纯文本即整管终局（假绿 completed/task 恒
# running，批次实锤多例）。契约：任务管道（task.id 存在）非会话模式、非终态
# 的纯文本轮回 llm_core，由 task_reminder 预算耗尽裁决收口；兜底 end 永远
# 最后且无条件。
# ═══════════════════════════════════════════════════════════════


class TestPostRouteTaskTextOnlyLoop:
    def _post_next(self):
        import yaml

        active = (
            Path(__file__).resolve().parent.parent / "config" / "pipelines" / "autonomous.yaml"
        )
        cfg = yaml.safe_load(active.read_text(encoding="utf-8"))

        def _find_post(node):
            """递归定位 id=post 且带 next 的路由节点（post 嵌套在 phase 组内）。"""
            if isinstance(node, dict):
                if node.get("id") == "post" and "next" in node:
                    return node["next"]
                for value in node.values():
                    found = _find_post(value)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = _find_post(item)
                    if found is not None:
                        return found
            return None

        rules = _find_post(cfg)
        if rules is None:
            raise AssertionError("autonomous.yaml 未找到 post.next 路由块")
        return rules

    def test_fallback_end_is_last_and_unconditional(self):
        """兜底 end 必须是最后一条且无条件（所有可续跑规则都在它之前）。"""
        rules = self._post_next()
        assert rules[-1].get("then") == "end"
        assert "when" not in rules[-1] or not rules[-1]["when"]

    def test_task_text_only_loop_before_fallback_end(self):
        """任务管道纯文本轮回 llm_core 的规则必须存在且位于兜底 end 之前。"""
        rules = self._post_next()
        loop_rules = [
            r
            for r in rules[:-1]
            if r.get("then") == "loop"
            and r.get("when")
            and "task.id != none" in r["when"]
            and "conversation_mode != True" in r["when"]
        ]
        assert loop_rules, (
            "缺少任务管道纯文本轮回规则（task.id != none + 非会话 + 非终态 → loop）："
            "纯文本首轮即终局的假绿缺陷会回归"
        )
        rule = loop_rules[0]
        for terminal in ("completed", "failed", "cancelled"):
            assert f"task.status != '{terminal}'" in rule["when"], (
                f"回环规则必须豁免终态 task.status={terminal}"
            )
        # 排序：回环规则必须先于兜底 end（rules[-1]）
        assert rules.index(rule) < len(rules) - 1
