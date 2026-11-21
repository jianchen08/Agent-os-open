#!/usr/bin/env python
"""统一机械门禁入口（quality-gates）：每个可机械检查的承诺都有非零退出命令。

原则（移植自 DSH quality-gates / coverage-exempt-heavy-suites 决策，
落地说明见 docs/working/机械门禁统一入口与覆盖率豁免.md）：

1. **单一事实源**——所有门禁的命令在本文件定义一次；CI job 与本地开发者
   调用同一入口（--mode / --filter），不存在第二套口径。
2. **CI 跑穷尽集**；Git hooks / 本地只留廉价检查（--mode fast）。
3. **非零退出**——任何门禁失败、或因依赖失败被跳过 → 退出码 1
   （观察型 allow_failure 门禁除外）。
4. **有界并行**——按依赖图调度，AGENTOS_GATE_CONCURRENCY 控制上限；
   默认 cap 4（cargo/npm/pytest 同跑防内存爆）。

覆盖率豁免：plugins-coverage（插桩，阈值守护）与 plugins-heavy（免插桩，
94 插件子进程冒烟矩阵）拆两个并行 gate，全部测试仍执行——名单与配对
校验见 scripts/coverage_exempt.py（单一名单点）。

用法：
    python scripts/run_gates.py --list                    # 列出全部门禁
    python scripts/run_gates.py --mode fast               # 本地廉价检查
    python scripts/run_gates.py --mode all                # 穷尽集
    python scripts/run_gates.py --mode kernel|plugins|frontend
    python scripts/run_gates.py --filter kernel-fmt,plugins-coverage   # CI job 精确选择

环境变量：
    AGENTOS_GATE_CONCURRENCY=N   并行上限（默认 min(CPU, 4)）
    AGENTOS_GATE_VERBOSE=1       打印通过门禁的完整输出（CI 建议开）

Python 侧环境前置（与 CI 各 job 的 setup 一致，本地跑一次即可）：
    uv sync --frozen --extra dev --extra isolation
    uv pip install -e ./plugins/sdk
前端 / Electron：frontend/ 与仓库根各自 npm ci（electron 免下载二进制：
ELECTRON_SKIP_BINARY_DOWNLOAD=1）。
Rust 覆盖率门禁需 cargo-llvm-cov（cargo install cargo-llvm-cov 或
taiki-e/install-action）。

门禁归属（非本入口持有、但同为非零退出命令的承诺）：
    pre-commit   → .github/workflows/ci.yml pre-commit job（pre-commit/action）
    python-e2e   → .github/workflows/e2e.yml Python E2E job（需内核二进制；
                   stub LLM 聊天轮 keyless 必跑，真实 LLM 用例有 key 才跑，
                   nightly/手动/push 触发，CI 专用车道）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: 本次 run 的临时目录命名空间（并发 run 互不干扰；清理只清自己）。
_RUN_ID = uuid.uuid4().hex[:12]
_GATE_TMP_ROOT = Path(".zctmp") / "gates"


def _run_tmp_dir() -> Path:
    """本 run 的门禁临时目录（相对仓库根；run id 为十六进制，无空格）。"""
    return _GATE_TMP_ROOT / _RUN_ID


def _cleanup_gate_tmp() -> None:
    """清理本 run 的门禁输出临时目录（并发 run 各自独享，不整树删除）。

    多会话/提交钩子并发跑 gate 时，整树删 ``.zctmp/gates`` 会把其它在跑 gate
    的 tee 文件一并删掉——长车道（plugins-coverage ~27min）收尾读
    ``--from-file`` 时 FileNotFoundError 误红（2026-11-04 实测两轮）。
    """
    run_dir = _run_tmp_dir()
    shutil.rmtree(run_dir, ignore_errors=True)
    parent = run_dir.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


import coverage_exempt  # noqa: E402 —— 豁免名单单一名单点，防两侧漂移

# 本地易用性：存在 .venv 时自动前置到 PATH（ruff/mypy/pytest 等门禁依赖）。
# CI 各 job 无 .venv（依赖由 setup 步骤装入系统 Python）→ 本行为无操作。
_VENV_BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
if _VENV_BIN.is_dir():
    os.environ["PATH"] = str(_VENV_BIN) + os.pathsep + os.environ.get("PATH", "")

# ── 公共环境 ──────────────────────────────────────────────────────────
# 与原 ci.yml 一致：插件运行时依赖 plugins（平铺模块）+ SDK 源码。
_PYTHONPATH = os.pathsep.join([str(ROOT / "plugins"), str(ROOT / "plugins" / "sdk" / "src")])
_PLUGINS_ENV = {"PYTHONPATH": _PYTHONPATH}


@dataclass(frozen=True)
class Gate:
    """一个机械门禁：命令 + 依赖元数据。

    command 与 shell 二选一：优先 argv 形态（无 shell、跨平台）；
    需要管道 / 后台进程的门禁（tee 捕获 + 基线锁、preview 服务）用
    bash -c 形态。
    """

    id: str
    label: str
    domain: str  # kernel | plugins | frontend | electron | cross
    command: tuple[str, ...] = ()
    shell: str = ""
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    fast: bool = False  # 廉价本地检查（--mode fast / Git hooks）
    allow_failure: bool = False  # 观察型：失败不阻塞
    # ── 接线元数据（元门禁 gate-wiring V1 消费；单一真值=定义处，见
    #    docs/working/企业级成熟度整改执行方案_20260924.md 批次 M）──
    # ci=被某 workflow --filter 引用（默认）；inline-ci=CI 内联实现；
    # observation=观察型；local=仅本地 mode（过渡态）。非 ci 必须给理由。
    wiring: str = "ci"
    wiring_reason: str = ""
    # fast 档估时（秒；V5 预算校验：所有 fast 门禁总和 ≤ FAST_BUDGET）。
    # fast=True 必须声明，防"未估时重门禁溜进 fast 档"的漏洞。
    est_seconds: int | None = None
    # 变更模块过滤守护路径（方案 AI辅助编程体系落地AgentOS_20261022 §3.6
    # 步骤 5）：该门禁守护的仓库相对路径前缀。空 = cross-cutting 恒跑；
    # 非空 = 仅 --changed-files 过滤语境下，至少一个变更文件命中前缀才跑。
    # 保守原则：只在归属 100% 确定时标注，不确定的留空恒跑（恒跑 = 保守
    # 方向，多跑不漏检）。
    paths: tuple[str, ...] = ()


def _shell_join_pytest(args: list[str]) -> str:
    """把 pytest argv 拼成 shell 字符串；-m 的 marker 表达式含空格须加引号。"""
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-m" and i + 1 < len(args):
            out.append('-m "' + args[i + 1] + '"')
            i += 2
        else:
            out.append(args[i])
            i += 1
    return " ".join(out)


GATES: list[Gate] = [
    # ── 内核（Rust，kernel/）──────────────────────────────────────────
    Gate(
        id="kernel-fmt",
        label="cargo fmt --check",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "fmt", "--all", "--", "--check"),
        est_seconds=5,
        fast=True,
        paths=("kernel/",),
    ),
    Gate(
        # too-many-lines(阈值 200, kernel/clippy.toml)挂入执法（2026-10-12
        # 文件规模棘轮，ADR 2026-10-12-file-length-mechanical-gate.md）：
        # 函数体 >200 行即红；存量 6 处以 #[allow]+归因注释吸收，只减不增。
        # unreachable-pub + redundant_pub_crate（2026-11-17 可见性面执法）：
        # "pub 该私有"的两条机械线（crate 根不可达 pub / 私有 mod 内冗余
        # pub(crate)）；"形式可达但零外部使用"类不在编译器执法域（dead-code
        # 明文豁免 exported），公开面快照审查见
        # ADR 2026-11-17-kernel-env-key-registry-gate.md 被否方案节。
        id="kernel-clippy",
        label="cargo clippy -D warnings",
        domain="kernel",
        cwd="kernel",
        command=(
            "cargo", "clippy", "--all-targets", "--",
            "-D", "warnings",
            "-D", "clippy::too-many-lines",
            "-D", "unreachable-pub",
            "-D", "clippy::redundant_pub_crate",
        ),
        # 串行到 kernel-test 之后：两条 cargo 车道并发 + 门禁捕获管道 +
        # kernel-test 内嵌 cdylib 构建，曾合取成整树挂死（2026-11-09 打包链
        # 三轮实锤，冻结点在 native 测试的内层 cargo）。clippy 本就被
        # workspace build 锁挡住全程空转，串行化零墙钟代价。依赖指向选中集
        # 外合法（validate_graph：CI job 层承担）。
        needs=("kernel-test",),
    ),
    Gate(
        id="kernel-build",
        label="cargo build (debug)",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "build", "--verbose"),
    ),
    Gate(
        id="kernel-build-release",
        label="cargo build (release)",
        domain="kernel",
        cwd="kernel",
        command=("cargo", "build", "--release", "--verbose"),
        needs=("kernel-build",),  # 同一 target 目录，串行避免 cargo 锁空转
    ),
    Gate(
        id="kernel-test",
        label="cargo test + 失败数基线锁（只减不增）",
        domain="kernel",
        cwd="kernel",
        shell=(
            # 文件重定向而非 `| tee`：测试二进制（含 native 测试的内嵌 cargo）
            # 继承管道写端，活流无人消费（Python 在门禁结束后才取整段输出，
            # 基线检查读 $T），管道层是纯故障面（2026-11-09 挂死合取项之一）。
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); cargo test --all > "$T" 2>&1; '
            'python ../scripts/check_rust_test_baseline.py --from-file "$T"'
        ),
        # needs+=timing-gate：时序 lane 与重型 cargo 车道并行会被线程饥饿打死
        # （2026-10-30 打包链两轮实锤）——timing 声明「独立阻塞」即须独占窗口。
        needs=("kernel-build", "timing-gate"),
    ),
    Gate(
        id="kernel-coverage",
        label="cargo llvm-cov line% 基线锁（只升不降）",
        domain="kernel",
        cwd="kernel",
        shell=(
            "cargo llvm-cov --workspace --exclude agentos-integration-tests "
            # bin 覆盖、不进本口径（下方 kernel-diff-coverage 注释同款 scope 声明）——
            # bin 迁至 crates/api/src/bin 后被单测口径重复收编（1279 缺行全是
            # main()/进程启动面，单测结构性不可达），按既定 scope 摘出。
            # 该参数 cargo llvm-cov 只接受一次，不可重复传。
            "--ignore-filename-regex 'bin[\\\\/]agentos-kernel\\.rs$' "
            "--lcov --output-path coverage.lcov --ignore-run-fail "
            # D1 拍板（2026-09-24）：执法线降至 90.0（Windows 实测 92.51），
            # --skip 摘除恢复执法；内核目标线 100% 见 rust-coverage-baseline.txt
            "&& python ../scripts/check_rust_coverage_baseline.py --lcov coverage.lcov"
        ),
        needs=("timing-gate",),
    ),
    Gate(
        # Rust 改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。scope 限
        # kernel/crates（库 crate，由各自单测驱动）；kernel/src 主进程 bin
        # 由 e2e 车道覆盖、build.rs 编译期运行不进 llvm-cov 口径，均不在 scope。
        id="kernel-diff-coverage",
        label="Rust 改动行覆盖率 100%（diff coverage）",
        domain="kernel",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "kernel/coverage.lcov",
            "--format",
            "lcov",
            "--scope",
            "kernel/crates",
            "--ext",
            ".rs",
            "--omit",
            r"build\.rs$",
            # scope 对齐（2026-09-16）：主进程 bin 由 e2e 车道覆盖（见上方
            # kernel-coverage 注释），不进本 gate 的改动行口径。
            "--omit",
            r"^kernel/crates/api/src/bin/",
            # 集成测试文件（crates/*/tests/）不在 llvm-cov lcov 度量面
            # （cargo-llvm-cov 只归属被测源码行），改动行口径同样排除——
            # 与 plugins-diff-coverage 的 /tests/ omit 同构；缺本 omit 时
            # tip 提交一触碰测试文件即"度量面漂移"假红。
            "--omit",
            r"^kernel/crates/[^/]+/tests/",
            # 嵌套测试模块（crates/*/src/**/tests/*.rs，#[cfg(test)] mod）同不在
            # 逐行度量面：llvm-cov 对纯测试源文件只出 FN/FNDA 记录、无 SF/DA 行
            # 数据（2026-10-29 实测 api ws_session/tests/cancel_poisoning.rs，
            # FN 8 条 / SF 0 条），触碰即"度量面漂移"假红——与上方 tests/ omit
            # 同理同构（测试代码不进改动行覆盖率口径）。
            "--omit",
            r"^kernel/crates/[^/]+/src/.*/tests/",
            # 基准文件（crates/*/benches/，criterion harness=false）同不在
            # 度量面：cargo llvm-cov 底层跑 cargo test，默认目标集不含
            # bench（harness=false 不构建进测试集），lcov 无其行记录——
            # 与上方 tests/ omit 同理，触碰即"度量面漂移"假红。
            "--omit",
            r"^kernel/crates/[^/]+/benches/",
        ),
        needs=("kernel-coverage",),
    ),
    Gate(
        # 内核 env 旋钮登记门禁（ADR 2026-11-17-kernel-env-key-registry-gate）：
        # kernel/crates 读的每个 AGENTOS_* 键必须在 config/kernel/env_keys.yaml
        # 登记（description 必填），防"逃生口存在但无人知晓"（run_chain 闸门
        # 上限 8 案例：env 覆盖存在三处先例引用，运维文档零登记）。检查器
        # 双向 fail-closed：未登记键红 + 索引过期条目红 + 空描述红。stdlib-only
        # （rust-lint job 裸 Python 3.11 可跑，无 PyYAML 依赖）。
        id="kernel-env-registry",
        label="env 键登记索引（AGENTOS_* 读取面 ⇆ env_keys.yaml 双向棘轮）",
        domain="kernel",
        command=(sys.executable, "scripts/check_env_key_registry.py"),
        # 不进 fast 档：V5 预算 144s 已顶格（gate-wiring 实证 146>144），
        # 本闸实跑 ~0.5s 但预算无余量，由 CI filter + --mode all 承载。
        paths=("kernel/", "config/kernel/env_keys.yaml", "scripts/check_env_key_registry.py"),
    ),
    Gate(
        # 编译产物增量回收：cargo 只增不删旧 fingerprint 副本（源码/依赖一变
        # 就整套新建，旧副本永不命中也永不回收，实测一个月可堆 ~30G 死重）。
        # per-crate hash 簇判定：同 crate 最新副本永远保留（增量加速所需），
        # 旧副本冷置满 3 天回收；单副本 crate 不动（lockfile 未变的老依赖仍
        # 是活缓存）。被占用条目跳过留待下轮，恒 exit 0，故为观察型门禁。
        # needs=kernel-build：kernel 域任何编译车道（build/test/coverage）跑完
        # 都触发；与 test/coverage 并行安全——本轮新产物 mtime 新、必属最新簇。
        # 启动链路（start_web_02.bat）另有 release 域的后台回收挂点。
        id="kernel-artifact-sweep",
        label="Rust 旧编译产物回收（--older-than 3）",
        domain="kernel",
        command=(
            sys.executable,
            "scripts/clean_rust_debug.py",
            "--older-than",
            "3",
        ),
        needs=("kernel-build",),
        allow_failure=True,
        wiring="observation",
        wiring_reason="观察型回收（allow_failure 恒 exit 0 不阻塞），设计如此",
    ),
    Gate(
        # Rust 依赖安全审计门禁（rust_dependency_review 报告 §二 #1）：RUSTSEC
        # 公告 + licenses 白名单 + bans 多版本 + sources 外源，配置 kernel/deny.toml
        # （workspace 根）。存量 advisory 基线在 deny.toml [advisories] ignore 逐条
        # 显式 allow + reason（不允许静默忽略），随对应治理项清理后移除。
        # 版本注意：钉 0.20.2（CI 的 taiki-e/install-action 预编译产物，本地
        # 同版本）。0.20.x 不能从源码编译（需 rustc 1.88+，本仓钉 1.85）；
        # 0.18.3 的 rustsec 无法解析 advisory-db 新增 CVSS 4.0 条目，已弃用。
        id="rust-deny",
        label="cargo deny check（RUSTSEC 公告 + licenses + bans + sources）",
        domain="kernel",
        cwd="kernel",
        # 网络韧性：advisory-db fetch 走 GitHub，直连间歇 reset（2026-10-30
        # 打包链两轮实锤）——重试两次后末次 --offline 兜底（本地库 fresh 度
        # 由最近一次成功 fetch 承载，advisories 判定语义不变；全离线环境
        # 下等价于按最近库执行，真实红仍会红，只兜网络抖）。
        command=(
            "bash",
            "-c",
            (
                "cargo deny check "
                "|| (sleep 10 && cargo deny check) "
                "|| (sleep 30 && cargo deny check) "
                "|| (sleep 20 && cargo deny --offline check)"
            ),
        ),
    ),
    Gate(
        # 性能指标面契约门禁（性能监测补强 R1 O2，真值 docs/working/
        # 性能监测补强_20261028.md §四）：scratch 真内核拉起（spawn_test_kernel
        # 三隔离）→ 登录取 token → /metrics 断言常用性能指标齐活 + 无 token
        # 被拒。指标清单单一真值在检查器 EXPECTED_METRICS（与观测看板、真值
        # 文档三方对齐）。needs=kernel-build-release：resolve_kernel_binary
        # release 优先于 debug——只链 debug 会烟测到陈旧 release 产物（首跑
        # 实证：release 为 T5 落地前产物，kernel_proc_* 全缺）。CI job 用
        # --filter 链式同跑，本地 mode all/kernel 经依赖图先建后烟测；二进制
        # 缺失红（fail-loud 执法面，非 timing 车道的 skipif 观察语义）。
        id="kernel-metrics-smoke",
        label="真内核拉起 /metrics 常用性能指标契约（scratch 三隔离 + 登录取 token）",
        domain="kernel",
        command=(sys.executable, "scripts/check_kernel_metrics_smoke.py"),
        needs=("kernel-build-release",),
    ),
    Gate(
        # 通道基准观察（批次 5）：既有 #[ignore] 手写基准的门禁化登记。
        # wiring=local：基准数值受机器负载影响不设红线；转 CI 观察态需
        # release 编译预算评估（正式 criterion 化另行立项）。
        id="kernel-bench",
        label="通道基准观察（native+serde --ignored，时序参考）",
        domain="kernel",
        cwd="kernel",
        shell=(
            "cargo test -p agentos-invoker --test tool_core_bench bench_native -- --ignored --nocapture && "
            "cargo test -p agentos-invoker --test tool_core_bench bench_serde -- --ignored --nocapture"
        ),
        allow_failure=True,
        wiring="local",
        wiring_reason="批次5 观察态：基准数值负载敏感不设红线；CI 化需 release 编译预算评估",
    ),
    # ── 插件 SDK（plugins/sdk，独立包）────────────────────────────────
    Gate(
        id="sdk-lint",
        label="SDK ruff",
        domain="plugins",
        cwd="plugins/sdk",
        command=("ruff", "check", "."),
        est_seconds=3,
        fast=True,
        paths=("plugins/sdk/",),
    ),
    Gate(
        id="sdk-mypy",
        label="SDK mypy",
        domain="plugins",
        cwd="plugins/sdk",
        command=("mypy", "src/agentos_plugin_sdk"),
        est_seconds=30,
        fast=True,
        paths=("plugins/sdk/",),
    ),
    Gate(
        id="sdk-test",
        label="SDK pytest",
        domain="plugins",
        cwd="plugins/sdk",
        command=("pytest", "-v"),
    ),
    # ── 插件体系（Python，仓库根，uv.lock 锁定环境）────────────────────
    Gate(
        id="plugins-coverage-pairing",
        label="豁免名单配对校验（--ignore ≡ positional）",
        domain="plugins",
        command=(sys.executable, "scripts/coverage_exempt.py", "--check"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # C1 渠道合流防复发守卫（2026-08-20）：channel_common 模块名黑名单
        # 不得重回任何 channel_* 目录；负样本演示见
        # docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §1.3 第 6 步执行记录。
        id="channel-copy-guard",
        label="渠道共享拷贝守卫（channel_common 黑名单 0 命中）",
        domain="plugins",
        command=(sys.executable, "scripts/check_channel_copy_guard.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 工具契约完备度闸（批次 4-2，评估 P1-3）：声明即注册须带
        # output_schema + render 的机械执法；存量已于批次 4-1 全部补齐，
        # 基线 0/0——新声明漏键即红。
        id="tool-contract-completeness",
        label="工具契约完备度（output_schema + render 缺项基线锁）",
        domain="plugins",
        command=(sys.executable, "scripts/check_tool_contract_completeness.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # config/ 静态校验闸（批次 4-4，盲区审计）：yaml 可解析 + 管道步骤
        # 引用存在性——把"内核启动才炸"的配置错前置到 PR 阶段。
        id="config-static",
        label="config 静态校验（yaml 可解析 + 管道引用存在）",
        domain="plugins",
        command=(sys.executable, "scripts/check_config_static.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 出厂播种版本闸（ADR 2026-10-24-config-seed-version-arbitration +
        # ADR 2026-11-09-seed-version-content-assets）：受管出厂默认（含
        # config/plugins 安全/隔离策略、config/tools 工具声明）必须声明版本 +
        # 内容变更必抬版本；模式种子包（plugins/shared/mode_*）内容变更必抬
        # plugin.json version——治「用户空间陈旧副本遮蔽包内修复」（2026-10-24
        # 评估通过空转事故、2026-11-07 bash 更名 mode agent 未抬版本事故）。
        # 纯 git+正则，零第三方依赖。
        id="config-seed-ver",
        label="出厂播种版本（受管默认 + 模式种子：声明 + 变更必抬版本）",
        domain="plugins",
        command=(sys.executable, "scripts/check_config_seed_versions.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 插件分级清单一致性对账闸（ADR 2026-11-07-plugin-tiered-single-copy-model
        # 刀 4）：Tier1 清单（config/kernel/plugin_tiers.yaml）↔ plugins/shared
        # 两面对账——清单条目悬空=红；`_` 前缀共享件新增忘登记=红。接线挂 ci.yml
        # 恒跑 job（traceability-gate，无 if:）：plugin_tiers.yaml 属 config/ 面，
        # 纯 config 变更不点亮 python_full 车道，恒跑是「清单变更必过闸」的唯一
        # 落点（ci.yml 注释同源）。非 fast：fast 预算满额（140/140），本闸由
        # 恒跑 job 执法，不占 fast 档。
        id="plugin-tiers",
        label="插件分级清单一致性（Tier1 清单 ↔ plugins/shared 两面对账）",
        domain="plugins",
        command=(sys.executable, "scripts/check_plugin_tiers_consistency.py"),
        paths=("config/kernel/plugin_tiers.yaml", "plugins/shared/"),
    ),
    Gate(
        # 关键链 venv 健康闸（2026-10-23 用户令「打包 CI 之前必须保证所有插件
        # 关键链路完整、能够运行、至少一轮 LLM」）：「.venv 目录存在」≠「可用」
        # ——llm_core 陈旧 editable 实证（在场 spawn 即 ModuleNotFoundError，
        # 主管道 LLM 死循环退避）。check-only fail-closed；判据零复制复用
        # ensure_plugin_venvs。venv 缺席默认跳过（CI 冷检出形态），漂移在场即红。
        id="plugin-venv-health",
        label="关键链 venv 健康（解释器在+SDK 可 import，打包前置）",
        domain="plugins",
        command=(sys.executable, "scripts/check_plugin_venv_health.py"),
        est_seconds=60,
    ),
    Gate(
        # 步骤声明↔注册 parity 闸（2026-10-10 CI 防再犯清单 C 项）：
        # 异名 steps 条目必须 @plugin.step 注册（否则调用即 STEP_NOT_FOUND
        # fail-closed 潜伏雷）、已注册异名必须回写 manifest（③级索引仅聚合
        # manifests，漏声明=死注册）；自名条目（name==插件 id）豁免。
        # 非 fast：检查器逐插件 import 注册面实测 ~200s（首跑实证），进 fast
        # 档会撑破 V5 预算（93s>90s）且估时严重失真。
        id="step-declaration-parity",
        label="步骤声明↔注册 parity（STEP_NOT_FOUND 潜伏雷静态清零）",
        domain="plugins",
        command=(sys.executable, "scripts/check_step_declaration_parity.py"),
        est_seconds=200,
    ),
    Gate(
        # 配置直读机械闸（ADR 2026-09-14 §2.6）：插件代码禁止直读 config/，
        # 唯一合法入口 = manifest config_files 声明 + 内核注入。2026-09-24
        # 复活——此前从未注册车道且基线键为机器绝对路径（与相对 found 键
        # 永不命中=从未绿过且无人知，元门禁 V7 的原型猎物）；基线已重建为
        # 仓库相对 posix 键（93 文件/267 处棘轮），检查器对绝对键 fail-closed。
        id="config-direct-reads",
        label="配置直读守卫（config/ 路径字面量基线锁，只减不增）",
        domain="plugins",
        command=(sys.executable, "scripts/check_config_direct_reads.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # state.reads 声明完备性机械闸（ADR 2026-11-12-plugin-state-reads-
        # declaration-gate）：内核按 manifest 声明切片投喂，未声明键插件恒
        # 读不到且无日志——duplicate_check 漏 task.id（失败归因静默不写）、
        # task_reminder 漏 reminder_seen_result_fp（5 条任务误判
        # reminder_exhausted）两起事故都是纯静态可判的漏声明。扫描
        # plugins/shared/*/plugin.py 三形态读点，控制键白名单直接解析 Rust
        # 真源，基线双向棘轮（新增=红 + 条目失效=红）。
        id="state-reads-declaration",
        label="state.reads 声明守卫（切片投喂漏声明，基线双向棘轮）",
        domain="plugins",
        command=(sys.executable, "scripts/check_state_reads_declaration.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 内核库直连机械闸（审查总册 20261014 C2 建闸，2026-10-15 刀3 收紧
        # 终态口径）：插件禁止自开 sqlite3 连接读内核库——唯一合法入口 =
        # 内核 db-read 契约服务（HTTP POST /api/v1/db/read 或 capability 面，
        # 桥在 plugins/shared/db_read.py）。口径 = 内核库直连零 + 自有库显式
        # 豁免名单（检查器 EXEMPT_FILES 带归因），测试文件豁免；基线只容
        # 在途欠账（sync 三处，迁移后清偿归零），新增直连即红。
        id="kernel-db-direct-reads",
        label="内核库直连守卫（直连零 + 自有库豁免名单，基线只减不增）",
        domain="plugins",
        command=(sys.executable, "scripts/check_kernel_db_direct_reads.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # 门禁基建自身的 lint（执行方案 0-6）：run_gates/检查器族 33 处存量
        # 入基线棘轮，新增即红。通用检查器 check_ruff_baseline.py 与
        # plugins-lint（批次 2）共用。
        id="scripts-lint",
        label="scripts/ ruff 基线锁（只减不增）",
        domain="cross",
        shell=(
            # 提交面评估（ADR 2026-11-07-gate-evaluation-face-commit-scope）：未跟踪
            # 与未暂存改动的平行会话 WIP 不计入基线，暂存/提交即入索引面无逃逸口
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( python scripts/ruff_commit_face.py scripts/ 2>&1 || true ) | tee "$T"; '
            'python scripts/check_ruff_baseline.py --name scripts --from-file "$T"'
        ),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 插件主体面 lint 真空收口（执行方案批次 2，评估 P0-2）：plugins/
        # 19.1 万行 + mcp-servers/（0-6 摘除历史排除）共 1590 处存量入基线
        # 棘轮，新增即红。类型面已有 plugins-mypy-baseline 执法，本 gate 只
        # 补 ruff 风格/卫生面。不进 fast 档（全量 ruff 约 40s，会破 V5 预算）。
        id="plugins-lint",
        label="插件主体 ruff 基线锁（plugins/ + mcp-servers/，只减不增）",
        domain="plugins",
        shell=(
            # 提交面评估（ADR 2026-11-07-gate-evaluation-face-commit-scope），同 scripts-lint
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( python scripts/ruff_commit_face.py plugins/ mcp-servers/ 2>&1 || true ) | tee "$T"; '
            'python scripts/check_ruff_baseline.py --name plugins --from-file "$T"'
        ),
    ),
    Gate(
        id="plugins-coverage",
        label="插件测试（插桩，免豁免重型套件）+ 失败数基线锁 + 覆盖率基线锁",
        domain="plugins",
        shell=(
            "D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( uv run --frozen python -m pytest -v "
            + _shell_join_pytest(coverage_exempt.instrumented_args())
            + " --cov=plugins --cov-report=term-missing --cov-report=xml:coverage.xml"
            + ' 2>&1 || true ) | tee "$T"; '
            'python scripts/check_pytest_failure_baseline.py --lane plugins-coverage --from-file "$T" '
            # D1 拍板（2026-09-24）：整体基线 100.00→90.0 并摘除 --skip 恢复执法
            # （插桩基集实测 ~98.5 绿）；旧挂起理由（Windows/Linux 恒差 vs 100
            # 压力线）随 90 目标口径失效。
            "&& python scripts/check_python_coverage_baseline.py"
        ),
        env=_PLUGINS_ENV,
        needs=("timing-gate",),
    ),
    Gate(
        # Python 改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。omit 与
        # pyproject [tool.coverage.run] omit 对齐（tests 自身/sdk/__init__ 不度量）。
        id="plugins-diff-coverage",
        label="Python 改动行覆盖率 100%（diff coverage）",
        domain="plugins",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "coverage.xml",
            "--format",
            "xml",
            "--scope",
            "plugins",
            "--ext",
            ".py",
            "--omit",
            "^plugins/sdk/",
            "--omit",
            "/tests/",
            "--omit",
            r"/test_[^/]+\.py$",
            "--omit",
            r"/__init__\.py$",
        ),
        needs=("plugins-coverage",),
        env=_PLUGINS_ENV,
    ),
    Gate(
        # 渠道三件 per-file 100%（DSH 口径，2026-08-26 覆盖率裁决；批F-1 接线）：
        # wecom/qq/dingtalk 每个源文件行+函数+分支三维全绿或显式豁免，
        # 消费 plugins-coverage 产出的 coverage.xml（tests/channels 在插桩基集内）。
        id="channel-per-file-coverage",
        label="渠道三件 per-file 覆盖率 100%（行+函数+分支，豁免可见）",
        domain="plugins",
        command=(
            sys.executable,
            "scripts/check_channel_per_file_coverage.py",
            "--check",
        ),
        needs=("plugins-coverage",),
    ),
    Gate(
        # 运行面产物新鲜（2026-11-09 过程发现）：内核 NativePluginLoader 加载的是
        # 插件目录根那份 cdylib——它必须既不早于 native-sdk（ABI 同源），也不早于
        # 插件自身源码（改了不重编 = 跑旧逻辑）。旧口径只比 native-sdk，实测
        # tool_core 改了 messages.rs 后本闸仍报 OK、实际跑的是旧产物。
        # 首次 clone 无产物时本闸报红，提示 --build；CI 在本 job 先建产物再跑。
        id="native-artifacts",
        label="native cdylib 与源码同源（运行面产物新旧）",
        domain="plugins",
        command=(sys.executable, "scripts/check_native_artifacts_sync.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        id="plugins-heavy",
        label="重型套件免插桩（94 插件子进程冒烟矩阵）+ 失败数基线锁",
        domain="plugins",
        shell=(
            "D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( uv run --frozen python -m pytest -v "
            + _shell_join_pytest(coverage_exempt.heavy_args())
            + ' 2>&1 || true ) | tee "$T"; '
            'python scripts/check_pytest_failure_baseline.py --lane plugins-heavy --from-file "$T"'
        ),
        env=_PLUGINS_ENV,
        needs=("timing-gate",),
    ),
    Gate(
        id="plugins-mypy-baseline",
        label="plugins/shared mypy 基线锁（只减不增）",
        domain="plugins",
        command=("uv", "run", "--frozen", "python", "scripts/check_mypy_baseline.py"),
    ),
    Gate(
        # 变更关联测试车道（2026-09-04 CI 分档）：非核心插件/配置/测试面变更
        # 只跑受影响插件的单元+集成测试；全量插桩/冒烟矩阵保留给核心面
        # （kernel + pipeline + SDK）。变更面解析契约与 check_diff_coverage.py
        # 一致（--base > GITHUB_BASE_REF > push 最后提交），CI 由 changes job
        # 的 related 输出决定是否挂本门禁；无关联命中放行，失败即红（无基线
        # 锁——新车道只验增量，失败基线仍由全量车道持有）。
        id="plugins-related-tests",
        label="变更关联测试（非核心插件/配置 → 对应单元+集成测试）",
        domain="plugins",
        command=("uv", "run", "--frozen", "python", "scripts/ci_related_tests.py"),
        env=_PLUGINS_ENV,
    ),
    Gate(
        # 按 marker 全量收集（-m timing tests/）：全仓所有 @pytest.mark.timing
        # 用例唯一归本闸，普通车道（plugins-coverage/heavy 经 MARKER_FILTER）
        # 与 e2e 车道（e2e.yml）以 -m "not timing" 排除，避免时序用例重复跑。
        id="timing-gate",
        label="时序不变量（-m timing，独立阻塞）",
        domain="plugins",
        command=(
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "pytest",
            "-m",
            "timing",
            "tests/",
        ),
        env=_PLUGINS_ENV,
    ),
    # ── 前端（frontend/）──────────────────────────────────────────────
    Gate(
        id="frontend-typecheck",
        label="tsc --noEmit",
        domain="frontend",
        cwd="frontend",
        command=("npm", "run", "typecheck"),
        # 2026-09-24 实测校准：本地/CI 实跑 ~14s（原估 25 虚高，V5 预算超限复核触发）
        est_seconds=15,
        fast=True,
    ),
    Gate(
        # 主题色卫生（2026-10-25 用户裁定：文字/图标/背景一律随主题变量，
        # 生产代码与插件声明模板禁硬编码颜色字面量；存量债基线棘轮只减不增）
        id="frontend-theme-color-hygiene",
        label="主题色卫生：hex/rgb()/hsl() 字面量扫描 + 基线棘轮（定义层豁免）",
        domain="frontend",
        command=(sys.executable, "scripts/check_theme_color_hygiene.py", "--baseline-monotonic"),
        est_seconds=2,
        fast=True,
        paths=("frontend/",),
    ),
    Gate(
        # 主题对比度静态核算（2026-11-07 用户裁定：渲染面文字/图标与实际背景
        # 必须有 WCAG 对比度差别——静态计算含 alpha 透明合成，不跑浏览器）。
        # 主题 token 对（预设+皮肤）+ 插件 web/webview 资产逐主题实例化两面；
        # 存量低对比对进 theme-contrast-baseline 棘轮（新增/恶化/待收紧即红）。
        # 不入 fast 档（预算满额）；paths 含 plugins/——资产面变更本地可触达。
        id="frontend-theme-contrast",
        label="主题对比度：WCAG 静态核算（token 对 + 资产实例化，alpha 合成，基线棘轮）",
        domain="frontend",
        command=(sys.executable, "scripts/check_theme_contrast.py", "--baseline-monotonic"),
        paths=("frontend/", "plugins/"),
    ),
    Gate(
        id="frontend-lint",
        label="eslint + 基线锁（只减不增）",
        domain="frontend",
        cwd="frontend",
        # ESLINT_EXIT=<code> 末行标记：eslint 零违规时无输出，基线解析器靠
        # 该标记区分"干净通过"与"eslint 崩溃/配置错"（2026-11-16 修复
        # fail-open 假绿）。PIPESTATUS[0] 取 npm 真实退出码（tee 恒 0 不算）；
        # 改标记协议须同步 check_frontend_baseline.parse_eslint_errors。
        shell=(
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( npm run lint 2>&1 ) | tee "$T"; '
            'echo "ESLINT_EXIT=${PIPESTATUS[0]}" >>"$T"; '
            'python ../scripts/check_frontend_baseline.py --eslint-file "$T"'
        ),
    ),
    Gate(
        id="frontend-coverage",
        label="vitest 覆盖率%基线锁 + 失败数基线锁（只减不增/只升不降）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( npm run test:coverage 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_frontend_baseline.py --vitest-file "$T"'
        ),
    ),
    Gate(
        # 前端改动行 100%（ADR 2026-08-20 覆盖率棘轮门禁）。omit 与
        # frontend/vitest.config.ts coverage.exclude 对齐（测试自身/main.tsx/
        # d.ts 不度量）。lcov 产物 = frontend/coverage/lcov.info。
        id="frontend-diff-coverage",
        label="前端改动行覆盖率 100%（diff coverage）",
        domain="frontend",
        command=(
            sys.executable,
            "scripts/check_diff_coverage.py",
            "--coverage-file",
            "frontend/coverage/lcov.info",
            "--format",
            "lcov",
            "--scope",
            "frontend/src",
            "--ext",
            ".ts",
            "--ext",
            ".tsx",
            "--omit",
            "/__tests__/",
            "--omit",
            r"\.test\.(ts|tsx)$",
            "--omit",
            "^frontend/src/test/",
            "--omit",
            r"\.d\.ts$",
            "--omit",
            r"^frontend/src/main\.tsx$",
        ),
        needs=("frontend-coverage",),
    ),
    Gate(
        id="frontend-any-baseline",
        label="any 基线棘轮（只减不增）+ >1000 行文件冻结",
        domain="frontend",
        # 纯扫描零依赖（批次F 2026-09-08）：any 存量消化另行立项，本闸防增；
        # 巨型文件拆分裁定为单独立项分批（第三/四轮扫描），冻结基线即管控机制。
        command=(sys.executable, "scripts/check_frontend_any_baseline.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # knip/jscpd 基线棘轮（R6v2-close 2026-09-13）：解析工具 stdout 对照
        # .github/knip-jscpd-baseline.txt 只减不增。调用口径 = npm exec
        # （node_modules/.bin 直调；工具链为 Windows 侧 pnpm 安装，
        # WSL native binding 不匹配不可用，与 vite 同口径）。
        id="frontend-knip-baseline",
        label="knip 未用导出/依赖基线棘轮（只减不增）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( npm exec -- knip --no-progress 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_knip_jscpd_baseline.py --knip-file "$T"'
        ),
        wiring="local",
        wiring_reason="批次0-1 过渡：Linux/Windows 计数一致性验证后接 ci.yml（执行方案 09-24）",
    ),
    Gate(
        id="frontend-jscpd-baseline",
        label="jscpd 克隆数基线棘轮（只减不增）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( npm exec -- jscpd 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_knip_jscpd_baseline.py --jscpd-file "$T"'
        ),
        wiring="local",
        wiring_reason="批次0-1 过渡：Linux/Windows 计数一致性验证后接 ci.yml（执行方案 09-24）",
    ),
    Gate(
        id="frontend-e2e-smoke",
        label="playwright 冒烟（vite preview + ci-smoke + design_tokens，零后端依赖）",
        domain="frontend",
        cwd="frontend",
        shell=(
            "npm run build && {\n"
            # preview 后台进程必须重定向脱离 stdout 管道：kill npm 不一定杀透
            # node 层，残留 node 持有管道会让 run_gate 的 subprocess.run 等
            # EOF 永久阻塞（CI 实证挂死 80min+，2026-09-01）。
            # Windows（Git Bash）下 kill npm 收不到 node 孙进程，残留监听会让
            # 下轮 strictPort 起不来——起服前按端口清残留（仅 MINGW/MSYS 分支，
            # Linux CI 无此问题走原路径）。
            '  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*)\n'
            "    for pid in $(netstat -ano | grep ':5188' | grep -i listen | awk '{print $NF}' | sort -u); do\n"
            "      taskkill //F //PID $pid >/dev/null 2>&1 || true\n"
            "    done ;;\n"
            "  esac\n"
            "  npm run preview -- --port 5188 --strictPort >/dev/null 2>&1 & PREVIEW_PID=$!\n"
            "  for i in $(seq 1 30); do curl -sf http://localhost:5188 >/dev/null && break; sleep 2; done\n"
            "  if ! curl -sf http://localhost:5188 >/dev/null; then\n"
            '    echo "preview 服务未就绪"; kill $PREVIEW_PID || true; exit 1\n'
            "  fi\n"
            "  npx playwright test e2e/specs/ci-smoke.spec.ts e2e/specs/design_tokens.spec.ts\n"
            "  RC=$?\n"
            "  kill $PREVIEW_PID || true\n"
            '  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*)\n'
            "    for pid in $(netstat -ano | grep ':5188' | grep -i listen | awk '{print $NF}' | sort -u); do\n"
            "      taskkill //F //PID $pid >/dev/null 2>&1 || true\n"
            "    done ;;\n"
            "  esac\n"
            "  exit $RC\n"
            "}"
        ),
        wiring="inline-ci",
        wiring_reason="CI 内联实现（ci.yml frontend-e2e 直排 steps）：run_gates 一次性 subprocess 捕获会让挂死阶段零日志（2026-09-01 实证 80min+），执法未缺失",
    ),
    Gate(
        # 前端产物体积闸（批次 5，评估 R5/P1-2）：vendor chunk 字节基线锁 +
        # 无效动态导入计数锁。链在 frontend-e2e-smoke 之后（needs）复用其
        # build 产物；不进 fast 档（build 约 50s）。
        id="bundle-size",
        label="前端产物体积（vendor 字节 + 无效动态导入计数，只减不增）",
        domain="frontend",
        cwd="frontend",
        shell=(
            'D=.zctmp/gates/$AGENTOS_GATE_RUN_ID; mkdir -p $D; T=$(mktemp $D/gate_tmp.XXXXXXXXXX); ( npm run build 2>&1 || true ) | tee "$T"; '
            'python ../scripts/check_bundle_size.py --from-file "$T"'
        ),
        needs=("frontend-e2e-smoke",),
    ),
    # ── Electron 桌面壳（electron/，root npm 环境）────────────────────
    Gate(
        id="electron-compile",
        label="Electron 主进程 tsc 编译（electron:compile）",
        domain="electron",
        command=("npm", "run", "electron:compile"),
        est_seconds=5,
        fast=True,
        paths=("electron/",),
    ),
    Gate(
        # electron/__tests__ 23 文件 289 用例（主进程模块：app-protocol CSP/
        # kernel-manager/auth-session/dialog/edit-menu/visibility-recovery 等）
        # ——2026-09-24 前零车道接线（盲区审计发现），本 gate 补齐。运行口径 =
        # frontend 的 vitest 二进制 + --root ../electron（electron 无独立
        # node_modules/配置，与 09-16 审查批手动验证口径一致）。
        id="electron-test",
        label="Electron 主进程 vitest（__tests__ 289 用例）",
        domain="electron",
        cwd="frontend",
        command=("npm", "exec", "--", "vitest", "run", "--root", "../electron"),
    ),
    Gate(
        # 安装器用户数据/凭据保留闸（ADR 2026-11-08-installer-credential-retention，
        # 2026-11-08 用户令「加个 CI 搞定它」）：三轮「升级即重登」实证的机械
        # 封口——安装器不得删口令播种（NSIS 升级先跑旧卸载器，删除=升级轮换
        # 口令=内核重置+全令牌作废）、不得 RMDir $APPDATA、nsis 配置不得
        # deleteAppDataOnUninstall；播种「已存在则保留」守卫正面断言防回潮。
        # 2026-11-12 用户令「安装不要用管理员账户」：perMachine 必须
        # false/缺省（true=RequestExecutionLevel admin + Program Files）。
        # 挂 ci.yml electron job --filter。非 fast：fast 预算满额（140/140，
        # plugin-tiers 先例），本闸由 electron job 恒跑执法，不占 fast 档。
        id="installer-userdata-guard",
        label="安装器数据/凭据保留 + 免管理员安装（播种不可删 + APPDATA 不可清 + perMachine=false）",
        domain="electron",
        command=(sys.executable, "scripts/check_installer_userdata_guard.py"),
        paths=("electron/nsis/", "electron/plugin-tier2-provision.ts", "package.json"),
    ),
    # ── 横切（仓库级）────────────────────────────────────────────────
    Gate(
        id="tdd-gate",
        label="TDD 合规（变更必有测试）",
        domain="cross",
        command=(sys.executable, "scripts/check_tdd_compliance.py"),
    ),
    Gate(
        # 基线总表一致性闸（批次 6-2）：生成物 vs .github 基线真值逐字节比对，
        # 手改生成物或基线变更未回写即红（根治活文档手抄数字漂移）。
        id="doc-baseline-sync",
        label="基线总表一致性（生成物 = .github 真值）",
        domain="cross",
        command=(sys.executable, "scripts/gen_baseline_table.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 活文档路径存在性闸（批次 6-3）：照着活文档必须能找到文件
        # （评估 §2.4 四处错位的机械化防复发）。历史记录层不回溯执法。
        id="doc-paths",
        label="活文档路径存在性（19 篇，引用全部可解析）",
        domain="cross",
        command=(sys.executable, "scripts/check_doc_paths.py"),
        est_seconds=2,
        fast=True,
        paths=("docs/",),
    ),
    Gate(
        # 端口同步闸（全仓独立审查整改方案 §3-K4）：electron 侧
        # KERNEL_DEFAULT_PORT 与前端打包件 PACKAGED_KERNEL_WS_ORIGIN 是两处
        # 平行簿记（无生成物/共享常量，曾因端口不一致导致打包件 WS 连接
        # 失败），本闸机械执法双值相等；抽取失败（文件/符号改名）同样红
        # ——fail-closed 防闸退化成永绿假闸。
        id="kernel-port-sync",
        label="内核端口同步（electron KERNEL_DEFAULT_PORT ≡ 前端 PACKAGED_KERNEL_WS_ORIGIN）",
        domain="cross",
        command=(sys.executable, "scripts/check_kernel_port_sync.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 前端插件页声明投影同步闸（审查总册 E6/P2-26）：contributes.pages
        # 生成投影 pluginPages.generated.ts 唯一来源（E6 落地）。
        id="frontend-plugin-pages-sync",
        label="前端插件页声明投影同步（gen_frontend_plugin_pages 重渲染逐字节比对）",
        domain="frontend",
        command=(sys.executable, "scripts/check_frontend_plugin_pages_sync.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # 共享根裸名导入禁区闸（审查总册 E2/P1-10）：共享根 28 件经
        # _root_api/ 显式出口消费（E2 落地），裸名 import 回流即红。
        id="shared-root-imports",
        label="共享根裸名导入禁区（_root_api 出口唯一消费面）",
        domain="plugins",
        command=(sys.executable, "scripts/check_shared_root_imports.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # 工具契约静态闸（2026-10-16 工具调用形态修复 ADR 执法面）：自由
        # object 字段必须表态（additionalProperties / 非空 description），
        # 同名工具同名字段跨插件声明须全等，manifest input_schema 与代码
        # 内联 @plugin.tool schema 须全等（G2 注册闸 verify.rs 全等比对，
        # 双源漂移=工具被拒注册）。plugin_check 安装期委托同一检查器。
        id="tool-schema-contract",
        label="工具 schema 契约（自由 object 表态 / 同名同形 / 双源一致）",
        domain="plugins",
        command=(sys.executable, "scripts/check_tool_schema_contracts.py"),
        est_seconds=6,
        fast=True,
    ),
    Gate(
        # agent tool_ids 静态闸（2026-10-18 tool_ids 漂移归因刀）：config/agents/**
        # 每条 tool_ids ⊆ 仓内 manifest 注册面 ∪ 整包插件 id ∪ 外部/用户空间提供方
        # 清单（单源 = tool_schema.EXTERNAL_PROVIDED_TOOL_IDS）。引用面外 id 的
        # agent 每轮 LLM 调用必败且只留运行期缩面痕迹（2026-10-17 日志 72 行/日
        # 同类实证），本闸在静态期拦下。测试面并有同契约
        # tests/plugins/test_tool_ids_contract.py。
        id="agent-tool-ids",
        label="agent tool_ids 白名单（注册面 ∪ 外部清单；缺注册即红）",
        domain="plugins",
        command=(sys.executable, "scripts/check_agent_tool_ids.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # grants 工具分级覆盖门禁（P1.5，ADR 2026-11-19-grants-ledger-and-tool-
        # classification D2「未分类 deny」的 CI 兜底）：LLM 可见工具面（manifests
        # 声明 ∩ tool_ids 白名单 + mode.yaml inject_tool_ids + call_tool/
        # dsh.tool.call 动态可达目标 = 全部声明工具）必须被
        # config/plugins/grants/tool_policy.yaml 全覆盖，缺口即红。键模型镜像
        # plugins/shared/grants/policy.py 查表序（精确 tool.action → 整工具 →
        # tool.* 通配）；多动作工具逐 action、整工具键仅限单动作、通配须 notes
        # 登记理由；verdict/effect 闭集单源 = grants/policy.py。
        # 非 fast：fast 预算满额（同 plugin-tiers 先例），CI 由独立恒跑 job 执法。
        # 当前态：P1 并行精化策略表——其补表提交落地前，对已提交版策略表缺口集
        # 非空（红 = 预期态），P1 落地即绿；缺口报告 = P1 补表输入（--report）。
        id="tool-policy-coverage",
        label="tool_policy 覆盖（LLM 工具面 ⇆ grants 分级表，缺口即红）",
        domain="plugins",
        command=(sys.executable, "scripts/check_tool_policy_coverage.py"),
        paths=(
            "plugins/shared/",
            "config/agents/",
            "config/plugins/grants/",
            "scripts/check_tool_policy_coverage.py",
        ),
    ),
    Gate(
        # 服务声明静态闸（资源注册收敛 T5.3，方案 §二 闭包第一层）：插件生产码
        # 的 get_capability / 句柄 .call 字面调用 ns ⊄ requires_services ∪
        # KERNEL_PROVIDED_SERVICES（registry.rs 表为单源，正则解析 fail-closed）
        # 即红；共享库（channel_common 等）调用点按 import 闭包归消费插件；
        # 声明无调用点（平台依赖豁免除外）→ 警告不阻断。动态拼名识别不到的
        # 边界由运行时 capability router unresolved 对账兜底（T5.5）。
        id="service-declaration",
        label="服务声明闭包（调用点 ⊆ 声明 ∪ 内建表；共享库按 import 归属）",
        domain="plugins",
        command=(sys.executable, "scripts/check_service_declarations.py"),
        est_seconds=8,
        fast=True,
    ),
    Gate(
        # render.card 卡型词汇表同步闸（审查总册 AR-M5/P1-20）：卡型词表真值
        # = 插件侧声明面（plugins/shared/*/plugin.json render.card +
        # dsh_adapter/translator.py DSH 卡型映射），生成物
        # frontend/src/utils/renderCardVocabulary.generated.ts 是前端类型面
        # 与声明校验的唯一词表来源（原 renderIntent.ts 两处手抄清单已删）。
        # 执法核在生成器 --check（重渲染逐字节比对）：手改生成物 / 插件侧
        # 增删卡型未再生成任一即红——fail-closed（单侧注入假卡型未再生成闸
        # 红；再生成则 tsc 在 payload 构造器缺位处红，双向封死）。
        id="render-card-sync",
        label="卡型词汇表同步（插件侧声明面 ≡ 前端生成物）",
        domain="cross",
        command=(sys.executable, "scripts/check_render_card_vocabulary_sync.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 移动壳 apiBase 漂移闸（审查总册 P1-14②，D1①②）：capacitor.config.json
        # plugins.ShellConfig.apiBase 是壳基址声明单点，syncweb 生成 www/shell-config.js
        # + 注入 index.html 标签；env 注入值（LINGXI_API_BASE，本机内核构建缝双
        # 模式）残留在库即成漂移（127.0.0.1:9300 过期 dev 值实锤）。执法核在
        # syncweb --check（生成器单源渲染逻辑零复制），声明面缺失/生成物漂移/
        # 标签缺失任一即红——fail-closed。运行时免疫在壳内（MainActivity 换装的
        # ShellConfigWebViewClient 用声明面/sidecar 现做现答），两道防线分工见
        # ShellConfigResolver。
        id="shell-config-sync",
        label="移动壳 apiBase 同步（capacitor 声明面 ≡ syncweb 生成物 ≡ 注入标签）",
        domain="cross",
        command=(sys.executable, "scripts/check_shell_config_sync.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # OpenAPI 说明书漂移闸（整改方案 §6 企业级补课第 1 项）：
        # scripts/gen_openapi.py 从 server.rs/http_dispatcher.rs 路由注册面
        # 生成 docs/api/openapi.yaml（OpenAPI 3.1，外部工具可生成客户端/
        # 导入 Postman/网关对接）；--check 重生成逐字节比对——改路由忘了
        # 再生成即红。security 字段复用 P-2 鉴权分类单一真值源（AUTH_*
        # 常量组 + classify_auth_surface，ADR 2026-10-10 枚举键锚定复用）；
        # /ext 插件动态声明面不收录（info.description 注明经 /api/v1/schema
        # 运行时发现）。解析退化（源缺失/常量抽不到/路由面条数过少/路径
        # 未落入鉴权面）同样红——fail-closed 防永绿假闸。
        id="openapi-drift",
        label="OpenAPI 说明书漂移（docs/api/openapi.yaml ≡ 路由注册面再生成的）",
        domain="cross",
        command=(sys.executable, "scripts/gen_openapi.py", "--check"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        id="traceability-gate",
        label="@feature 追溯闭环（非法标记硬失败 + 未标记基线锁）",
        domain="cross",
        command=(sys.executable, "scripts/check_test_traceability.py"),
        est_seconds=5,
        fast=True,
    ),
    Gate(
        # 文件长度机械闸（ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）：
        # 新/存分档四档——新文件 1500/3000 硬（无基线可入）、存量未入册触线
        # 2500/5000、冻结件天花板只减不增、软提醒 1000/2000 报表（2026-10-12
        # 三次裁定：初拟统一 1500 → 统一 2500 → 新/存分档，见 ADR）
        # + 触碰即拆第五维（2026-10-15 增补）：触碰拆分线 >5000（非测试/
        # 测试同线；2500~5000 灰带不设闸）文件——含冻结件——被本次 diff
        # 触碰即红，须同刀拆分或缩行；触碰范围解析复用
        # check_diff_coverage 基底机制（--touch-range > GITHUB_BASE_REF
        # merge-base > HEAD^..HEAD），逃生口 [skip-touch-split]。
        # CI 侧 GITHUB_EVENT_BEFORE 供新文件判定、GITHUB_BASE_REF 供触碰
        # 判定（checkout 需 fetch-depth: 0），--baseline-monotonic 执法账本棘轮。
        # invoker.rs 8 月底 4215→2708 后 6 周长回 4780（约 345 行/周）实证
        # "只拆不闸"不可持续，闸与拆必须配套。
        id="file-length-baseline",
        label="文件长度新/存分档闸（新1500/3000 硬 + 存量触线2500/5000 + 基线棘轮 + 触碰即拆）",
        domain="cross",
        command=(sys.executable, "scripts/check_file_length_baseline.py", "--baseline-monotonic"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 观测配方契约门禁（性能监测补强 R1 O1/O2）：deploy/observability 的
        # 抓取/鉴权/回环绑定/看板指标名/令牌不入库 五面契约，被改动时不得
        # 静默漂移。stdlib-only（消费方 traceability-gate 恒跑 job 是裸
        # setup-python，无 PyYAML），YAML 走行级断言、JSON 走真解析。
        # 不入 fast 档：fast 预算满额（2026-10-28 并行在飞闸已超），CI 由
        # traceability-gate 恒跑 job 承载，本地 --mode all 可跑。
        id="observability-config",
        label="观测配方契约（Prometheus 抓取/Grafana provisioning/看板指标/令牌不入库）",
        domain="cross",
        command=(sys.executable, "scripts/check_observability_config.py"),
        paths=("deploy/observability/",),
    ),
    Gate(
        # async 内同步阻塞门禁（真源 docs/working/fs_tools同族阻塞_同类审计与
        # 修复方案_20261116.md；ADR docs/decisions/2026-11-16-async-blocking-gate.md）：
        # AST 扫描 plugins/shared + plugins/sdk/src 全部 async def 函数体内的
        # 同步文件/进程 I/O（async-def-with-sync-body 是 SDK _invoke_callable
        # 同步函数下放线程池的盲区——装机 watchdog 停滞自杀实证根因面）。
        # 基线棘轮 .github/async-blocking-baseline.txt（逐项 file::func::call::line，
        # 匹配键 = 前 3 段）：不在基线内的新增违规红；P0/P1 五文件随修复任务
        # 收缩为零残留。不入 fast 档：fast 预算满额 144/144（同 observability-config
        # 先例），CI 由 traceability-gate 恒跑 job 承载；本地 --mode all / --filter 可跑。
        id="async-blocking",
        label="async 内同步阻塞 AST 扫描（新增违规红 + 基线棘轮只减不增）",
        domain="cross",
        command=(sys.executable, "scripts/check_async_blocking.py", "--baseline-monotonic"),
        est_seconds=3,
    ),
    Gate(
        # Electron 主/渲染进程导入边界闸（ADR 2026-11-16-lint-error-ratchet-
        # electron-boundary-gate）：electron/** ↔ frontend/src/** 互导禁令 +
        # 渲染侧 electron 包运行时引用禁令（唯一合法面 = preload 暴露的
        # window.electronAPI）+ preload 最小面禁令。零容忍集合规则（现状双向
        # 零导入，从零起步，无基线）。不入 fast 档：fast 预算满额 144/144
        # （同 async-blocking 先例），CI 由 traceability-gate 恒跑 job 承载
        # （electron 车道闸不住 frontend 侧改动，恒跑无双向盲区）；
        # 本地 --mode all / --filter 可跑。
        id="electron-boundary",
        label="Electron 导入边界（互导/渲染侧 electron 包/preload 最小面，零容忍）",
        domain="cross",
        command=(sys.executable, "scripts/check_electron_boundary.py"),
        est_seconds=2,
    ),
    Gate(
        # 元门禁（批次 M，执行方案 09-24）：门禁体系的门禁 V1–V8。自举：
        # 本 gate wiring=ci，未接线时 V1 红在自己身上。
        id="gate-wiring",
        label="元门禁：接线/契约/力度/账本/基线覆盖 八项校验",
        domain="cross",
        command=(sys.executable, "scripts/check_gate_wiring.py"),
        est_seconds=3,
        fast=True,
    ),
    Gate(
        # 项目全景图三合一（方案 AI辅助编程体系落地AgentOS_20261022 §4.4，
        # 2026-10-28 接线）：增量聚合 → 落盘 docs/graph/graph.js（gitignored
        # 派生物）→ 校验红绿；校验与出图同一遍聚合，数据文件=校验顺手副产物，
        # 无"只校验不出图"的独立入口。纯标准库，LLM 零参与。
        id="graph-check",
        label="项目全景图聚合+校验（增量，graph.js=校验副产物）",
        domain="cross",
        command=(sys.executable, "scripts/graph_aggregate.py"),
        est_seconds=10,
        fast=True,
        paths=("docs/",),
    ),
    Gate(
        # 新增文档 frontmatter 强制闸（提案 P-01/E2-1，gate-change ADR
        # 2026-10-30-doc-frontmatter-gate）：变更集新增的 docs/**.md 必带
        # doc_id/type/status 三字段，枚举真值单源 graph_model。存量不回填
        # （方案已裁），docs/decisions/ 豁免（ADR 五段格式独立约定）。
        # 变更集=暂存区新增 ∪（--base/GITHUB_BASE_REF 时）PR 范围新增，
        # 刻意无 HEAD^ 兜底（多会话并行仓 false red）。
        id="doc-frontmatter",
        label="新增 docs 文档三字段强制（存量不回填）",
        domain="docs",
        command=(sys.executable, "scripts/check_doc_frontmatter.py"),
        est_seconds=1,
        fast=True,
        paths=("docs/",),
    ),
    Gate(
        # docs/working/INDEX.md 新鲜度闸（F3①，gate-change ADR
        # docs/decisions/2026-11-04-gate-surface-hardening-index-freshness-
        # and-schema-dual-source.md）：INDEX 是快照、无自动再生成触发（复核件
        # _修复复核_F2F3F7.md F3 段活体演示：42194d443 提交前 2 分钟入库致
        # INDEX 落地即滞后）。比对面=git 跟踪面条目覆盖与链接正确性（多会话
        # 在飞未跟踪件不执法——磁盘语义会逼出含外来件条目的 INDEX，CI 干净
        # 检出下永红）；红即指路跑 gen_working_index.py。
        id="working-index-fresh",
        label="docs/working 索引新鲜度（跟踪面条目覆盖 ≡ 生成器产出）",
        domain="docs",
        command=(sys.executable, "scripts/check_working_index_fresh.py"),
        # est 0 = 实测次秒级（0.22s：collect+render 复用 + git ls-files）；
        # fast 预算已满 140/140，秒粒度入档取 0——预算上调在 check_gate_wiring.py
        # （本闸 ADR 批改动面外），移交后续 gate-change 随门禁面增长同前例处理。
        est_seconds=0,
        fast=True,
        paths=("docs/",),
    ),
    Gate(
        # 部署环境检测门（用户裁定 2026-10-28「CI 必含部署环境的检测和测试」）：
        # deploy/**/*.sh 逐个 bash -n + deploy/*.service systemd-analyze verify
        # （/opt 部署路径经临时 --root 沙箱补桩，本机路径缺失不误红）+
        # deploy/nginx.conf server 片段经 events+http harness 过 nginx -t。
        # Windows 本机 systemd/nginx 专属项显式 SKIP（CI ubuntu 车道执法），
        # bash 缺失 FAIL；Linux 缺任一工具 FAIL（环境缺陷本身即本门标的）。
        id="deploy-env",
        label="部署环境检测（bash -n + systemd verify 沙箱 + nginx -t harness）",
        domain="cross",
        command=(sys.executable, "scripts/check_deploy_env.py"),
    ),
    Gate(
        # 受保护清单检查（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 2，
        # 2026-10-28 接线）：门禁配置/基线棘轮/评估指标/渲染规定源码 diff 命中
        # 即红——执法面被被执法面修改 = 博弈入口；唯一放行 = gate-change 独立
        # 任务（ADR + [gate-change: 标记 + 全量档）。paths 空 = 恒跑（检查器
        # 自身按变更集判定，不得被模块过滤掉）。
        id="gate-protected",
        label="机械闸受保护清单（门禁护门禁，gate-change 标记放行）",
        domain="cross",
        command=(sys.executable, "scripts/check_gate_protected.py"),
        est_seconds=2,
        fast=True,
    ),
    Gate(
        # 规范执法档位（方案 §3.6 件 4）：每条规范必须标三档执法位之一
        # （机械闸/评估指标/审查清单），注册表 + 基线棘轮（存量触碰即补）+
        # 防悬空引用（gate id/指标 id/清单路径必须真实存在）。
        id="enforcement-tier",
        label="规范执法档位注册表（三档执法位 + 防悬空引用 + 基线棘轮）",
        domain="cross",
        command=(sys.executable, "scripts/check_rule_enforcement_tier.py"),
        est_seconds=2,
        fast=True,
        paths=("config/rules/",),
    ),
]

# all = 穷尽集（本入口持有的全部门禁；pre-commit/python-e2e 见模块 docstring 归属说明）
MODES: dict[str, list[str]] = {
    "fast": [g.id for g in GATES if g.fast],
    "kernel": [g.id for g in GATES if g.domain == "kernel"],
    # 插件车道并入主题色卫生闸 + 主题对比度闸：plugin.json 声明模板与
    # web/webview 资产在两闸扫描面内（2026-10-25 / 2026-11-07 用户裁定
    # 文字/图标/背景一律随主题且渲染面必须可读），插件开发本地验绿不漏裁定
    "plugins": [g.id for g in GATES if g.domain == "plugins"]
    + ["frontend-theme-color-hygiene", "frontend-theme-contrast"],
    "frontend": [g.id for g in GATES if g.domain == "frontend"],
    "all": [g.id for g in GATES],
    # CI 接线全集（wiring=ci/inline-ci）：远端 Actions 跑的就是这个集合。
    # 2026-10-30 用户裁定「CI 跟随执行空间」——本地提交+本地打包时由
    # check_ci_gate --local 在本地执行同一集合作 CI 取证；新增门禁默认
    # wiring=ci 自动并入，无需改此表。
    "ci": [g.id for g in GATES if g.wiring in ("ci", "inline-ci")],
}


# ── 门禁图校验（重复 id / 未知依赖 / 环 / wiring 合法性）──────────────
WIRING_VALUES = ("ci", "inline-ci", "observation", "local")


def validate_graph(gates: list[Gate]) -> None:
    ids = [g.id for g in gates]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"run_gates: 重复门禁 id: {dupes}")
    for gate in gates:
        if gate.wiring not in WIRING_VALUES:
            raise ValueError(f"run_gates: 门禁 {gate.id} wiring 非法: {gate.wiring!r}（合法值 {WIRING_VALUES}）")
        if gate.wiring != "ci" and not gate.wiring_reason:
            raise ValueError(f"run_gates: 门禁 {gate.id} wiring={gate.wiring} 但缺 wiring_reason（豁免必须留痕）")
        if gate.fast and gate.est_seconds is None:
            raise ValueError(f"run_gates: 门禁 {gate.id} fast=True 但未声明 est_seconds（V5 预算防漏洞）")
    # 依赖可以指向选中集之外的门禁（CI job 用 --filter 拆开时，依赖关系
    # 由 workflow 的 job needs: 承担），只要它在全量清单里存在即可。
    known = {g.id for g in GATES}
    for gate in gates:
        for dep in gate.needs:
            if dep not in known:
                raise ValueError(f"run_gates: 门禁 {gate.id} 依赖未知门禁 {dep}")
    # Kahn 拓扑排序检环（仅选中集内部的边）
    in_set = set(ids)
    indegree = {g.id: 0 for g in gates}
    dependents: dict[str, list[str]] = {g.id: [] for g in gates}
    for gate in gates:
        for dep in gate.needs:
            if dep in in_set:
                indegree[gate.id] += 1
                dependents[dep].append(gate.id)
    queue = [gid for gid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        gid = queue.pop()
        seen += 1
        for nxt in dependents[gid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(gates):
        cyclic = sorted(gid for gid, deg in indegree.items() if deg > 0)
        raise ValueError(f"run_gates: 依赖环: {cyclic}")


# ── 执行 ─────────────────────────────────────────────────────────────
def resolve_exe(name: str) -> str:
    """解析可执行文件全路径（Windows 下 npm/cargo 等需展开 .cmd/.exe）。"""
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"run_gates: 找不到可执行文件 {name!r}（PATH 或环境前置缺失，见模块 docstring）")
    return resolved


def build_argv(gate: Gate) -> list[str]:
    if gate.command:
        argv = [resolve_exe(gate.command[0]), *gate.command[1:]]
    elif gate.shell:
        argv = [resolve_exe("bash"), "-c", gate.shell]
    else:
        raise ValueError(f"run_gates: 门禁 {gate.id} 未定义 command/shell")
    return argv


def run_gate(gate: Gate) -> tuple[str, int | None, str, float]:
    """执行一个门禁，返回 (状态, 退出码, 输出, 耗时秒)。"""
    started = time.perf_counter()
    try:
        argv = build_argv(gate)
    except (FileNotFoundError, ValueError) as exc:
        return "failed", None, f"{exc}\n", 0.0
    env = {**os.environ, **gate.env, "AGENTOS_GATE_RUN_ID": _RUN_ID}
    try:
        proc = subprocess.run(  # noqa: S603 —— argv 已按白名单构造
            argv,
            cwd=str(ROOT / gate.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = proc.stdout or ""
        status = "passed" if proc.returncode == 0 else "failed"
        return status, proc.returncode, output, time.perf_counter() - started
    except OSError as exc:
        return "failed", None, f"启动失败: {exc}\n", time.perf_counter() - started


def _print_result(gate: Gate, status: str, seconds: float, output: str, exit_code: int | None) -> None:
    verbose = os.environ.get("AGENTOS_GATE_VERBOSE") == "1"
    tag = status.upper()
    if status == "passed":
        print(f"run_gates: PASS {gate.label} ({seconds:.1f}s)", flush=True)
        if verbose and output:
            print(output, end="", flush=True)
        return
    print(f"\n== {tag} {gate.label} ({seconds:.1f}s) ==", flush=True)
    print(f"run_gates: 门禁 {gate.id} 失败，完整输出：", flush=True)
    print(output, end="", flush=True)


def run_selected(gates: list[Gate], max_active: int) -> int:
    """按依赖图有界并行执行，返回进程退出码。"""
    validate_graph(gates)
    by_id = {g.id: g for g in gates}
    in_set = set(by_id)
    state: dict[str, str] = {g.id: "pending" for g in gates}
    results: dict[str, tuple[str, int | None, str, float]] = {}
    running: dict[Future, str] = {}

    # timing 独占窗口（ADR 2026-10-30-timing-gate-exclusive-scheduling 升级）：
    # timing-gate 在选中集内时**绝对第一**——其余车道一律 hold 到它出结果
    # （passed 之外 failed/skipped 也放行，批已判死不再空转）。负载敏感的
    # 时序断言族连 wave-1 轻邻居都吃不住（独立 CI B 轮实锤：S1 预算放宽后
    # 仍被同波 sdk-test/tsc 打死三只兄弟）。
    _exclusive_head = "timing-gate" if "timing-gate" in in_set else None

    def head_released(gate: Gate) -> bool:
        if _exclusive_head is None or gate.id == _exclusive_head:
            return True
        return state.get(_exclusive_head) in ("passed", "failed", "skipped")

    def deps_passed(gate: Gate) -> bool:
        return all(state[d] == "passed" for d in gate.needs if d in in_set)

    def deps_failed(gate: Gate) -> bool:
        return any(state[d] in ("failed", "skipped") for d in gate.needs if d in in_set)

    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=max_active)
    started_at = time.perf_counter()
    try:
        while True:
            # 尽量填满并发槽（就绪 = pending 且选中集内依赖全部 passed）
            for gate in gates:
                if len(running) >= max_active:
                    break
                if state[gate.id] != "pending":
                    continue
                if deps_passed(gate) and head_released(gate):
                    state[gate.id] = "running"
                    print(f"run_gates: start {gate.label}", flush=True)
                    fut = pool.submit(run_gate, gate)
                    running[fut] = gate.id
            if not running:
                # 无在跑且无可启动：级联标记被失败依赖阻塞的剩余门禁为 skipped
                progressed = True
                while progressed:
                    progressed = False
                    for gate in gates:
                        if state[gate.id] != "pending":
                            continue
                        if deps_failed(gate):
                            state[gate.id] = "skipped"
                            results[gate.id] = ("skipped", None, "", 0.0)
                            print(f"run_gates: SKIP {gate.label}（依赖失败）", flush=True)
                            progressed = True
                break
            done, _ = wait(set(running), return_when=FIRST_COMPLETED)
            for fut in done:
                gid = running.pop(fut)
                status, exit_code, output, seconds = fut.result()
                state[gid] = status
                results[gid] = (status, exit_code, output, seconds)
                _print_result(by_id[gid], status, seconds, output, exit_code)
    finally:
        pool.shutdown(wait=True)

    total = time.perf_counter() - started_at
    passed = sum(1 for s, *_ in results.values() if s == "passed")
    failed = sum(1 for s, *_ in results.values() if s == "failed")
    skipped = sum(1 for s, *_ in results.values() if s == "skipped")
    print(f"\nrun_gates: {passed} passed, {failed} failed, {skipped} skipped in {total:.1f}s.", flush=True)

    blocking = [
        (by_id[gid], results[gid])
        for gid, (status, *_) in results.items()
        if status in ("failed", "skipped") and not by_id[gid].allow_failure
    ]
    if blocking:
        print("run_gates: 未通过门禁：", file=sys.stderr)
        for gate, (_, exit_code, _, seconds) in blocking:
            disposition = "" if not gate.allow_failure else "（观察型）"
            print(
                f"  - {gate.id}{disposition}: {gate.label} (exit={exit_code}, {seconds:.1f}s)",
                file=sys.stderr,
            )
        return 1
    return 0


def filter_by_changed(gates: list[Gate], changed: list[str] | None) -> list[Gate]:
    """fast 档变更模块过滤（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 步骤 5）。

    changed=None（CI / 既有用法）→ 恒等返回全集，行为完全不变；
    changed=list → paths 为空的门禁恒跑（cross-cutting），paths 非空的门禁
    仅当至少一个变更文件命中其守护路径前缀才跑。过滤只缩不扩
    （结果 ⊆ 全集），保守方向防漏检。
    """
    if changed is None:
        return list(gates)
    kept: list[Gate] = []
    for g in gates:
        if not g.paths or any(f.startswith(p) for f in changed for p in g.paths):
            kept.append(g)
    return kept


def list_gates(as_json: bool = False) -> int:
    if as_json:
        # 元门禁消费的机器可读合同（check_gate_wiring.py 经 subprocess 取
        # 此输出，不 import 本文件、不重抄 GATES——单一真值防第二口径）。
        payload = [
            {
                "id": g.id,
                "label": g.label,
                "domain": g.domain,
                "fast": g.fast,
                "wiring": g.wiring,
                "wiring_reason": g.wiring_reason,
                "allow_failure": g.allow_failure,
                "est_seconds": g.est_seconds,
                "needs": list(g.needs),
                "paths": list(g.paths),
                "command": g.shell if g.shell else " ".join(g.command),
            }
            for g in GATES
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    print(f"{'门禁 id':<28} {'域':<10} {'fast':<5} {'needs':<20} 命令")
    for g in GATES:
        cmd = g.shell if g.shell else " ".join(g.command)
        needs = ",".join(g.needs) or "-"
        fast = "✓" if g.fast else "-"
        print(f"{g.id:<28} {g.domain:<10} {fast:<5} {needs:<20} {cmd}")
    print("\n模式：")
    for mode, ids in MODES.items():
        print(f"  {mode:<10} {len(ids)} 个门禁")
    return 0


def main() -> int:
    # Windows 控制台默认 GBK/cp1252，门禁 label 含数学符号（如 ⊆）时 print
    # 直接 UnicodeEncodeError（2026-11-06 本机实证）——入口重配置 UTF-8，
    # 与 CI（Linux UTF-8）行为对齐。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="统一机械门禁入口（quality-gates）")
    parser.add_argument("--list", action="store_true", help="列出全部门禁")
    parser.add_argument("--json", action="store_true", help="与 --list 连用：机器可读 JSON（元门禁消费）")
    parser.add_argument("--mode", choices=sorted(MODES), help="按模式选择门禁聚合")
    parser.add_argument("--filter", help="逗号分隔的门禁 id 精确选择（CI job 用）")
    parser.add_argument(
        "--changed-files",
        nargs="*",
        help="变更文件列表（pre-commit staged 变更集；fast 档仅跑变更模块的门禁）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印选中的门禁 id，不执行")
    ns = parser.parse_args()

    if ns.list:
        return list_gates(as_json=ns.json)
    if bool(ns.mode) == bool(ns.filter):
        parser.error("必须且只能指定 --mode 或 --filter 其一")

    known = {g.id for g in GATES}
    if ns.filter:
        wanted = [x.strip() for x in ns.filter.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in known]
        if unknown:
            print(f"run_gates: 未知门禁 id: {unknown}（--list 查看）", file=sys.stderr)
            return 2
        selected = [g for g in GATES if g.id in set(wanted)]
    else:
        selected = [g for g in GATES if g.id in set(MODES[ns.mode])]

    # fast 档变更模块过滤（pre-commit 提交即闸路径；CI / 既有用法传 None 不变）
    if ns.changed_files is not None:
        selected = filter_by_changed(selected, ns.changed_files)

    if not selected:
        print("run_gates: 未选中任何门禁", file=sys.stderr)
        return 2

    if ns.dry_run:
        for g in selected:
            print(g.id)
        return 0

    raw = os.environ.get("AGENTOS_GATE_CONCURRENCY", "")
    if raw:
        max_active = int(raw)
        if max_active < 1:
            parser.error("AGENTOS_GATE_CONCURRENCY 须为正整数")
        source = "$AGENTOS_GATE_CONCURRENCY"
    else:
        max_active = min(4, os.cpu_count() or 1, len(selected)) or 1
        source = "默认 cap 4"
    print(f"run_gates: 运行 {len(selected)} 个门禁，并行 {max_active}（{source}）。", flush=True)
    try:
        return run_selected(selected, max_active)
    finally:
        _cleanup_gate_tmp()


if __name__ == "__main__":
    sys.exit(main())
