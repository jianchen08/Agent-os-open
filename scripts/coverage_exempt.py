#!/usr/bin/env python
"""覆盖率豁免重型套件——单一名单点（DSH coverage-exempt-heavy-suites 适配）。

机制：插件测试车道拆两个并行 gate，全部测试仍然执行，只有重型套件不再交插桩税：

- **插桩 gate**（run_gates.py 的 `plugins-coverage`）：跑 BASE_TEST_PATHS，
  豁免套件经 ``--ignore`` 从收集中剔除，其余照常 ``--cov=plugins`` +
  ``--cov-fail-under=50``，承担全部阈值证明。
- **无插桩 gate**（`plugins-heavy`）：positional 参数恰好只跑豁免套件，
  不加 ``--cov``——正确性信号一点不缩水，只是不再被覆盖率插桩拖慢。

两条命令的参数都由本模块单点构造（instrumented_args / heavy_paths），
插桩侧与免插桩侧不可能漂移；CI 与本地经由 run_gates.py 调用同一构造。

**名单由阈值自动守护（misconfiguration fails loud）**：
若某个豁免套件实际在 pytest 进程内独家覆盖了某被度量文件（--cov=plugins），
把它豁免出去会让插桩 gate 当场跌破 fail-under 而红——名单错误无法静默通过，
不依赖人工维护名单的正确性。

**成员资格约定**（新增豁免条目必须逐项对账，随名单同文件维护）：
豁免套件在 pytest 进程内执行的每个被度量文件（[tool.coverage.run]
source=plugins），都必须已由其他非豁免套件覆盖，或本就不在阈值口径内；
其子进程执行的部分天然不被父进程 coverage 度量，无需对账。

用法：
    python scripts/coverage_exempt.py --check     # 配对校验（plugins-coverage-pairing 门禁）
    python scripts/coverage_exempt.py --print     # 打印两条 gate 的 pytest 参数
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 插件体系测试的完整路径清单（原 ci.yml python-plugins-test 内联列表的唯一来源，
# 迁移至此使插桩/免插桩两侧共用一个基集）。
BASE_TEST_PATHS: list[str] = [
    "plugins/test_system_plugins.py",
    "plugins/shared/system/llm/",
    "plugins/shared/system/tasks/",
    # 模式插件出厂种子契约测试（2026-09-15 四模式种子接线：缺此条目则
    # plugins/shared/modes/ 下 server.py 不进插桩车道、覆盖率失真）。
    "plugins/shared/modes/",
    # eval_harness 纯函数面（题集展开/聚合/提案校验，2026-09-15 模式服务化接线）。
    "plugins/shared/system/eval_harness/",
    # context_build 双根自举/sidecar 复用测试（2026-09-18 登记：目录内 7 用例
    # 此前不在基集，plugin.py sys.path 守卫缺行失真——覆盖率批十修正）。
    "plugins/shared/pipeline/input/context_build/",
    "plugins/shared/pipeline/input/environment_lifecycle/",
    "plugins/shared/pipeline/input/level_guard/",
    "plugins/shared/pipeline/input/multimodal_preprocessor/",
    "plugins/shared/pipeline/input/tool_schema/",
    "plugins/shared/pipeline/output/duplicate_check/",
    "plugins/shared/tools/task/",
    "plugins/shared/tools/human/",
    "plugins/shared/tools/lsp/",
    "plugins/shared/tools/task_evaluate/",
    "plugins/shared/tools/web_ext/",
    "plugins/shared/tools/builtin_tools/tests/",
    "plugins/shared/system/dsh_adapter/tests/",
    "tests/plugins/",
    "tests/suites/plugins/",
    "tests/channels/",
    # 门禁脚本单测（2026-08-20 覆盖率棘轮门禁批次：check_diff_coverage /
    # check_python_coverage_baseline / check_frontend_baseline 的解析器测试）
    "tests/gates/",
    "tests/test_security_check_allow_priority.py",
    "tests/test_security_check_isolation.py",
    "tests/test_track_stats_contract.py",
    "tests/test_process_watchdog_integration.py",
    "tests/test_isolation_docker_timeout.py",
    "tests/test_isolation_container_self_heal.py",
    # P1-lite 白名单扩容试点（2026-08-16 本地逐文件实跑通过、且在全车道
    # 共跑上下文通过后纳入；test_security_check_soft_block_loop 单跑绿、与
    # test_security_check_isolation 双文件共跑也绿（conftest 裸模块逐出钩子
    # 已修复该对冲突），但全车道共跑仍 7 红（mock.patch 对裸模块名的事后
    # 解析命中车道内其他插件的同名模块），未纳入——迁移债）。
    # 同批未纳入复核（2026-08-16 单跑实测）：host_mode 1 红（危险工具双轨
    # 判定）、per_round 9 红、signature 2 红、workspace_mount 1 红（需真实
    # docker 且容器名冲突）、docker_recheck/io_error/l1_main_agent/
    # namespace_desync 单跑收集即 ImportError（pipeline 依赖车道 conftest）。
    "tests/test_isolation_checkpoint_security.py",
    "tests/test_isolation_concurrent_create.py",
    "tests/test_isolation_docker_provider_injection.py",
    "tests/test_isolation_io_error_self_heal.py",
    "tests/test_isolation_prune_throttle.py",
    "tests/test_isolation_sandbox.py",
    "tests/test_isolation_skills_copy.py",
    # 2026-08-21 覆盖率批次：既有绿灯测试接线进插桩车道（@ci: none-local
    # 未接车道时目标模块整体不进覆盖面：python_packager/server.py（sidecar-only
    # 缺进程内导入）、download/tool.py、triggers_ext、monitoring 等）。
    # 全部目录 2026-08-21 本地单跑绿后接入。
    "plugins/shared/system/python_packager/",
    "plugins/shared/system/monitoring/",
    "plugins/shared/tools/download/",
    "plugins/shared/tools/triggers_ext/",
    # 2026-08-25 批次E：散落测试全绿接入插桩车道（本地全车道共跑 2371 全绿后纳入）。
    # tests/suites（含 core/task 等平铺 import 套件，task_types 独占模块双实例
    # 根因已修：根 conftest 逐出名单去独占名 + 运行期路径守卫）、connectors（0.2
    # 连接器平铺 import）、unit（含 channel_migration 六通道插件契约）、monitoring
    # （0.2 监控模块）、multimodal（storage 槽位收敛）、顶层任务契约与权限测试。
    "tests/suites/",
    "tests/connectors/",
    "tests/unit/",
    "tests/monitoring/",
    "tests/multimodal/",
    "tests/test_delete_task_cascade_pipeline.py",
    # 2026-09-13 覆盖率补测批四：task_submit tool 分支面（权限门/项目挂靠/
    # 继承/workspace 解析/metadata 直调面），缺此条目则新文件不进插桩车道。
    "tests/test_task_submit_tool_branches.py",
    # 2026-09-16 覆盖率收口批：task_submit server.py 的 on_load 三能力桥接线
    # + task_submit 工具包装载荷面（缺此条目则 server.py 21 缺行回涨）。
    "tests/test_task_submit_server_wiring.py",
    # 2026-09-13 覆盖率补测批三：isolation server 适配层（工具面/生命周期/
    # 配置 watcher），缺此条目则新文件不进插桩车道、server.py 覆盖率失真。
    "tests/test_isolation_service_server.py",
    # 2026-09-13 覆盖率补测批七：db_admin server HTTP 面 + rollback reversers
    # 缺口测试（缺此条目则新文件不进插桩车道、模块覆盖率失真）。
    "tests/test_db_admin_server_gaps.py",
    "tests/test_rollback_reversers_gaps.py",
    "tests/test_browser_tool_gaps.py",
    "tests/test_bash_tool_gaps.py",
    "tests/test_llm_core_plugin_gaps.py",
    "tests/tools/test_task_submit_permission_p0.py",
    "tests/tools/test_task_permission_p0.py",
    "tests/tools/test_memory_idor_p0.py",
    # 2026-08-25 批次F：plugins/shared 存量绿灯测试接线（逐文件单跑绿 +
    # security_check 系八文件共跑绿后纳入）。此前 pipeline/** 与 system/tools
    # 散目录不在车道 → context_window_guard/security_check/isolation/
    # hindsight_memory 等模块虽有进程内测试但车道不收集，覆盖率长期失真。
    "plugins/shared/pipeline/input/context_window_guard/",
    "plugins/shared/pipeline/input/prompt_build/",
    "plugins/shared/pipeline/input/security_check/",
    "plugins/shared/pipeline/output/child_task_guard/tests/",
    "plugins/shared/pipeline/output/task_reminder/",
    "plugins/shared/pipeline/output/tool_cache_writer/",
    "plugins/shared/system/agent_manager/",
    "plugins/shared/system/approval/",
    "plugins/shared/system/connectors/",
    # evaluation 读面/执行面行为测试（2026-08-26 A5.3 P0 批：server.py 50%→93%）
    "plugins/shared/system/evaluation/",
    "plugins/shared/system/isolation/",
    "plugins/shared/system/scene/",
    "plugins/shared/system/task_form/",
    "plugins/shared/system/workspace/",
    "plugins/shared/system/hindsight_memory/",
    "plugins/shared/system/review/test_review_persistence.py",
    "plugins/shared/tools/memory/",
    "plugins/shared/tools/resource_merge/",
    "plugins/shared/tools/simple/",
    "plugins/shared/tools/spill_retrieve/",
    # 2026-08-26 批次G：LLM/隔离/任务链孤儿测试接线（12 文件单跑 107 绿
    # 10.2s 后接入；此前在车道名单外 → llm_core adapter 16.6%/system.llm
    # adapter 17.8%/isolation workspace 三件 7.6-17.9% 的覆盖率失真）。
    "tests/test_llm_core_thinking_strength.py",
    "tests/test_llm_adapter_call_streaming.py",
    "tests/test_model_prompt_adapter_plugin.py",
    "tests/test_pipeline_tool_calls_standardization_imports.py",
    "tests/test_workspace_lifecycle_mode.py",
    "tests/test_workspace_git_exclude.py",
    "tests/test_lifecycle_plugins.py",
    "tests/test_task_submit_params.py",
    "tests/test_bug_fixes.py",
    # 2026-08-26 批次H：剩余顶层散落测试全量接线（tests/ 根最后一批车道外
    # 文件 + bash 工具套件 tests/tools/builtin/ + 测试工具箱自检 tests/test_utils/
    # ——658 用例收集 643 绿/12 红，红项修复后接入；tests/e2e_02/ 归 e2e.yml
    # 真后端车道不进本车道）。bash conftest 裸名逐出改条件化（仅逐出指向
    # bash 目录之外的缓存），否则模块双实例让 isinstance 恒假。
    "tests/test_agent_config_fix.py",
    "tests/test_approval_policy_source.py",
    "tests/test_asr_service.py",
    "tests/test_autonomous_context_build_wiring.py",
    "tests/test_context_build_agent_yaml_observability.py",
    "tests/test_context_build_dynamic_vars.py",
    "tests/test_context_build_runtime_params.py",
    "tests/test_duplicate_check_merge.py",
    "tests/test_godot_context_plugin.py",
    "tests/test_host_mode_security.py",
    "tests/test_isolation_docker_recheck.py",
    "tests/test_isolation_io_error.py",
    "tests/test_isolation_l1_main_agent.py",
    "tests/test_isolation_namespace_desync.py",
    "tests/test_isolation_workspace_mount.py",
    "tests/test_new_project_e2e.py",
    "tests/test_port_config.py",
    "tests/test_process_watchdog.py",
    "tests/test_scene.py",
    "tests/test_security_check_dangerous_params.py",
    "tests/test_security_check_per_round.py",
    "tests/test_security_check_permission_mode_api.py",
    "tests/test_security_check_permission_modes.py",
    "tests/test_security_check_signature.py",
    "tests/test_security_check_soft_block_loop.py",
    "tests/test_sensitive_paths.py",
    "tests/test_startup_env_adaptation.py",
    "tests/test_startup_scripts_fix.py",
    "tests/test_tool_block_not_end_pipeline.py",
    "tests/test_tool_schema_drift_detection.py",
    "tests/test_tool_schema_validator.py",
    "tests/test_watchdog_per_process_memory.py",
    "tests/test_workspace_frontend.py",
    "tests/test_utils/",
    "tests/tools/builtin/",
    # 2026-09-13 覆盖率补测批九：cost_control 目录接线（既有并发测试在
    # tests/plugins/ 下、目录内缺口测试 38 用例车道口径绿后纳入）。
    "plugins/shared/system/cost_control/",
    # 2026-09-13 覆盖率补测批九：param_inject 目录接线——既有 test_param_inject.py
    # 9 用例此前从未进车道（目录不在名单）、车道口径全绿后随缺口测试一并纳入。
    "plugins/shared/pipeline/input/param_inject/",
    # 2026-09-14 覆盖率补测批九：context_build 缺口分支补测（config_id 回退/
    # 缓存失效/非 dict yaml/priority/血缘投影/static_vars 装载/层级覆盖），
    # 缺此条目则新文件不进插桩车道、plugin.py 覆盖率失真。
    "tests/test_context_build_gaps.py",
    # 2026-09-15 模式体系 P2：context_build 模式物料档注入（设计稿 §3.3②/
    # §4.1/§4.2）行为测试，缺此条目则 mode_material.py 不进插桩车道、覆盖率失真。
    "tests/test_context_build_mode_material.py",
    # 2026-09-15 模式体系 P3：mode 键两级解析第二级（§3.3 系统注册表未命中 →
    # 模式包 agents/<stem>.yaml）装配测试，缺此条目则 plugin.py 模式分支
    # 不进插桩车道、改动行覆盖率失真。
    "tests/test_context_build_mode_agent_key.py",
    # 2026-09-14 覆盖率补测批九：review 缺口测试接线（逐文件登记——目录内
    # test_review_hindsight_e2e.py 是环境门槛 e2e，不进插桩车道）。
    "plugins/shared/system/review/test_review_gaps.py",
    "plugins/shared/system/review/test_improvement.py",
    # 2026-09-14 覆盖率补测批九：_host/host.py 缺口测试（_host 非插件目录，
    # 文件自带路径注入不依赖 tests/plugins/_host/conftest）。
    "tests/test_host_shared_gaps.py",
    # 2026-09-14 覆盖率补测批十：两文件此前不在任何车道（模块覆盖率失真），
    # 随缺口补测一并接线——model_prompt_adapter 顶层非映射文档降级缺陷已同刀修复。
    "tests/test_model_prompt_adapter_gaps.py",
    # 批十：review 缺口补测随插件目录接线（原 tests/test_review_gaps_2.py 归位到
    # plugins/shared/system/review/，与该目录既有 test_review_gaps.py 同址）。
    "plugins/shared/system/review/test_review_service_gaps.py",
    # 2026-09-14 覆盖率补测簇 J：pipeline/_base 基础三件套（IPlugin 接口族/
    # PluginContext/PluginResult/create_initial_state/find_plugin_config）此前
    # 不在任何车道（_base 非插件目录、SDK 侧是复制品），模块覆盖率长期失真。
    # 插件侧 _base 由各 pipeline 插件的顶层 re-export（pipeline/plugin.py、
    # pipeline/types.py）消费，缺此条目则新文件不进插桩车道。
    "tests/test_pipeline_base_gaps.py",
    # 2026-09-15：track 统计并入 llm_core（ADR track-merged-into-llm-core），
    # 原 6 个 tests/test_track_*.py 归并为契约测试 + 接线测试两文件并原址接线；
    # 并入后的 manifest 声明面（reads/persistent/export/grants）由契约测试钉死。
    "tests/test_llm_core_track_wiring.py",
    "tests/test_llm_core_track_manifest.py",
    # 2026-09-14 覆盖率补测簇 I（llm 簇）：llm_core/server.py 的 execute 工具面
    # （102-117 行）此前不在任何车道——该目录整体收集会与 system/llm 的平铺
    # `import adapter` 裸名冲突（3 文件收集期 AttributeError），故只登记执行面
    # 单文件（自带唯一模块名装载，与车道共跑实测 2794 绿/服务器面 100%）。
    "plugins/shared/pipeline/core/llm_core/test_llm_core_server_execute.py",
    # 2026-09-14 覆盖率补测批十（簇 B/C）：shared 根散模块 + 三个未登记插件目录
    # 的缺口补测——这些文件不在任何已登记目录下，缺此条目则模块覆盖率失真。
    "tests/test_host_shared_modules_gaps.py",   # project_registry/tenant_data/repo_anchor/proc_tree/user_space/state_fields/bounded_dict/uploads_path
    "tests/test_metrics_admin_server_gaps.py",  # metrics_admin/server.py
    "tests/test_task_form_server_gaps.py",      # task_form/server.py
    "tests/test_tool_cache_gaps.py",            # pipeline/input/tool_cache
    # 2026-09-16 覆盖率收官批：artifacts 缺口测试接线（版本链断点截断等，
    # 此前文件不在车道 → artifact_service.py 覆盖率失真）。
    "tests/test_artifacts_gaps.py",
    "tests/test_workspace_lifecycle_gaps.py",   # pipeline/input/workspace_lifecycle
]

# 车道 marker 过滤：@pytest.mark.timing 用例唯一归 timing-gate（独立 stage，
# §9.4），本文件两条车道（插桩/免插桩）一律排除，避免时序用例重复跑。
MARKER_FILTER = "not timing"


@dataclass(frozen=True)
class ExemptSuite:
    """一个豁免套件：positional 路径 + 逐项对账记录。

    新增条目要求：填齐 measured_in_process / covered_by 两个字段完成对账——
    说明它在 pytest 进程内执行了哪些被度量文件、这些文件的覆盖由谁接住。
    对账不成立的条目会让插桩 gate 跌破阈值而红（自动守护）。
    """

    path: str
    profile: str  # 为什么重（重型画像）
    measured_in_process: str  # 进程内执行的被度量代码对账
    covered_by: str  # 覆盖由谁接住


EXEMPT_SUITES: list[ExemptSuite] = [
    ExemptSuite(
        path="tests/plugins/test_plugin_smoke_matrix.py",
        profile=(
            "全插件冒烟矩阵：全部 Python sidecar 插件逐个以子进程加载"
            "（cwd=插件目录，与生产 sidecar 语义一致）+ external_mcp + native，"
            "每个参数化用例一次 Python 子进程启动 + 插件全量 import"
        ),
        measured_in_process=(
            "无——插件代码全部在探针子进程（plugin_probe.py）内执行，"
            "父进程 coverage 测不到子进程；测试文件自身仅 import json/os/subprocess/pytest"
        ),
        covered_by=(
            "各插件的进程内单测（tests/plugins/{system,input,output,shared}/ 与"
            " plugins/shared/**/test_*.py，均留在插桩 gate 内）+ 免插桩并行 gate 本身的红绿"
        ),
    ),
]


def heavy_paths() -> list[str]:
    """无插桩 gate 的 positional 路径：恰好等于豁免名单。"""
    return [s.path for s in EXEMPT_SUITES]


def instrumented_args() -> list[str]:
    """插桩 gate 的 pytest 参数：基集 + --ignore 豁免套件 + 公共过滤。"""
    args = list(BASE_TEST_PATHS)
    args.append("-m")
    args.append(MARKER_FILTER)
    for p in heavy_paths():
        args.append(f"--ignore={p}")
    return args


def heavy_args() -> list[str]:
    """无插桩 gate 的 pytest 参数：豁免名单 + 公共过滤（无 --cov）。"""
    args = list(heavy_paths())
    args.append("-m")
    args.append(MARKER_FILTER)
    return args


def check() -> int:
    """配对校验（廉价、静态）：文件存在、豁免 ⊆ 基集树、参数不重叠。"""
    problems: list[str] = []

    for p in heavy_paths():
        path = ROOT / p
        if not path.exists():
            problems.append(f"豁免套件不存在: {p}")
            continue
        # 豁免路径必须被基集覆盖（基集中的某条目是其祖先）
        covered = any(
            p == base or p.startswith(base.rstrip("/\\") + "/") or p.startswith(base.rstrip("/\\") + "\\")
            for base in BASE_TEST_PATHS
        )
        if not covered:
            problems.append(f"豁免套件 {p} 不在 BASE_TEST_PATHS 覆盖范围内（免插桩 gate 会跑到基集之外的测试）")

    for base in BASE_TEST_PATHS:
        if not (ROOT / base).exists():
            problems.append(f"基集路径不存在: {base}")

    # 配对不变量：插桩侧剔除的 --ignore 名单 ≡ 免插桩侧 positional 名单
    ignores = sorted(a.removeprefix("--ignore=") for a in instrumented_args() if a.startswith("--ignore="))
    if ignores != sorted(heavy_paths()):
        problems.append(f"--ignore 名单与 positional 名单不一致: {ignores} != {sorted(heavy_paths())}")

    if problems:
        print("coverage_exempt: 配对校验失败：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    n_ignores = len(ignores)
    print(
        f"coverage_exempt: 配对校验通过——基集 {len(BASE_TEST_PATHS)} 条，豁免 {len(EXEMPT_SUITES)} 条"
        f"（--ignore {n_ignores} 条 = positional {len(heavy_paths())} 条）"
    )
    for s in EXEMPT_SUITES:
        print(f"  豁免: {s.path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mutex = parser.add_mutually_exclusive_group(required=True)
    mutex.add_argument("--check", action="store_true", help="配对校验（plugins-coverage-pairing 门禁）")
    mutex.add_argument("--print", dest="print_args", action="store_true", help="打印两条 gate 的 pytest 参数")
    ns = parser.parse_args()
    if ns.check:
        return check()
    print("插桩 gate（plugins-coverage）pytest 参数:")
    print("  " + " ".join(instrumented_args()))
    print("无插桩 gate（plugins-heavy）pytest 参数:")
    print("  " + " ".join(heavy_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
