#!/usr/bin/env python3
"""日志统一模块功能验证脚本。

验证内容：
1. get_logger() 返回标准 Logger
2. JSON 结构化日志输出字段完整
3. LogContext 上下文注入（trace_id/task_id/pipeline_id/agent_name）
4. scoped() 上下文隔离与恢复
5. LoggingConfig 级别调整
6. 环境变量配置
7. 旧入口转发（monitoring/config）
"""
import sys
import os
import io
import json
import logging

sys.path.insert(0, ".")

from src.core.logging import (
    get_logger,
    setup_logging,
    LoggingConfig,
    LogContext,
    JsonFormatter,
    StructuredFormatter,
)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}: {detail}")
        failures.append(name)


# === 验证1: get_logger + JSON 结构化输出 ===
print("=== 验证1: get_logger + JSON 结构化输出 ===")
log = get_logger("verify.module")
check("get_logger 返回标准 Logger", isinstance(log, logging.Logger))

log_capture = io.StringIO()
handler = logging.StreamHandler(log_capture)
handler.setFormatter(JsonFormatter())
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.INFO)

test_logger = logging.getLogger("json.test")
test_logger.info("JSON格式验证消息")
output = log_capture.getvalue().strip()
data = json.loads(output)
required = ["timestamp", "level", "logger", "message", "module", "function", "line"]
for f in required:
    check(f"JSON 包含字段 {f}", f in data, f"缺失字段 {f}")
check("JSON message 正确", data["message"] == "JSON格式验证消息")
root.removeHandler(handler)

# === 验证2: 上下文注入 ===
print("\n=== 验证2: LogContext 上下文注入 ===")
log_capture2 = io.StringIO()
handler2 = logging.StreamHandler(log_capture2)
handler2.setFormatter(JsonFormatter(context_fields=("trace_id", "task_id", "pipeline_id", "agent_name")))
root.addHandler(handler2)

LogContext.bind(trace_id="trace-abc", task_id="task-001", pipeline_id="pipe-002", agent_name="灵汐")
test_logger.info("带上下文的日志")
data2 = json.loads(log_capture2.getvalue().strip())
check("trace_id 注入", data2["trace_id"] == "trace-abc")
check("task_id 注入", data2["task_id"] == "task-001")
check("pipeline_id 注入", data2["pipeline_id"] == "pipe-002")
check("agent_name 注入", data2["agent_name"] == "灵汐")
LogContext.unbind()
root.removeHandler(handler2)

# === 验证3: scoped 上下文管理器 ===
print("\n=== 验证3: scoped 上下文管理器 ===")
log_capture3 = io.StringIO()
handler3 = logging.StreamHandler(log_capture3)
handler3.setFormatter(JsonFormatter(context_fields=("trace_id",)))
root.addHandler(handler3)

LogContext.bind(trace_id="outer")
with LogContext.scoped(trace_id="inner"):
    test_logger.info("scoped内部")
inner_data = json.loads(log_capture3.getvalue().strip())
check("scoped 内部值正确", inner_data["trace_id"] == "inner")
check("scoped 退出后恢复", LogContext.get("trace_id") == "outer")
root.removeHandler(handler3)
LogContext.unbind()

# === 验证4: 级别调整 ===
print("\n=== 验证4: LoggingConfig 级别调整 ===")
setup_logging(LoggingConfig(output="console", level=logging.DEBUG), reset=True)
check("DEBUG 级别生效", logging.getLogger().level == logging.DEBUG)
setup_logging(LoggingConfig(output="console", level=logging.WARNING), reset=True)
check("WARNING 级别生效", logging.getLogger().level == logging.WARNING)

# === 验证5: 环境变量配置 ===
print("\n=== 验证5: 环境变量配置 ===")
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["LOG_JSON"] = "true"
config = LoggingConfig.from_env()
check("from_env 读取 LOG_LEVEL", config.level == logging.ERROR)
check("from_env 读取 LOG_JSON", config.json_output is True)
del os.environ["LOG_LEVEL"]
del os.environ["LOG_JSON"]

# === 验证6: StructuredFormatter 输出 ===
print("\n=== 验证6: StructuredFormatter 输出 ===")
formatter = StructuredFormatter()
record = logging.LogRecord(
    name="test.module", level=logging.WARNING, pathname="test.py",
    lineno=10, msg="警告消息", args=None, exc_info=None,
)
text_output = formatter.format(record)
check("StructuredFormatter 含级别", "WARNING" in text_output)
check("StructuredFormatter 含模块名", "test.module" in text_output)
check("StructuredFormatter 含消息", "警告消息" in text_output)

# === 验证7: 监控旧入口转发 ===
print("\n=== 验证7: monitoring 旧入口转发到 LogContext ===")
import importlib.util
spec = importlib.util.spec_from_file_location(
    "monitoring_logging_config", "src/monitoring/logging_config.py"
)
mon_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mon_mod)

LogContext.unbind()
mon_mod.set_trace_id("mon-trace-001")
check("set_trace_id 转发到 LogContext", LogContext.get("trace_id") == "mon-trace-001")
check("get_trace_id 返回正确值", mon_mod.get_trace_id() == "mon-trace-001")

mon_mod.set_request_id("mon-req-002")
check("set_request_id 转发到 LogContext", LogContext.get("request_id") == "mon-req-002")
check("get_request_id 返回正确值", mon_mod.get_request_id() == "mon-req-002")

LogContext.unbind()
check("未设置时返回空字符串", mon_mod.get_trace_id() == "")
LogContext.unbind()

# === 结果 ===
print(f"\n{'='*50}")
total = 22
passed = total - len(failures)
print(f"验证结果: {passed}/{total} 通过")
if failures:
    print(f"失败项: {failures}")
    sys.exit(1)
else:
    print("🎉 全部验证通过！")
    sys.exit(0)
