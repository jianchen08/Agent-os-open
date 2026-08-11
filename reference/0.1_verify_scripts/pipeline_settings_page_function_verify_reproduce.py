"""管道配置设置页（PipelineSettingsPage）功能验证 — 可复现验证脚本。

复现 docs/working/pipeline_settings_page_function_verify_report.md 的核心验证动作：

  1. vitest 组件/集成/API 层测试（3 文件 19 用例）：
     - src/pages/settings/__tests__/SettingsPage.test.tsx        （4 用例：场景1 入口/场景2 读取/场景3 修改保存）
     - src/pages/settings/__tests__/PipelineSettingsPage.test.tsx （11 用例：加载/展示/保存/失败/tab/空配置/embedded）
     - src/services/api/__tests__/pipelineConfig.test.ts         （4 用例：GET/PUT 契约）
  2. 静态代码路径核对：
     - router.tsx SETTINGS_PIPELINE 路由注册
     - SettingsPage BUILTIN_ITEMS 管道配置入口 + 内核设置分组 + 内联渲染
     - constants/api.ts PIPELINE_GET/PIPELINE_UPDATE 端点
     - kernel routes.rs P7 端点契约（GET/PUT /api/v1/config/pipelines/{name}）
     - PluginConfigEditor ConfigObject 空对象分支（该配置暂无字段）
     - config/pipelines/ 4 个管道 yaml 存在性
  3. E2E 环境探测（如实报告限制）：
     - kernel/target/release/agentos-kernel 是否存在（本环境缺失 → 真实内核联调不可验证）
     - playwright 浏览器 /opt/ms-playwright 是否存在

前置条件：
  - frontend/node_modules 已安装（vitest 可运行）
  - 工作目录为项目根目录（含 frontend/ 与 kernel/）

用法：
    python3 docs/working/pipeline_settings_page_function_verify_reproduce.py
    # 可选：--no-vitest 跳过测试运行，仅做静态核对

退出码：全部场景 PASS → 0；任一 FAIL → 1

[来源: docs/working/pipeline_settings_page_function_verify_report.md]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(ROOT, "frontend")

# ── 输出工具 ──

PASSED: list[str] = []
FAILED: list[str] = []
WARNED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    (PASSED if ok else FAILED).append(name)
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def warn(name: str, detail: str) -> None:
    WARNED.append(name)
    print(f"[WARN] {name} — {detail}")


def read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


# ── 1. vitest 测试运行 ──

VITEST_FILES = [
    "src/pages/settings/__tests__/SettingsPage.test.tsx",
    "src/pages/settings/__tests__/PipelineSettingsPage.test.tsx",
    "src/services/api/__tests__/pipelineConfig.test.ts",
]


def run_vitest() -> None:
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        warn("vitest 运行", "frontend/node_modules 不存在，跳过测试运行")
        return
    cmd = ["npx", "vitest", "--run"] + VITEST_FILES
    print(">>>", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=FRONTEND, capture_output=True, text=True, timeout=290)
    out = proc.stdout + proc.stderr
    m_files = re.search(r"Test Files\s+(\d+) passed", out)
    m_tests = re.search(r"Tests\s+(\d+) passed", out)
    ok = proc.returncode == 0 and m_tests is not None
    detail = ""
    if m_files and m_tests:
        detail = f"{m_files.group(1)} 文件 {m_tests.group(1)} 用例通过"
    check("vitest 组件/集成/API 测试", ok, detail)
    if not ok:
        print("---- vitest 输出尾部 ----")
        print("\n".join(out.splitlines()[-30:]))


# ── 2. 静态代码路径核对 ──

def static_checks() -> None:
    router = read_file(os.path.join(FRONTEND, "src/router.tsx"))
    settings = read_file(os.path.join(FRONTEND, "src/pages/settings/SettingsPage.tsx"))
    pipeline_page = read_file(os.path.join(FRONTEND, "src/pages/settings/PipelineSettingsPage.tsx"))
    api_const = read_file(os.path.join(FRONTEND, "src/constants/api.ts"))
    routes_const = read_file(os.path.join(FRONTEND, "src/constants/routes.ts"))
    pipeline_api = read_file(os.path.join(FRONTEND, "src/services/api/pipelineConfig.ts"))
    editor = read_file(os.path.join(FRONTEND, "src/components/config/PluginConfigEditor.tsx"))
    kernel_routes = read_file(os.path.join(ROOT, "kernel/crates/api/src/routes.rs"))

    # 场景4：路由注册
    check(
        "场景4 路由：/settings/pipeline 注册",
        "SETTINGS_PIPELINE" in router
        and "PipelineSettingsPage" in router
        and "path: ROUTES.SETTINGS_PIPELINE" in router,
    )
    check(
        "场景4 路由：routes.ts 常量",
        "SETTINGS_PIPELINE: '/settings/pipeline'" in routes_const,
    )
    check(
        "场景4 独立模式：← 返回设置 头",
        "← 返回设置" in pipeline_page and "<h1" in pipeline_page,
    )

    # 场景1：入口注册
    check(
        "场景1 入口：BUILTIN_ITEMS 含管道配置",
        "id: 'pipeline'" in settings and "title: '管道配置'" in settings,
    )
    check(
        "场景1 入口：内核设置分组 + 内联渲染",
        "内核设置" in settings
        and "PipelineSettingsPage embedded" in settings,
    )

    # 场景5：tabs
    check(
        "场景5 tabs：default/l1-main/l2-evaluator/l2-subtask",
        all(
            t in pipeline_page
            for t in ("'default'", "'l1-main'", "'l2-evaluator'", "'l2-subtask'")
        ),
    )

    # API 契约
    check(
        "API 常量：PIPELINE_GET/PIPELINE_UPDATE",
        "PIPELINE_GET" in api_const
        and "PIPELINE_UPDATE" in api_const
        and "/api/v1/config/pipelines/" in api_const,
    )
    check(
        "API 层：GET 返回 {name,data,etag} / PUT body {data}",
        "PipelineConfigResponse" in pipeline_api
        and "PipelineConfigSaveResult" in pipeline_api
        and "{ data }" in pipeline_api,
    )
    check(
        "内核 P7 端点：GET/PUT /api/v1/config/pipelines/{name}",
        "get_pipeline_config_handler" in kernel_routes
        and "put_pipeline_config_handler" in kernel_routes
        and "PipelineConfigResponse" in kernel_routes
        and "PipelineConfigUpdateRequest" in kernel_routes,
    )
    check(
        "内核 P7 端点：响应体 name/data/etag + PUT 返回 {name,etag}",
        "pub data: serde_json::Value" in kernel_routes
        and "pub etag: String" in kernel_routes
        and '"etag": new_etag' in kernel_routes,
    )

    # 场景6：空配置分支
    check(
        "场景6 空配置：ConfigObject 该配置暂无字段",
        "该配置暂无字段" in editor,
    )
    check(
        "场景6 加载失败：无法加载配置 提示",
        "无法加载配置" in pipeline_page,
    )

    # 管道 yaml 存在性
    pipelines_dir = os.path.join(ROOT, "config", "pipelines")
    yamls = ["default.yaml", "l1-main.yaml", "l2-evaluator.yaml", "l2-subtask.yaml"]
    missing = [y for y in yamls if not os.path.isfile(os.path.join(pipelines_dir, y))]
    check("config/pipelines/ 4 个管道 yaml 存在", not missing, f"缺失: {missing}" if missing else "全部存在")


# ── 3. E2E 环境探测（如实报告限制） ──

def env_probe() -> None:
    kernel_bin = os.path.join(ROOT, "kernel/target/release/agentos-kernel")
    if os.path.isfile(kernel_bin):
        check("内核二进制存在（可做真实联调）", True)
    else:
        warn("真实内核联调", f"{kernel_bin} 不存在（任务描述称已构建，实际缺失）→ 真实内核端点验证不可验证")
    pw = "/opt/ms-playwright"
    if os.path.isdir(pw):
        check("playwright 浏览器缓存存在", True, os.listdir(pw))
    else:
        warn("playwright 浏览器", f"{pw} 不存在 → 浏览器 E2E 不可验证")


def main() -> int:
    parser = argparse.ArgumentParser(description="PipelineSettingsPage 功能验证可复现脚本")
    parser.add_argument("--no-vitest", action="store_true", help="跳过 vitest 测试运行，仅做静态核对")
    args = parser.parse_args()

    print("=" * 60)
    print("PipelineSettingsPage 功能验证复现")
    print("=" * 60)

    if not args.no_vitest:
        run_vitest()
    else:
        warn("vitest 测试", "已通过 --no-vitest 跳过（静态核对继续）")

    static_checks()
    env_probe()

    print()
    print("=" * 60)
    print(f"结果汇总：PASS {len(PASSED)} / FAIL {len(FAILED)} / WARN {len(WARNED)}")
    if WARNED:
        print("警告（环境限制，非代码缺陷）：")
        for w in WARNED:
            print(f"  - {w}")
    print("=" * 60)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
