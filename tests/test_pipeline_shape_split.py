# @feature: FP-0.2.二 管道配置接线 | @ci: python-test
"""autonomous.yaml 形态拆分契约锁定（ADR 2026-10-10-pipeline-shape-split-core-type）。

main 体是双形态交替循环：post 路由写 core_type（'llm_call'|'tool_execute'）
决定下一迭代形态，引擎 execute_body 据此决定开轮/沿用打开轮 message_id。
本文件锁六件事（防回融/防迁移漂移）：
  1. 两面互斥组门（G9）——工具面三组与 LLM 面两组的 when 逐字锁死；
  2. post.next 次序契约——工具收尾路由居对话挂起之前、派发路由写形态、
     全部 then:loop 路由显式写 core_type（防残留形态跨 run 泄漏）、
     task.status 终态收束路由带 task.id 守卫（防会话残留终态吞工具调用）；
  3. init 体一次性脚手架成对（agent_config_load 紧随 mode_material_inject；
     tool_schema/host_context 经用户裁定确认为动态，不在 init）；
  4. 不可门化五步（prompt_build / multimodal_preprocessor / todo.fold /
     tool_schema / host_context）在 prepare 且无 when；
  5. tool_exec loop_config 字段集逐一等于既值（for-each 契约，ADR
     2026-10-02-loopconfig-parallel-foreach）；
  6. 本管道顶层声明 max_rounds: 400（形态拆分后每周期 2 迭代，有效周期预算
     200 不变；内核缺省 200 零改动，预算口径归采用形态拆分的管道自声明）。
"""
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent
_YAML = _REPO / "config" / "pipelines" / "autonomous.yaml"

TOOL_FACE_WHEN = "core_type == 'tool_execute'"
LLM_FACE_WHEN = "core_type != 'tool_execute'"


def _load():
    return yaml.safe_load(_YAML.read_text(encoding="utf-8"))


def _main_body(cfg=None):
    cfg = cfg or _load()
    return next(b for b in cfg["loop_bodies"] if b["id"] == "main")


def _init_body_steps(cfg=None):
    """init 体步骤名（字符串步直取；带 when 的条件调用步取 name 键——
    dd80ec851 起 init 存在 dict 形态条件步，裸 str 过滤会把条件步漏出
    步骤序列，lifecycle 前置断言失真）。"""
    cfg = cfg or _load()
    body = next(b for b in cfg["loop_bodies"] if b["id"] == "init")
    inner = next(s for s in body["steps"] if isinstance(s, dict) and s.get("id") == "init")
    return [
        s if isinstance(s, str) else s.get("name")
        for s in inner["steps"]
        if isinstance(s, (str, dict))
    ]


def _group(group_id, cfg=None):
    cfg = cfg or _load()
    return next(
        s for s in _main_body(cfg)["steps"] if isinstance(s, dict) and s.get("id") == group_id
    )


def _step_names(group):
    names = []
    for s in group["steps"]:
        if isinstance(s, str):
            names.append(s)
        elif isinstance(s, dict):
            names.append(s.get("name"))
    return names


# ═══════════════════════════════════════════════════════════════
# 1. 两面互斥组门（防回融）
# ═══════════════════════════════════════════════════════════════


class TestFaceExclusiveGates:
    def test_tool_face_groups_gated_on_tool_execute(self):
        for gid in ("tool_input", "core", "tool_exec"):
            group = _group(gid)
            assert group["when"] == TOOL_FACE_WHEN, (
                f"{gid} 组门必须恰为 {TOOL_FACE_WHEN!r}（工具迭代面），"
                "弱化为无门即回融——守卫链/执行核每轮白付的缺陷回归"
            )

    def test_llm_face_groups_gated_on_not_tool_execute(self):
        for gid in ("prepare", "llm"):
            group = _group(gid)
            assert group["when"] == LLM_FACE_WHEN, (
                f"{gid} 组门必须恰为 {LLM_FACE_WHEN!r}（LLM 迭代面，缺省形态）"
            )

    def test_faces_are_mutually_exclusive(self):
        assert TOOL_FACE_WHEN != LLM_FACE_WHEN
        # 同一迭代两面不可同真：== 与 != 对 core_type 的任意取值互斥
        for core_type in (None, "llm_call", "tool_execute", "other"):
            tool_active = core_type == "tool_execute"
            llm_active = core_type != "tool_execute"
            assert tool_active != llm_active, core_type


# ═══════════════════════════════════════════════════════════════
# 2. post.next 次序契约
# ═══════════════════════════════════════════════════════════════


class TestPostRouteOrder:
    def _routes(self):
        return _group("post")["next"]

    def test_tool_tail_route_before_conversation_suspend(self):
        """工具迭代收尾回环必须居对话挂起路由之前。

        工具迭代 post 时刻 raw_tool_calls 可能已被缓存命中清空，对话挂起路由
        若先判会在 LLM 消费结果之前抢跑挂起。
        """
        routes = self._routes()
        tool_tail_idx = next(
            i for i, r in enumerate(routes) if r.get("when") == "core_type == 'tool_execute'"
        )
        suspend_idx = next(
            i
            for i, r in enumerate(routes)
            if r.get("when") == "conversation_mode == True and raw_tool_calls == []"
        )
        assert tool_tail_idx < suspend_idx, "工具收尾回环必须先于对话挂起路由"

    def test_dispatch_routes_write_tool_execute_shape(self):
        """raw_tool_calls 派发路由（含 conversation 清除变体）必须写 tool_execute。"""
        routes = self._routes()
        dispatch = [
            r
            for r in routes
            if r.get("when") and "raw_tool_calls != []" in r["when"] and r.get("then") == "loop"
        ]
        assert len(dispatch) >= 2, "派发路由至少两条（通用 + conversation 清除变体）"
        for r in dispatch:
            assert r.get("set", {}).get("core_type") == "tool_execute", (
                f"派发路由必须显式写 core_type=tool_execute：{r}"
            )
        conv_clear = [r for r in dispatch if "conversation_mode == True" in r["when"]]
        assert conv_clear, "conversation 清除变体路由必须存在"
        assert conv_clear[0]["set"].get("conversation_mode") is False, (
            "conversation 清除变体必须同时清 conversation_mode（先清再回环）"
        )

    def test_all_loop_routes_write_core_type(self):
        """全部 then:loop 路由均显式写 core_type（防残留形态跨迭代泄漏）。"""
        bare = [
            r
            for r in self._routes()
            if r.get("then") == "loop" and "core_type" not in r.get("set", {})
        ]
        assert not bare, f"以下回环路由缺 core_type 写入（残留形态泄漏面）：{bare}"

    def test_suspend_route_writes_llm_call(self):
        """挂起路由显式写 llm_call：恢复后续 run 首迭代形态确定。"""
        routes = self._routes()
        suspend = next(
            r
            for r in routes
            if r.get("when") == "conversation_mode == True and raw_tool_calls == []"
        )
        assert suspend["set"]["core_type"] == "llm_call"
        assert suspend["set"]["suspended"] is True

    def test_task_terminal_end_route_gated_by_task_id(self):
        """task.status 终态收束路由必须带 task.id 守卫（任务管道判别键）。

        会话管道的裸 task.status 是跨 run 残留（装机 2026-10-21 实锤：reconcile
        把会话自己的 run 取消簿记误补写成会话的 task.status=cancelled，本路由
        先于 raw_tool_calls 回环命中——每轮 LLM 产出的工具调用未消费即收束，
        引擎异常终局闸署名 dropped_tool_calls，会话死锁）。task.id 仅在任务
        管道由 agent_config_load 写入（lineage 键存在时），会话恒缺——守卫后
        会话轮不被此路由截杀。
        """
        routes = self._routes()
        terminal = [
            r
            for r in routes
            if r.get("then") == "end"
            and r.get("when")
            and "task.status == 'completed'" in r["when"]
        ]
        assert len(terminal) == 1, f"task.status 终态收束路由必须恰一条：{terminal}"
        when = terminal[0]["when"]
        assert "task.id != none" in when, f"终态收束路由缺 task.id 守卫（死锁回归面）：{when}"
        assert when.index("task.id != none") < when.index("task.status"), (
            "task.id 守卫必须与 task.status 同层 and（先判管道身份再判终态）"
        )

    def test_terminal_end_route_includes_evaluation_pass_projection(self):
        """终态收束路由必须含评估通过投影键（评估通过当轮直接收束，零空转）。

        ``task_evaluation_completed`` 是 tool_core 在 task_evaluate 评估通过当轮
        写入 working state 的投影键（评估通过写面 pipeline-state.update 只落
        DB/内存 registry，在飞 state 的 task.status 当轮不同步）——缺此条件时
        评估通过的工具轮命中不了本路由，回 LLM 空转一轮后才由 task_reminder
        的 DB 权威位收束（"评估通过后直接停止"回归面）。
        """
        routes = self._routes()
        terminal = next(
            r
            for r in routes
            if r.get("then") == "end"
            and r.get("when")
            and "task.status == 'completed'" in r["when"]
        )
        when = terminal["when"]
        assert "task_evaluation_completed == True" in when, (
            f"终态收束路由缺评估通过投影键（评估通过轮回 LLM 空转）：{when}"
        )
        assert "or" in when, "投影键必须以 or 并入终态判据（任一终态证据即收束）"


# ═══════════════════════════════════════════════════════════════
# 3. init 体一次性脚手架（成对 + 次序）
# ═══════════════════════════════════════════════════════════════


class TestInitOnceScaffold:
    def test_pair_and_order(self):
        steps = _init_body_steps()
        assert "pipeline_agent_config_load" in steps, "agent_config_load 未接入 init 体"
        idx = {name: i for i, name in enumerate(steps)}
        assert idx["pipeline_agent_config_load"] + 1 == idx["pipeline_mode_material_inject"], (
            "mode_material_inject 必须紧跟 agent_config_load（成对约束：非幂等追加"
            "靠基线重建防重复，拆开单迁必膨胀）"
        )

    def test_dynamic_steps_not_in_init(self):
        """tool_schema/host_context 是动态步（用户裁定 2026-10-10），不得入 init。"""
        steps = _init_body_steps()
        for dynamic in ("pipeline_tool_schema", "pipeline_host_context"):
            assert dynamic not in steps, (
                f"{dynamic} 在 init 体（每 run 冻结一次）：动态步回归 init 即丢中态同步"
            )

    def test_lifecycle_steps_still_lead_init(self):
        steps = _init_body_steps()
        assert steps[:2] == ["pipeline_workspace_lifecycle", "pipeline_environment_lifecycle"]

    def test_scaffold_absent_from_main_prepare(self):
        """上提后 main.prepare 不得残留一次性脚手架（残留即每轮白付回归）。"""
        prepare_names = _step_names(_group("prepare"))
        for leaked in (
            "pipeline_agent_config_load",
            "pipeline_mode_material_inject",
        ):
            assert leaked not in prepare_names, f"{leaked} 仍在 main.prepare（每轮白付回归）"


# ═══════════════════════════════════════════════════════════════
# 4. 不可门化五步
# ═══════════════════════════════════════════════════════════════


class TestPerRoundMandatorySteps:
    def test_five_steps_in_prepare_without_when(self):
        """prompt_build / multimodal / todo.fold / tool_schema / host_context 必须每 LLM 轮执行。"""
        prepare = _group("prepare")
        names = _step_names(prepare)
        for mandatory in (
            "pipeline_prompt_build",
            "pipeline_multimodal_preprocessor",
            "todo.fold",
            "pipeline_tool_schema",
            "pipeline_host_context",
        ):
            assert mandatory in names, f"{mandatory} 不在 prepare 面"
        for s in prepare["steps"]:
            if isinstance(s, dict) and s.get("name") in (
                "pipeline_prompt_build",
                "pipeline_multimodal_preprocessor",
                "todo.fold",
                "pipeline_tool_schema",
                "pipeline_host_context",
            ):
                assert not s.get("when"), f"{s['name']} 被门化（不可门化五步纪律破坏）：{s}"


# ═══════════════════════════════════════════════════════════════
# 5. tool_exec loop_config 字段集（防迁移漂移）
# ═══════════════════════════════════════════════════════════════


class TestToolExecLoopConfig:
    EXPECTED = {
        "as": "current_call",
        "collect": ["tool_results", "_full_tool_results", "_executed_tool_calls"],
        "collect_append": ["submitted_task_ids"],
        "consume": True,
        "consume_keys": ["pre_decided_results"],
        "enabled": True,
        "max_concurrency": 4,
        "over": "state.raw_tool_calls",
    }

    def test_fields_match_exactly(self):
        lc = _group("tool_exec")["loop_config"]
        assert set(lc.keys()) == set(self.EXPECTED.keys()), (
            f"loop_config 字段集漂移：{sorted(lc.keys())}"
        )
        for key, expected in self.EXPECTED.items():
            assert lc[key] == expected, f"loop_config.{key} 漂移：{lc[key]!r} != {expected!r}"

    def test_tool_exec_single_step_is_tool_core(self):
        assert _step_names(_group("tool_exec")) == ["pipeline_tool_core"]


# ═══════════════════════════════════════════════════════════════
# 6. 管道自声明轮预算（形态拆分后 400 = 有效周期预算 200；内核零改动）
# ═══════════════════════════════════════════════════════════════


def test_pipeline_declares_max_rounds():
    """max_rounds 顶层声明在场——预算口径随形态拆分管道自声明。

    2400 = 轮次预算唯一所有者 stop_check（按 LLM 调用次数计，2026-10-17 用户
    裁定）之下的引擎机械硬顶兜底：1000 次调用 ≤ 2000 迭代（mode_coding/
    mode_godot 编排 agent 预算 2026-11-14 用户裁定上调至 1000），2400 留 ~20%
    余量（错误轮/重试也占迭代），确保硬顶永不先于预算触发。
    """
    cfg = _load()
    assert cfg.get("max_rounds") == 2400, (
        "autonomous.yaml 顶层必须声明 max_rounds: 2400（2026-11-14 用户裁定）："
        "缺声明即回落引擎缺省 200，形态拆分下 1000 次调用预算会被硬顶截断"
    )


def test_engine_default_max_rounds_unchanged():
    """内核零改动锁定：引擎缺省仍 200（用户复核裁定 2026-10-10）。"""
    src = (_REPO / "kernel" / "crates" / "engine" / "src" / "pipeline_loop.rs").read_text(
        encoding="utf-8"
    )
    assert "const DEFAULT_MAX_ROUNDS: usize = 200;" in src, (
        "引擎缺省轮数被改动：max_rounds 预算走管道自声明路线，内核零改动"
    )
