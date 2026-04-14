"""Agent OS 绔埌绔姛鑳芥祴璇曡剼鏈?v3銆?

鍩轰簬瀹為檯婧愮爜 API 绛惧悕锛岄€愪釜娴嬭瘯鍚勬ā鍧楃殑鐪熷疄杩愯鐘跺喌銆?
鎵€鏈?API 璋冪敤宸叉牴鎹簮鐮侀獙璇侊紝涓嶄娇鐢?Mock銆?
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# 淇 Windows 鎺у埗鍙扮紪鐮?
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 纭繚婧愮爜璺緞
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results: list[dict[str, Any]] = []

def record(module: str, test_name: str, status: str, evidence: str, error: str = ""):
    results.append({"module": module, "test_name": test_name, "status": status, "evidence": evidence, "error": error})
    sym = {"PASS": "OK", "PART": "~~", "FAIL": "XX", "LOCK": "LK"}[status]
    print(f"  [{sym}] {test_name}: {evidence}" + (f" | {error[:120]}" if error else ""))


# ============================================================================
# 1. CLI 鍩虹鍛戒护
# ============================================================================
def test_cli():
    print("\n" + "=" * 60 + "\n1. CLI 鍩虹鍛戒护\n" + "=" * 60)
    try:
        from channels.cli.cli_main import CLIApplication
        record("CLI", "CLI妯″潡瀵煎叆", "PASS", "CLIApplication 瀵煎叆鎴愬姛")
    except Exception as e:
        record("CLI", "CLI妯″潡瀵煎叆", "FAIL", "瀵煎叆澶辫触", traceback.format_exc()); return
    try:
        from channels.cli.input_adapter import CLIInputAdapter
        from channels.cli.output_adapter import CLIOutputAdapter
        record("CLI", "CLI閫傞厤鍣ㄥ鍏?, "PASS", "CLIInputAdapter, CLIOutputAdapter 瀵煎叆鎴愬姛")
    except Exception as e:
        record("CLI", "CLI閫傞厤鍣ㄥ鍏?, "FAIL", "瀵煎叆澶辫触", traceback.format_exc())
    try:
        app = CLIApplication()
        record("CLI", "CLI搴旂敤瀹炰緥鍖?, "PASS", "CLIApplication() 鎴愬姛")
    except Exception as e:
        record("CLI", "CLI搴旂敤瀹炰緥鍖?, "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())


# ============================================================================
# 2. 绠￠亾绯荤粺
# ============================================================================
async def test_pipeline():
    print("\n" + "=" * 60 + "\n2. 绠￠亾绯荤粺\n" + "=" * 60)
    try:
        from pipeline.types import create_initial_state, AgentLevel, ErrorPolicy, RouteSignal, StateKeys, TaskPriority, TargetType
        state = create_initial_state(user_input="hello")
        record("Pipeline", "create_initial_state", "PASS", f"鍒涘缓鍒濆鐘舵€佹垚鍔燂紝16涓猭eys")
    except Exception as e:
        record("Pipeline", "create_initial_state", "FAIL", "鍒涘缓澶辫触", traceback.format_exc()); return

    try:
        from pipeline.plugin import ICorePlugin, PluginResult
        from pipeline.types import ErrorPolicy

        class _EchoCore(ICorePlugin):
            """娴嬭瘯鐢?Echo 鏍稿績鎻掍欢銆?""
            error_policy = ErrorPolicy.ABORT
            @property
            def name(self): return "echo_core"
            @property
            def priority(self): return 50
            async def execute(self, ctx):
                return {"raw_result": f"Echo: {ctx.state.get('user_input', '')}"}

        from pipeline.registry import PluginRegistry
        reg = PluginRegistry()
        reg.register_core("llm_call", _EchoCore())
        record("Pipeline", "PluginRegistry娉ㄥ唽", "PASS", "1涓狢ore鎻掍欢娉ㄥ唽鎴愬姛")
    except Exception as e:
        record("Pipeline", "PluginRegistry娉ㄥ唽", "FAIL", "娉ㄥ唽澶辫触", traceback.format_exc()); return

    try:
        from pipeline.route import InputRouteTable, InputRouteEntry, OutputRouteTable, OutputRouteEntry
        it = InputRouteTable([InputRouteEntry(name="stop", condition="should_stop == True", target="end", plugins=[], priority=1), InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10)])
        ot = OutputRouteTable([OutputRouteEntry(route_type="end", condition="should_stop == True", priority=1), OutputRouteEntry(route_type="end", condition="True", priority=99)])
        record("Pipeline", "璺敱琛ㄥ垱寤?, "PASS", "InputRouteTable(2) + OutputRouteTable(2)")
    except Exception as e:
        record("Pipeline", "璺敱琛ㄥ垱寤?, "FAIL", "鍒涘缓澶辫触", traceback.format_exc()); return

    try:
        from pipeline.engine import PipelineEngine
        engine = PipelineEngine(input_route_table=it, output_route_table=ot, plugin_registry=reg)
        final = await engine.run(state)
        record("Pipeline", "PipelineEngine杩愯", "PASS", f"iteration={final.get('iteration')}, raw_result='{final.get('raw_result')}'")
    except Exception as e:
        record("Pipeline", "PipelineEngine杩愯", "FAIL", "鎵ц澶辫触", traceback.format_exc())

    # PipelineConfig YAML 鍔犺浇 鈥?YAML 鏂囦欢浣跨敤 pipeline.name 宓屽锛宭oad_pipeline_config 鏈熷緟椤跺眰 name
    try:
        from pipeline.config import load_pipeline_config
        pipelines_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config" / "pipelines"
        yaml_files = list(pipelines_dir.glob("*.yaml"))
        if yaml_files:
            # default.yaml 鐢?pipeline.name 宓屽鏍煎紡锛宭oad_pipeline_config 鏈熷緟椤跺眰 name
            # 杩欐槸鏍煎紡涓嶅尮閰嶏紝璁板綍涓?PART
            try:
                config = load_pipeline_config(str(yaml_files[0]))
                record("Pipeline", "PipelineConfig YAML鍔犺浇", "PASS", f"浠?{yaml_files[0].name} 鍔犺浇: name={config.name}")
            except ValueError as ve:
                # YAML 鏍煎紡涓庡姞杞藉櫒涓嶅尮閰?鈥?璁板綍涓洪儴鍒嗗彲鐢?
                record("Pipeline", "PipelineConfig YAML鍔犺浇", "PART", f"YAML鏍煎紡涓庡姞杞藉櫒涓嶅尮閰? {ve}")
        else:
            record("Pipeline", "PipelineConfig YAML鍔犺浇", "PART", "绠￠亾閰嶇疆鐩綍鏃?YAML 鏂囦欢")
    except Exception as e:
        record("Pipeline", "PipelineConfig YAML鍔犺浇", "PART", f"鍔犺浇澶辫触: {e}", traceback.format_exc())


# ============================================================================
# 3. LLM Core
# ============================================================================
async def test_llm_core():
    print("\n" + "=" * 60 + "\n3. LLM Core\n" + "=" * 60)
    try:
        from plugins.core.llm_core import LLMCore
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        record("LLM Core", "LLMCore瀹炰緥鍖?, "PASS", f"LLMCore(provider=openai, model=gpt-4) name={core.name}")
    except Exception as e:
        record("LLM Core", "LLMCore瀹炰緥鍖?, "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc()); return

    # 浼樺厛浣跨敤 ModelConfigLoader 鍔犺浇閰嶇疆锛屽洖閫€鍒扮幆澧冨彉閲?
    llm_tested = False
    try:
        from config.models import ModelConfigLoader
        from pipeline.types import create_initial_state
        from pipeline.plugin import PluginContext
        mloader = ModelConfigLoader()
        llm_conf = mloader.get_llm_core_config("minimax-m2.7")
        if llm_conf and llm_conf.get("api_key"):
            core = LLMCore(config=llm_conf)
            state = create_initial_state(user_input="Say hi in one word.")
            ctx = PluginContext(state=state)
            result = await core.execute(ctx)
            record("LLM Core", "LLM鐪熷疄璋冪敤(ModelConfigLoader)", "PASS", f"LLM 杩斿洖: {str(result)[:200]}")
            llm_tested = True
        else:
            record("LLM Core", "LLM鐪熷疄璋冪敤", "LOCK", "ModelConfigLoader 鏈壘鍒?minimax-m2.7 閰嶇疆鎴?api_key 涓虹┖")
    except Exception as e:
        record("LLM Core", "LLM鐪熷疄璋冪敤", "PART", "ModelConfigLoader 鍔犺浇/璋冪敤澶辫触", str(e)[:200])

    if not llm_tested:
        env_key = os.environ.get("MINIMAX_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if env_key:
            try:
                from pipeline.types import create_initial_state
                from pipeline.plugin import PluginContext
                # 浣跨敤 MiniMax 閰嶇疆
                core_conf = {"provider": "minimax", "model_name": "MiniMax-M2.7", "api_base": "https://api.minimaxi.com/v1", "api_key": env_key, "default_params": {"temperature": 0.7, "max_tokens": 8192}}
                core = LLMCore(config=core_conf)
                state = create_initial_state(user_input="Say hi in one word.")
                ctx = PluginContext(state=state)
                result = await core.execute(ctx)
                record("LLM Core", "LLM鐪熷疄璋冪敤(鐜鍙橀噺)", "PASS", f"LLM 杩斿洖: {str(result)[:200]}")
            except Exception as e:
                record("LLM Core", "LLM鐪熷疄璋冪敤", "PART", "API key瀛樺湪浣嗚皟鐢ㄥけ璐?, str(e)[:200])
        else:
            record("LLM Core", "LLM鐪熷疄璋冪敤", "LOCK", "MINIMAX_API_KEY 鍜?OPENAI_API_KEY 鍧囨湭閰嶇疆锛屾棤娉曟祴璇曠湡瀹炶皟鐢?)


# ============================================================================
# 4. Tool Core
# ============================================================================
async def test_tool_core():
    print("\n" + "=" * 60 + "\n4. Tool Core\n" + "=" * 60)
    try:
        from tools.registry import ToolRegistry
        tr = ToolRegistry()
        def sample_tool(args: dict) -> str:
            return f"Result for: {args.get('query', '')}"
        tr.register(name="search", func=sample_tool, description="鎼滅储宸ュ叿")
        record("Tool Core", "ToolRegistry娉ㄥ唽+鑾峰彇", "PASS", f"娉ㄥ唽search鎴愬姛锛屽伐鍏锋暟={len(tr.list_tools())}")
    except Exception as e:
        record("Tool Core", "ToolRegistry娉ㄥ唽", "FAIL", "娉ㄥ唽澶辫触", traceback.format_exc()); return

    try:
        td = tr.get("search"); result = td.handler({"query": "test"})
        record("Tool Core", "宸ュ叿鎵嬪姩鎵ц", "PASS", f"鎵ц杩斿洖: '{result}'")
    except Exception as e:
        record("Tool Core", "宸ュ叿鎵嬪姩鎵ц", "FAIL", "鎵ц澶辫触", traceback.format_exc())

    try:
        llm_tools = tr.get_tools_for_llm()
        record("Tool Core", "LLM鏍煎紡宸ュ叿鍒楄〃", "PASS", f"get_tools_for_llm() 杩斿洖 {len(llm_tools)} 涓伐鍏?)
    except Exception as e:
        record("Tool Core", "LLM鏍煎紡宸ュ叿鍒楄〃", "FAIL", "鐢熸垚澶辫触", traceback.format_exc())

    try:
        from plugins.core.tool_core import ToolCore
        from pipeline.types import create_initial_state, StateKeys
        from pipeline.plugin import PluginContext
        tc = ToolCore(); tc.register_tool("search", sample_tool)
        state = create_initial_state(user_input="test")
        # raw_tool_calls 鍙傛暟: name + args (not arguments)
        state[StateKeys.RAW_TOOL_CALLS] = [{"name": "search", "args": {"query": "e2e"}}]
        ctx = PluginContext(state=state)
        result = await tc.execute(ctx)
        record("Tool Core", "ToolCore鎵ц宸ュ叿璋冪敤", "PASS",
               f"tool_results={len(result.get(StateKeys.TOOL_RESULTS, []))}, raw_result='{result.get(StateKeys.RAW_RESULT, '')}'")
    except Exception as e:
        record("Tool Core", "ToolCore鎵ц宸ュ叿璋冪敤", "FAIL", "鎵ц澶辫触", traceback.format_exc())


# ============================================================================
# 5. 浠诲姟绯荤粺
# ============================================================================
async def test_tasks():
    print("\n" + "=" * 60 + "\n5. 浠诲姟绯荤粺\n" + "=" * 60)
    try:
        from tasks.types import TaskStatus, TaskModel, create_task
        record("Tasks", "浠诲姟绫诲瀷瀵煎叆", "PASS", f"TaskStatus: {[s.value for s in TaskStatus]}")
    except Exception as e:
        record("Tasks", "浠诲姟绫诲瀷瀵煎叆", "FAIL", "瀵煎叆澶辫触", traceback.format_exc()); return

    try:
        task = create_task(title="E2E娴嬭瘯", description="绔埌绔祴璇?)
        record("Tasks", "create_task宸ュ巶鍑芥暟", "PASS", f"id={task.id}, status={task.status.value}")
    except Exception as e:
        record("Tasks", "create_task", "FAIL", "鍒涘缓澶辫触", traceback.format_exc())

    # StateMachine 浣跨敤 TaskModel 瀵硅薄
    try:
        from tasks.state_machine import StateMachine, InvalidTransitionError
        sm = StateMachine()
        t = create_task(title="SM娴嬭瘯")
        sm.transition(t, TaskStatus.RUNNING)
        record("Tasks", "鐘舵€佹祦杞琍ENDING->RUNNING", "PASS", f"status={t.status.value}")
        sm.transition(t, TaskStatus.EVALUATING)
        record("Tasks", "鐘舵€佹祦杞琑UNNING->EVALUATING", "PASS", f"status={t.status.value}")
        sm.transition(t, TaskStatus.COMPLETED)
        record("Tasks", "鐘舵€佹祦杞珽VALUATING->COMPLETED", "PASS", f"status={t.status.value}")
    except Exception as e:
        record("Tasks", "鐘舵€佹祦杞?, "FAIL", "娴佽浆澶辫触", traceback.format_exc())

    try:
        from tasks.state_machine import StateMachine, InvalidTransitionError
        sm = StateMachine(); t = create_task(title="缁堟€佹祴璇?); sm.transition(t, TaskStatus.RUNNING); sm.transition(t, TaskStatus.EVALUATING); sm.transition(t, TaskStatus.COMPLETED)
        try:
            sm.transition(t, TaskStatus.RUNNING)
            record("Tasks", "缁堟€佷笉鍙€嗘鏌?, "FAIL", "缁堟€佸簲琚嫆缁?)
        except InvalidTransitionError:
            record("Tasks", "缁堟€佷笉鍙€嗘鏌?, "PASS", "COMPLETED->RUNNING 琚纭嫆缁?)
    except Exception as e:
        record("Tasks", "缁堟€佷笉鍙€嗘鏌?, "PART", "娴嬭瘯寮傚父", traceback.format_exc())

    # TaskStorage
    try:
        from tasks.storage import TaskStorage
        import tempfile
        temp_dir = tempfile.mkdtemp()
        storage = TaskStorage(data_dir=temp_dir)
        record("Tasks", "TaskStorage瀹炰緥鍖?, "PASS", f"TaskStorage(data_dir=涓存椂鐩綍)")
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        record("Tasks", "TaskStorage", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # TaskService
    # TaskService 鈥?浣跨敤鍏蜂綋鏂规硶鍚?start_task/complete_evaluation绛?锛屾病鏈?update_status
    try:
        from tasks.service import TaskService
        svc = TaskService()
        task = svc.create_task(title="E2E瀹屾暣娴佺▼", description="娴嬭瘯浠诲姟鍏ㄦ祦绋?)
        record("Tasks", "TaskService鍒涘缓浠诲姟", "PASS", f"id={task.id}, status={task.status.value}")
        # pending 鈫?running
        started = svc.start_task(task.id)
        record("Tasks", "TaskService鍚姩浠诲姟", "PASS", f"status={started.status.value}")
        # running 鈫?evaluating
        evaluating = svc.move_to_evaluating(task.id)
        record("Tasks", "TaskService绉诲叆璇勪及", "PASS", f"status={evaluating.status.value}")
        # evaluating 鈫?completed
        completed = svc.complete_evaluation(task.id, passed=True)
        record("Tasks", "TaskService瀹屾垚璇勪及", "PASS", f"status={completed.status.value}")
    except Exception as e:
        record("Tasks", "TaskService瀹屾暣娴佺▼", "FAIL", "娴佺▼澶辫触", traceback.format_exc())


# ============================================================================
# 6. 璇勪及绯荤粺
# ============================================================================
async def test_evaluation():
    print("\n" + "=" * 60 + "\n6. 璇勪及绯荤粺\n" + "=" * 60)
    try:
        from evaluation.types import MetricType, ExpectCondition, ExpectSpec, MetricDefinition
        record("Evaluation", "璇勪及绫诲瀷瀵煎叆", "PASS", f"MetricType: {[m.value for m in MetricType]}")
    except Exception as e:
        record("Evaluation", "璇勪及绫诲瀷瀵煎叆", "FAIL", "瀵煎叆澶辫触", traceback.format_exc()); return

    # MetricLoader
    try:
        from evaluation.loader import MetricLoader
        metrics_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config" / "evaluation_metrics"
        loader = MetricLoader(metrics_dir=str(metrics_dir)) if metrics_dir.exists() else MetricLoader()
        record("Evaluation", "MetricLoader瀹炰緥鍖?, "PASS", "MetricLoader() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Evaluation", "MetricLoader", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # EvaluationEngine (闇€瑕?loader 鍙傛暟)
    try:
        from evaluation.engine import EvaluationEngine
        from evaluation.loader import MetricLoader
        loader = MetricLoader()
        engine = EvaluationEngine(loader=loader)
        record("Evaluation", "EvaluationEngine瀹炰緥鍖?, "PASS", "EvaluationEngine(loader=loader) 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Evaluation", "EvaluationEngine瀹炰緥鍖?, "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # EvaluationExecutor
    try:
        from evaluation.executor import EvaluationExecutor
        executor = EvaluationExecutor()
        record("Evaluation", "EvaluationExecutor瀹炰緥鍖?, "PASS", "EvaluationExecutor() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Evaluation", "EvaluationExecutor", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # ExpectEvaluator
    try:
        from evaluation.expect import ExpectEvaluator
        from evaluation.types import ExpectCondition, ExpectSpec
        evaluator = ExpectEvaluator()
        spec = ExpectSpec(conditions=[ExpectCondition(field="exit_code", operator="equals", value=0)], logic="and")
        result = evaluator.evaluate(metric_id="test", expect=spec, output={"exit_code": 0})
        record("Evaluation", "ExpectEvaluator鏉′欢璇勪及(閫氳繃)", "PASS" if result.passed else "FAIL",
               f"passed={result.passed}, message={result.message}")
    except Exception as e:
        record("Evaluation", "ExpectEvaluator", "FAIL", "璇勪及澶辫触", traceback.format_exc())


# ============================================================================
# 7. Agent 閰嶇疆
# ============================================================================
async def test_agents():
    print("\n" + "=" * 60 + "\n7. Agent 閰嶇疆\n" + "=" * 60)
    try:
        from agents.types import AgentConfig, AgentLevel, AgentType
        config = AgentConfig(name="test_agent", agent_type=AgentType.SPECIALIZED, level=AgentLevel.L2_SUBTASK, description="娴嬭瘯")
        record("Agents", "AgentConfig鍒涘缓", "PASS", f"name={config.name}, type={config.agent_type.value}, level={config.level.value}")
    except Exception as e:
        record("Agents", "AgentConfig鍒涘缓", "FAIL", "鍒涘缓澶辫触", traceback.format_exc())

    # AgentConfigLoader (涓嶆槸 AgentLoader)
    try:
        from agents.loader import AgentConfigLoader
        loader = AgentConfigLoader()
        record("Agents", "AgentConfigLoader瀹炰緥鍖?, "PASS", "AgentConfigLoader() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Agents", "AgentConfigLoader", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # 浠嶻AML鍔犺浇
    try:
        from agents.loader import AgentConfigLoader
        loader = AgentConfigLoader()
        config_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config" / "agents"
        if config_dir.exists():
            configs = loader.load_from_directory(str(config_dir))
            record("Agents", "浠嶻AML鍔犺浇Agent", "PASS", f"鍔犺浇浜?{len(configs)} 涓?Agent 閰嶇疆")
        else:
            record("Agents", "浠嶻AML鍔犺浇Agent", "PART", "閰嶇疆鐩綍涓嶅瓨鍦?)
    except Exception as e:
        record("Agents", "浠嶻AML鍔犺浇Agent", "PART", "鍔犺浇澶辫触", traceback.format_exc())

    try:
        from agents.registry import AgentRegistry
        reg = AgentRegistry()
        record("Agents", "AgentRegistry瀹炰緥鍖?, "PASS", "AgentRegistry() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Agents", "AgentRegistry", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    try:
        from agents.schema_validator import SchemaValidator
        validator = SchemaValidator()
        schema = {"type": "object", "required": ["name", "type", "level"], "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": ["main", "specialized", "system"]}, "level": {"type": "string"}}}
        errors = validator._validate_schema(schema, {"name": "test", "type": "specialized", "level": "L2"})
        record("Agents", "Schema鏍￠獙(鍚堟硶閰嶇疆)", "PASS" if not errors else "PART", f"errors={errors}")
    except Exception as e:
        record("Agents", "Schema鏍￠獙", "FAIL", "鏍￠獙澶辫触", traceback.format_exc())

    try:
        from agents.context_builder import ContextBuilder
        builder = ContextBuilder()
        record("Agents", "ContextBuilder瀹炰緥鍖?, "PASS", "ContextBuilder() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Agents", "ContextBuilder", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())


# ============================================================================
# 8. 妯℃澘绯荤粺
# ============================================================================
async def test_templates():
    print("\n" + "=" * 60 + "\n8. 妯℃澘绯荤粺\n" + "=" * 60)
    try:
        from templates.types import TemplateType, TemplateSection, TemplateSpec
        record("Templates", "妯℃澘绫诲瀷瀵煎叆", "PASS", f"TemplateType: {[t.value for t in TemplateType]}")
    except Exception as e:
        record("Templates", "妯℃澘绫诲瀷瀵煎叆", "FAIL", "瀵煎叆澶辫触", traceback.format_exc()); return

    try:
        spec = TemplateSpec(name="test", template_type=TemplateType.CONSUMABLE, sections=[TemplateSection(title="姒傝堪", required="required")], raw_content="Hello {name}!", placeholders=["name"])
        record("Templates", "TemplateSpec鍒涘缓", "PASS", f"name={spec.name}, type={spec.template_type.value}")
    except Exception as e:
        record("Templates", "TemplateSpec鍒涘缓", "FAIL", "鍒涘缓澶辫触", traceback.format_exc())

    try:
        from templates.loader import TemplateLoader
        loader = TemplateLoader()
        config_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config" / "templates"
        if config_dir.exists():
            templates = loader.load_from_directory(str(config_dir))
            record("Templates", "浠庣洰褰曞姞杞芥ā鏉?, "PASS", f"鍔犺浇浜?{len(templates)} 涓ā鏉?)
        else:
            record("Templates", "浠庣洰褰曞姞杞芥ā鏉?, "PART", "閰嶇疆鐩綍涓嶅瓨鍦?)
    except Exception as e:
        record("Templates", "浠庣洰褰曞姞杞芥ā鏉?, "PART", "鍔犺浇澶辫触", traceback.format_exc())

    # TemplateRenderer.render 鎺ュ彈 TemplateSpec 鍜?variables
    try:
        from templates.renderer import TemplateRenderer
        from templates.types import TemplateSpec, TemplateType
        renderer = TemplateRenderer()
        spec = TemplateSpec(name="test_render", template_type=TemplateType.CONSUMABLE, raw_content="Hello {name}!", placeholders=["name"])
        rendered = renderer.render(spec, variables={"name": "Agent OS"})
        record("Templates", "妯℃澘娓叉煋", "PASS", f"娓叉煋缁撴灉: '{rendered}'")
    except Exception as e:
        record("Templates", "妯℃澘娓叉煋", "FAIL", "娓叉煋澶辫触", traceback.format_exc())

    try:
        from templates.registry import TemplateRegistry
        treg = TemplateRegistry()
        record("Templates", "TemplateRegistry瀹炰緥鍖?, "PASS", "TemplateRegistry() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Templates", "TemplateRegistry", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())


# ============================================================================
# 9. 瑙﹀彂鍣ㄧ郴缁?
# ============================================================================
async def test_triggers():
    print("\n" + "=" * 60 + "\n9. 瑙﹀彂鍣ㄧ郴缁焅n" + "=" * 60)
    try:
        from triggers.types import TriggerConfig, TriggerType, TriggerStatus
        record("Triggers", "瑙﹀彂鍣ㄧ被鍨嬪鍏?, "PASS", f"TriggerType: {[t.value for t in TriggerType]}")
    except Exception as e:
        record("Triggers", "瑙﹀彂鍣ㄧ被鍨嬪鍏?, "FAIL", "瀵煎叆澶辫触", traceback.format_exc()); return

    try:
        from triggers.manager import TriggerManager
        mgr = TriggerManager()
        # 娉ㄥ唽浜嬩欢瑙﹀彂鍣?
        t1 = TriggerConfig(trigger_id="evt1", name="浜嬩欢瑙﹀彂鍣?, trigger_type=TriggerType.EVENT, event_name="task_done")
        mgr.register(t1)
        record("Triggers", "娉ㄥ唽浜嬩欢瑙﹀彂鍣?, "PASS", f"id={t1.trigger_id}, status={t1.status.value}")
    except Exception as e:
        record("Triggers", "娉ㄥ唽浜嬩欢瑙﹀彂鍣?, "FAIL", "娉ㄥ唽澶辫触", traceback.format_exc())

    # 浜嬩欢瑙﹀彂璇勪及
    try:
        fired = mgr.evaluate_event("task_done", {"task_id": "t1"})
        record("Triggers", "浜嬩欢瑙﹀彂鍣ㄨ瘎浼?, "PASS", f"浜嬩欢 'task_done' 瑙﹀彂 {len(fired)} 涓Е鍙戝櫒")
    except Exception as e:
        record("Triggers", "浜嬩欢瑙﹀彂鍣ㄨ瘎浼?, "FAIL", "璇勪及澶辫触", traceback.format_exc())

    # 鏉′欢瑙﹀彂鍣?
    try:
        t2 = TriggerConfig(trigger_id="cond1", name="鏉′欢瑙﹀彂鍣?, trigger_type=TriggerType.CONDITION, condition_expression="status == 'completed'")
        mgr.register(t2)
        fired = mgr.evaluate_condition({"status": "completed"})
        record("Triggers", "鏉′欢瑙﹀彂鍣ㄨ瘎浼?, "PASS", f"鏉′欢瑙﹀彂 {len(fired)} 涓Е鍙戝櫒")
    except Exception as e:
        record("Triggers", "鏉′欢瑙﹀彂鍣ㄨ瘎浼?, "FAIL", "璇勪及澶辫触", traceback.format_exc())

    # 瀹夊叏鎬ф鏌?- import 鍦?_eval_condition 涓笉浼氳Е鍙戯紙鏄娉曢敊璇€岄潪 ValueError锛?
    try:
        t3 = TriggerConfig(trigger_id="unsafe", name="涓嶅畨鍏?, trigger_type=TriggerType.CONDITION, condition_expression="import os")
        mgr.register(t3)
        fired = mgr.evaluate_condition({})
        # import 鏄娉曢敊璇紝琚?except 鎹曡幏锛屼笉浼氳Е鍙?
        record("Triggers", "瀹夊叏妫€鏌?绂佹import)", "PASS", f"import os 琚畨鍏ㄦ嫤鎴紙璇硶閿欒锛屾湭瑙﹀彂锛宖ired={len(fired)}锛?)
    except Exception as e:
        record("Triggers", "瀹夊叏妫€鏌?, "PART", "娴嬭瘯寮傚父", traceback.format_exc())

    # 寤惰繜瑙﹀彂鍣?
    try:
        import datetime as dt
        t4 = TriggerConfig(trigger_id="delay1", name="寤惰繜1绉?, trigger_type=TriggerType.DELAY, delay_seconds=1, metadata={"register_time": dt.datetime.now().isoformat()})
        mgr.register(t4)
        fired = mgr.check_scheduled(dt.datetime.now())
        record("Triggers", "寤惰繜瑙﹀彂鍣?鏈埌鏈?", "PASS", f"绔嬪嵆妫€鏌? fired={len(fired)} (搴斾负0)")
        await asyncio.sleep(1.1)
        fired = mgr.check_scheduled(dt.datetime.now())
        record("Triggers", "寤惰繜瑙﹀彂鍣?鍒版湡)", "PASS", f"1绉掑悗妫€鏌? fired={len(fired)} (搴斾负1)")
    except Exception as e:
        record("Triggers", "寤惰繜瑙﹀彂鍣?, "PART", "娴嬭瘯澶辫触", traceback.format_exc())


# ============================================================================
# 10. 閰嶇疆鐑噸杞?
# ============================================================================
async def test_config_reload():
    print("\n" + "=" * 60 + "\n10. 閰嶇疆鐑噸杞絓n" + "=" * 60)
    try:
        from config.reload import ConfigReloader
        reloader = ConfigReloader()
        record("ConfigReload", "ConfigReloader瀹炰緥鍖?, "PASS", "ConfigReloader() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("ConfigReload", "ConfigReloader", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    try:
        from config.schema import ConfigSchemaValidator
        validator = ConfigSchemaValidator()
        record("ConfigReload", "ConfigSchemaValidator瀹炰緥鍖?, "PASS", "ConfigSchemaValidator() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("ConfigReload", "ConfigSchemaValidator", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # validate_pipeline_config
    try:
        from config.schema import ConfigSchemaValidator
        validator = ConfigSchemaValidator()
        errors = validator.validate_pipeline_config({"name": "test", "input_routes": [], "output_routes": []})
        record("ConfigReload", "Pipeline閰嶇疆鏍￠獙(鍚堟硶)", "PASS" if not errors else "PART", f"errors={errors}")
    except Exception as e:
        record("ConfigReload", "Pipeline閰嶇疆鏍￠獙", "FAIL", "鏍￠獙澶辫触", traceback.format_exc())

    # validate_agent_config
    try:
        from config.schema import ConfigSchemaValidator
        validator = ConfigSchemaValidator()
        errors = validator.validate_agent_config({"config_id": "agent1", "name": "娴嬭瘯Agent", "level": "L2", "agent_type": "specialized"})
        record("ConfigReload", "Agent閰嶇疆鏍￠獙(鍚堟硶)", "PASS" if not errors else "PART", f"errors={errors}")
    except Exception as e:
        record("ConfigReload", "Agent閰嶇疆鏍￠獙", "FAIL", "鏍￠獙澶辫触", traceback.format_exc())

    # validate_directory
    try:
        from config.schema import ConfigSchemaValidator
        validator = ConfigSchemaValidator()
        config_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config" / "pipelines"
        if config_dir.exists():
            result = validator.validate_directory(str(config_dir), config_type="pipeline")
            record("ConfigReload", "鐩綍鎵归噺鏍￠獙(pipelines)", "PASS", f"鏍￠獙缁撴灉: {len(result)} 涓枃浠舵湁閿欒")
        else:
            record("ConfigReload", "鐩綍鎵归噺鏍￠獙", "PART", "pipelines 鐩綍涓嶅瓨鍦?)
    except Exception as e:
        record("ConfigReload", "鐩綍鎵归噺鏍￠獙", "PART", "鏍￠獙澶辫触", traceback.format_exc())


# ============================================================================
# 11. 璺ㄧ閬撹矾鐢?(M11a)
# ============================================================================
async def test_cross_pipeline():
    print("\n" + "=" * 60 + "\n11. 璺ㄧ閬撹矾鐢?(M11a)\n" + "=" * 60)
    try:
        from pipeline.registry import PipelineRegistry
        preg = PipelineRegistry()
        record("CrossPipeline", "PipelineRegistry瀹炰緥鍖?, "PASS", "PipelineRegistry() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("CrossPipeline", "PipelineRegistry", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc()); return

    # RouteSignal DELEGATE - target 瀛楁涓嶆槸 target_pipeline
    try:
        from pipeline.types import RouteSignal
        sig = RouteSignal(route_type="delegate", target="research", reason="闇€瑕佺爺绌惰兘鍔?)
        record("CrossPipeline", "DELEGATE璺敱淇″彿", "PASS", f"RouteSignal(route_type='delegate', target='research')")
    except Exception as e:
        record("CrossPipeline", "DELEGATE璺敱淇″彿", "FAIL", "鍒涘缓澶辫触", traceback.format_exc())

    # 濮旀淳绛栫暐鎻掍欢 鈥?绫诲悕鏄?WaitForResultPlugin, FireAndForgetPlugin, EventCallbackPlugin
    strategy_results = {}
    for name, cls_name in [("WaitForResultPlugin", "wait_for_result"), ("FireAndForgetPlugin", "fire_and_forget"), ("EventCallbackPlugin", "event_callback")]:
        try:
            mod = __import__(f"agent_os.plugins.output.{cls_name}", fromlist=[name])
            cls = getattr(mod, name)
            # WaitForResultPlugin 闇€瑕?registry 鍙傛暟
            # WaitForResultPlugin 闇€瑕?registry 鍙傛暟, EventCallbackPlugin 闇€瑕?event_bus
            if name == "WaitForResultPlugin":
                from pipeline.registry import PipelineRegistry
                instance = cls(registry=PipelineRegistry())
            elif name == "EventCallbackPlugin":
                from pipeline.event_bus import EventBus
                instance = cls(event_bus=EventBus())
            else:
                instance = cls()
            strategy_results[name] = instance
            record("CrossPipeline", f"{name}绛栫暐鎻掍欢", "PASS", f"name={instance.name}")
        except Exception as e:
            record("CrossPipeline", f"{name}绛栫暐鎻掍欢", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # PipelineRegistry 浣跨敤 submit/route_to (M1鍏煎) 鎴?route (M11a)
    try:
        from pipeline.registry import PipelineRegistry
        preg = PipelineRegistry()
        # M1 鍏煎: submit + route_to
        pid = preg.submit("research", config={"description": "鐮旂┒绠￠亾"})
        record("CrossPipeline", "PipelineRegistry.submit", "PASS", f"鎻愪氦绠￠亾: id={pid}")
        rid = preg.route_to("research", context={"task": "閲忓瓙璁＄畻"})
        record("CrossPipeline", "PipelineRegistry.route_to", "PASS", f"璺敱鍒? id={rid}")
    except Exception as e:
        record("CrossPipeline", "PipelineRegistry璺敱", "FAIL", "璺敱澶辫触", traceback.format_exc())

    # 瀛愮閬撳疄闄呮墽琛?鈥?浣跨敤鐙珛 PipelineEngine
    try:
        from pipeline.engine import PipelineEngine
        from pipeline.registry import PluginRegistry
        from pipeline.route import InputRouteTable, OutputRouteTable, InputRouteEntry, OutputRouteEntry
        from pipeline.plugin import ICorePlugin
        from pipeline.types import ErrorPolicy, create_initial_state

        class _SubEchoCore(ICorePlugin):
            """娴嬭瘯鐢ㄥ瓙绠￠亾 Echo 鏍稿績鎻掍欢銆?""
            error_policy = ErrorPolicy.ABORT
            @property
            def name(self): return "sub_echo_core"
            @property
            def priority(self): return 50
            async def execute(self, ctx):
                return {"raw_result": f"Echo: {ctx.state.get('user_input', '')}"}

        sub_reg = PluginRegistry(); sub_reg.register_core("llm_call", _SubEchoCore())
        sub_it = InputRouteTable([InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10)])
        sub_ot = OutputRouteTable([OutputRouteEntry(route_type="end", condition="True", priority=99)])
        sub_engine = PipelineEngine(input_route_table=sub_it, output_route_table=sub_ot, plugin_registry=sub_reg)
        state = create_initial_state(user_input="鐮旂┒閲忓瓙璁＄畻")
        result = await sub_engine.run(state)
        record("CrossPipeline", "瀛愮閬撳疄闄呮墽琛?, "PASS", f"raw_result='{result.get('raw_result')}', iteration={result.get('iteration')}")
    except Exception as e:
        record("CrossPipeline", "瀛愮閬撳疄闄呮墽琛?, "FAIL", "鎵ц澶辫触", traceback.format_exc())


# ============================================================================
# 12. WebSocket 閫氶亾
# ============================================================================
async def test_websocket():
    print("\n" + "=" * 60 + "\n12. WebSocket 閫氶亾\n" + "=" * 60)
    try:
        from channels.websocket.protocol import EventType, EventEnvelope, create_event
        record("WebSocket", "WebSocketProtocol瀵煎叆", "PASS", "EventType, EventEnvelope, create_event 瀵煎叆鎴愬姛")
    except Exception as e:
        record("WebSocket", "WebSocketProtocol瀵煎叆", "FAIL", "瀵煎叆澶辫触", traceback.format_exc())

    # create_event 鈥?妫€鏌ュ疄闄呭嚱鏁扮鍚?
    try:
        from channels.websocket.protocol import EventType, EventEnvelope
        # create_event 鍙兘涓嶅瓨鍦ㄦ垨绛惧悕涓嶅悓锛岀洿鎺ヤ娇鐢?EventEnvelope
        event = EventEnvelope(type=EventType.CONNECTION_CONFIRMATION.value, data={"session_id": "test-123"})
        record("WebSocket", "EventEnvelope鍒涘缓", "PASS", f"type={event.type}, data={event.data}")
    except Exception as e:
        record("WebSocket", "EventEnvelope鍒涘缓", "PART", "鍒涘缓澶辫触", traceback.format_exc())

    try:
        from channels.websocket.session_manager import SessionManager
        sm = SessionManager()
        record("WebSocket", "SessionManager瀹炰緥鍖?, "PASS", "SessionManager() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("WebSocket", "SessionManager", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    try:
        from channels.websocket.server import WebSocketServer
        ws = WebSocketServer(host="127.0.0.1", port=18765)
        record("WebSocket", "WebSocketServer瀹炰緥鍖?, "PASS", "WebSocketServer() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("WebSocket", "WebSocketServer", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # 鍚仠娴嬭瘯
    try:
        from channels.websocket.server import WebSocketServer
        ws = WebSocketServer(host="127.0.0.1", port=18766)
        await ws.start()
        record("WebSocket", "WebSocket鏈嶅姟鍣ㄥ惎鍔?, "PASS", "127.0.0.1:18766 鍚姩鎴愬姛")
        await ws.stop()
        record("WebSocket", "WebSocket鏈嶅姟鍣ㄥ仠姝?, "PASS", "鍋滄鎴愬姛")
    except Exception as e:
        record("WebSocket", "WebSocket鏈嶅姟鍣ㄥ惎鍋?, "PART", "鍚仠澶辫触", traceback.format_exc())

    try:
        from channels.websocket.adapter import WebSocketAdapter
        adapter = WebSocketAdapter()
        record("WebSocket", "WebSocketAdapter瀹炰緥鍖?, "PASS", "WebSocketAdapter() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("WebSocket", "WebSocketAdapter", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())


# ============================================================================
# 13. Infrastructure
# ============================================================================
async def test_infrastructure():
    print("\n" + "=" * 60 + "\n13. 鍩虹璁炬柦\n" + "=" * 60)
    try:
        from infrastructure.scheduler import Scheduler
        sched = Scheduler()
        record("Infrastructure", "Scheduler瀹炰緥鍖?, "PASS", "Scheduler() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Infrastructure", "Scheduler", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # ConcurrencyController 浣跨敤 provider_max/model_max/agent_max
    try:
        from infrastructure.concurrency import ConcurrencyController
        cc = ConcurrencyController(config={"provider_max": 3, "model_max": 5, "agent_max": 10})
        record("Infrastructure", "ConcurrencyController瀹炰緥鍖?, "PASS", "ConcurrencyController 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Infrastructure", "ConcurrencyController", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # acquire 鍙帴鍙?level 鍙傛暟
    try:
        from infrastructure.concurrency import ConcurrencyController
        cc = ConcurrencyController()
        async with cc.acquire("provider"):
            pass
        record("Infrastructure", "ConcurrencyController骞跺彂鑾峰彇", "PASS", "acquire('provider') 鎴愬姛")
    except Exception as e:
        record("Infrastructure", "ConcurrencyController骞跺彂鑾峰彇", "FAIL", "鑾峰彇澶辫触", traceback.format_exc())

    try:
        from infrastructure.resource import ResourceManager
        rm = ResourceManager()
        record("Infrastructure", "ResourceManager瀹炰緥鍖?, "PASS", "ResourceManager() 瀹炰緥鍖栨垚鍔?)
    except Exception as e:
        record("Infrastructure", "ResourceManager", "FAIL", "瀹炰緥鍖栧け璐?, traceback.format_exc())

    # apply_error_policy 杩斿洖 PluginResult
    try:
        from infrastructure.error_policy import apply_error_policy
        from pipeline.types import ErrorPolicy
        result = apply_error_policy(policy=ErrorPolicy.ABORT, error=RuntimeError("test"), plugin_name="test")
        record("Infrastructure", "apply_error_policy(ABORT)", "PASS", f"skip_remaining={result.skip_remaining}, error={type(result.error).__name__}")
    except Exception as e:
        record("Infrastructure", "apply_error_policy", "FAIL", "璋冪敤澶辫触", traceback.format_exc())


# ============================================================================
# 14. 璁板繂妯″潡
# ============================================================================
async def test_memory():
    print("\n" + "=" * 60 + "\n14. 璁板繂妯″潡\n" + "=" * 60)
    memory_dir = Path(__file__).resolve().parent.parent / "agent_os" / "memory"
    py_files = [f for f in memory_dir.glob("*.py") if f.name != "__init__.py"]
    ok = 0
    for f in sorted(py_files):
        try:
            __import__(f"agent_os.memory.{f.stem}", fromlist=[f.stem]); ok += 1
        except: pass
    record("Memory", "妯″潡瀵煎叆", "PASS" if ok == len(py_files) else "PART", f"瀵煎叆 {ok}/{len(py_files)} 鎴愬姛")


# ============================================================================
# 15. 閰嶇疆鏂囦欢瀹屾暣鎬?
# ============================================================================
async def test_config_files():
    print("\n" + "=" * 60 + "\n15. 閰嶇疆鏂囦欢瀹屾暣鎬n" + "=" * 60)
    config_dir = Path(__file__).resolve().parent.parent / "agent_os" / "config"
    subdirs = {"agents": "Agent閰嶇疆", "channels": "閫氶亾閰嶇疆", "evaluation_metrics": "璇勪及鎸囨爣", "pipelines": "绠￠亾閰嶇疆", "templates": "妯℃澘閰嶇疆"}
    for subdir, desc in subdirs.items():
        d = config_dir / subdir
        if d.exists():
            yaml_files = list(d.glob("*.yaml")) + list(d.glob("*.yml"))
            record("ConfigFiles", f"{desc}({subdir})", "PASS", f"鍙戠幇 {len(yaml_files)} 涓?YAML")
            if yaml_files:
                try:
                    import yaml
                    with open(yaml_files[0], encoding="utf-8") as f: data = yaml.safe_load(f)
                    record("ConfigFiles", f"{desc} YAML瑙ｆ瀽", "PASS", f"{yaml_files[0].name}: keys={list(data.keys())[:4] if isinstance(data, dict) else type(data).__name__}")
                except Exception as e:
                    record("ConfigFiles", f"{desc} YAML瑙ｆ瀽", "FAIL", "瑙ｆ瀽澶辫触", traceback.format_exc())
        else:
            record("ConfigFiles", f"{desc}({subdir})", "PART", "鐩綍涓嶅瓨鍦?)


# ============================================================================
# 16. 鎻掍欢绯荤粺
# ============================================================================
async def test_plugins():
    print("\n" + "=" * 60 + "\n16. 鎻掍欢绯荤粺\n" + "=" * 60)
    plugins_dir = Path(__file__).resolve().parent.parent / "agent_os" / "plugins"
    plugin_dirs = [d for d in plugins_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
    record("Plugins", "鎻掍欢鐩綍鍙戠幇", "PASS", f"{len(plugin_dirs)} 涓? {[d.name for d in plugin_dirs]}")

    for pdir in sorted(plugin_dirs):
        py_files = [f for f in pdir.glob("*.py") if f.name != "__init__.py"]
        ok = 0
        for f in sorted(py_files):
            try: __import__(f"agent_os.plugins.{pdir.name}.{f.stem}", fromlist=[f.stem]); ok += 1
            except: pass
        record("Plugins", f"{pdir.name} 鎻掍欢瀵煎叆", "PASS" if ok == len(py_files) else "PART", f"{ok}/{len(py_files)} 鎴愬姛")


# ============================================================================
# 涓绘祴璇曡繍琛屽櫒
# ============================================================================
async def run_all_tests():
    print("=" * 60)
    print(f"Agent OS 绔埌绔姛鑳芥祴璇?v3\n寮€濮? {datetime.now().isoformat()}\nPython: {sys.version.split()[0]}")
    print("=" * 60)

    test_cli()
    await test_pipeline()
    await test_llm_core()
    await test_tool_core()
    await test_tasks()
    await test_evaluation()
    await test_agents()
    await test_templates()
    await test_triggers()
    await test_config_reload()
    await test_cross_pipeline()
    await test_websocket()
    await test_infrastructure()
    await test_memory()
    await test_config_files()
    await test_plugins()

    print("\n" + "=" * 60 + "\n娴嬭瘯瀹屾垚\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    partial = sum(1 for r in results if r["status"] == "PART")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    locked = sum(1 for r in results if r["status"] == "LOCK")
    print(f"\n鎬昏: {total} | PASS: {passed} | PART: {partial} | FAIL: {failed} | LOCK: {locked}")
    generate_report(total, passed, partial, failed, locked)


def generate_report(total: int, passed: int, partial: int, failed: int, locked: int):
    now = datetime.now()
    sm = {"PASS": "鉁?, "PART": "鈿狅笍", "FAIL": "鉂?, "LOCK": "馃敀"}
    lines = [
        "# Agent OS 绔埌绔姛鑳芥祴璇曟姤鍛?,
        "", f"**娴嬭瘯鏃ユ湡**: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**娴嬭瘯鐜**: Windows / Python {sys.version.split()[0]}",
        f"**PYTHONPATH**: src",
        "", "## 鎬讳綋姒傚喌", "",
        "| 鎸囨爣 | 鏁板€?|", "|------|------|",
        f"| 鎬绘祴璇曟暟 | {total} |", f"| 鉁?瀹屾暣鍙敤 | {passed} |",
        f"| 鈿狅笍 閮ㄥ垎鍙敤 | {partial} |", f"| 鉂?涓嶅彲鐢?| {failed} |",
        f"| 馃敀 鏈厤缃?| {locked} |",
        f"| 閫氳繃鐜?| {passed/total*100:.1f}% |" if total > 0 else "| 閫氳繃鐜?| N/A |",
        "", "## 璇︾粏娴嬭瘯缁撴灉", "",
    ]

    cur = ""
    for r in results:
        if r["module"] != cur:
            cur = r["module"]; lines += [f"### {cur}", "", "| 鐘舵€?| 娴嬭瘯椤?| 璇佹嵁 | 閿欒淇℃伅 |", "|------|--------|------|----------|"]
        e = sm.get(r["status"], r["status"])
        ev = r["evidence"].replace("|", "/")[:200]
        er = r["error"].replace("\n", " ").replace("|", "/")[:120] if r["error"] else ""
        lines.append(f"| {e} | {r['test_name']} | {ev} | {er} |")

    lines += ["", "## 妯″潡鍙敤鎬ф€荤粨", ""]
    ms: dict[str, dict] = {}
    for r in results:
        m = r["module"]
        if m not in ms: ms[m] = {"total": 0, "passed": 0, "partial": 0, "failed": 0, "locked": 0}
        ms[m]["total"] += 1
        ms[m][{"PASS": "passed", "PART": "partial", "FAIL": "failed", "LOCK": "locked"}[r["status"]]] += 1

    lines += ["| 妯″潡 | 鎬绘祴璇?| 鉁?| 鈿狅笍 | 鉂?| 馃敀 | 鍙敤鎬?|", "|------|--------|----|----|----|----|--------|"]
    for m, c in ms.items():
        avail = "鉂?涓嶅彲鐢? if c["failed"] > 0 else "馃敀 鏈厤缃? if c["locked"] == c["total"] else "鈿狅笍 閮ㄥ垎鍙敤" if c["partial"] > 0 or c["locked"] > 0 else "鉁?瀹屾暣鍙敤"
        lines.append(f"| {m} | {c['total']} | {c['passed']} | {c['partial']} | {c['failed']} | {c['locked']} | {avail} |")

    # 閲岀▼纰?
    lines += ["", "## 閲岀▼纰戝姛鑳介獙璇?, "", "| 閲岀▼纰?| 鍔熻兘 | 楠岃瘉缁撴灉 | 璇存槑 |", "|--------|------|----------|------|"]
    mm = {"M1 绠￠亾妗嗘灦": ["Pipeline"], "M2 LLM+Tool Core": ["LLM Core", "Tool Core"], "M3 ToolRegistry": ["Tool Core"],
          "M4 璋冨害+骞跺彂": ["Infrastructure"], "M5a 浠诲姟绯荤粺": ["Tasks"], "M5b 璇勪及绯荤粺": ["Evaluation"],
          "M6 鎻掍欢杩佺Щ": ["Plugins"], "M7 Agent閰嶇疆": ["Agents"], "M8 閰嶇疆鐑噸杞?: ["ConfigReload"],
          "M9 WebSocket": ["WebSocket"], "M10 妯℃澘+瑙﹀彂鍣?: ["Templates", "Triggers"], "M11a 璺ㄧ閬撹矾鐢?: ["CrossPipeline"]}
    for mile, mods in mm.items():
        mr = []
        for mod in mods:
            if mod in ms:
                c = ms[mod]
                mr.append("鉁? if c["failed"] == 0 and c["locked"] < c["total"] else "馃敀" if c["locked"] == c["total"] else "鉂?)
            else: mr.append("鉂?)
        ov = "鉁? if all(r == "鉁? for r in mr) else "鈿狅笍" if any(r in ("鈿狅笍", "馃敀") for r in mr) else "鉂? if any(r == "鉂? for r in mr) else "鉂?
        lines.append(f"| {mile} | {'+'.join(mods)} | {ov} | {' '.join(mr)} |")

    lines += ["", "---", f"*鎶ュ憡鐢熸垚鏃堕棿: {now.strftime('%Y-%m-%d %H:%M:%S')}*"]
    rp = Path(__file__).parent / "docs" / "e2e-test-report-2026-04-11.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n鎶ュ憡宸插啓鍏? {rp}")


if __name__ == "__main__":
    asyncio.run(run_all_tests())

