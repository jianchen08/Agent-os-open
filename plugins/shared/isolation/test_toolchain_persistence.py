# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""工具链持久层契约测试（ADR 2026-11-07 决策 5/6，实施拆分 T3；2026-11-10 修订二）。

覆盖（判据指向交付物内容）：
1. docker_provider._TOOLCHAIN_VOLUMES：标准挂载点在位（RUSTUP_HOME/CARGO_HOME
   整目录/NVM_DIR/GOPATH/pipx/uv tools）、标准 env 与挂载点一一对应、
   _BUILD_CACHE_VOLUMES 退役 cargo-registry 子目录卷（迁移语义）、
   _build_run_args 双集合合并挂载 + 标准 env 注入；
2. Dockerfile 种子层契约：rustup 安装在位、RUSTUP_HOME/CARGO_HOME 指向卷挂载
   路径、钉版与仓根 rust-toolchain.toml 一致（镜像层种子，环境层零管理）、
   minimal profile 体积控制、单源文件 COPY 进 CARGO_HOME；
3. 单源面：config/toolchains/cargo-source.toml 在位、官方 sparse 源语义
   （禁 replace-with）、被 Dockerfile 引用；
   配置面零注入：isolation_config.yaml 无 pin/cargo 源注入（2026-11-10 修订二
   回退），rust-toolchain-pin.toml / toolchain_pin.py 不存在；
4. 环境层零管理（2026-11-10 修订二：CC/Codex 同构——零探测、零预装、零工具链
   管理，agent 运行中撞到依赖缺失自己装，持久层保证装一次永久）：
   - 嗅探判据退役保持：_is_rust_task 不存在、decline 文案不存在；
   - 无主动探测/种子面：源码无 command -v cargo / rustup 调用面 /
     _TOOLCHAIN_PROBE_SH / _TOOLCHAIN_SEED_SH / _ensure_toolchain 等；
   - 工作区根含 rust-toolchain.toml 的 context 走 create_environment 直通
     READY（无工具链调用桩交互，仅 mkdir + workspace 侧种子写入）；
5. 持久化接线保留（agent 自装的 rustup/cargo 对后续任务可见）：
   BASH_ENV 引导（PATH="$CARGO_HOME/bin:$PATH"）+ RUSTUP_HOME/CARGO_HOME
   直接 env 对钉发行版持久 home（VHDX，跨任务/跨重启）+ workspace 侧种子
   写入（被动文件播种，失败不阻断）。

隔离策略：isolation_types 用真实模块；docker CLI / wsl.exe 属外部依赖，mock
仅限 _popen_run（记录型替身）；仓库文件（Dockerfile / rust-toolchain.toml /
cargo-source.toml / isolation_config.yaml）只读断言，不写。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationLevel,
    OperationType,
    TaskType,
)

if TYPE_CHECKING:
    from plugins.shared.system.isolation.providers.docker_provider import DockerProvider
    from plugins.shared.system.isolation.providers.wsl_native_provider import WslNativeProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/
_REPO_ROOT = _PLUGIN_DIR.parents[2]  # → 仓根
_DOCKERFILE = _REPO_ROOT / "docker_assets" / "agentos" / "Dockerfile"
_RUST_TOOLCHAIN_TOML = _REPO_ROOT / "rust-toolchain.toml"
# 单源面路径常量：测试夹具对仓库文件的只读契约断言（直读闸豁免通道，基线
# 条目注明）；路径写斜杠串形态——闸扫描的是 config 目录直读面，本处是断言
# 注入值的夹具，非生产直读。
_CARGO_SOURCE_TOML = _REPO_ROOT / "config/toolchains/cargo-source.toml"
_ISOLATION_CONFIG = _REPO_ROOT / "config/plugins/isolation/isolation_config.yaml"

_SAMPLE_CARGO_SOURCE = '[source.crates-io]\nregistry = "sparse+https://index.crates.io/"\n'


def _load_mod(rel: Path, mod_name: str) -> Any:
    """动态加载 provider 模块（唯一模块名，防与其它测试的裸名模块冲突）。"""
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / rel)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_WSL_MOD = _load_mod(Path("providers") / "wsl_native_provider.py", "toolchain_wsl_provider_test")
_DOCKER_MOD = _load_mod(Path("providers") / "docker_provider.py", "toolchain_docker_provider_test")

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    WslNativeProvider = _WSL_MOD.WslNativeProvider  # type: ignore[misc]
    DockerProvider = _DOCKER_MOD.DockerProvider  # type: ignore[misc]


def _run(coro: Any) -> Any:
    """共享测试进程中其他测试可能关闭主 loop，须自建独立 loop。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Recorder:
    """_popen_run 记录型替身：按谓词选脚本（可抛异常），默认 (0, b"", b"")。"""

    def __init__(self, routes: list[tuple[Callable[[str], bool], Any]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._routes = routes or []

    async def __call__(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        self.calls.append(list(args))
        joined = " ".join(args)
        for predicate, result in self._routes:
            if predicate(joined):
                if isinstance(result, Exception):
                    raise result
                return result  # type: ignore[no-any-return]
        return 0, b"", b""


def _wsl_provider(tmp_path: Path, **overrides: Any) -> Any:
    """构造受测 wsl provider：state_dir 指向 tmp_path，_popen_run 为记录替身。

    配置即生产形态（无 pin/cargo 源注入——环境层零管理）；个别用例可经
    overrides 注入 cargo_source_config 测被动播种 payload。
    """
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "distro": "Ubuntu",
        "state_dir": str(tmp_path / "envs"),
        "wsl_exe": "wsl",
    }
    config.update(overrides)
    provider = WslNativeProvider(config)
    provider._popen_run = _Recorder()  # type: ignore[method-assign]
    provider._workspace = str(ws)
    return provider


def _wsl_ctx(workspace: str | None, metadata: dict[str, Any] | None = None) -> IsolationContext:
    return IsolationContext(
        task_id="t-1",
        task_type=TaskType.ATOMIC,
        operation_type=OperationType.CODE_EXECUTION,
        workspace=workspace,
        isolation_level=IsolationLevel.CONTAINER,
        metadata=metadata or {},
    )


def _docker_provider(**overrides: Any) -> Any:
    config: dict[str, Any] = {"auto_build": False}
    config.update(overrides)
    return DockerProvider(config)


def _metadata_file(provider: Any, name: str) -> Path:
    return Path(provider._state_dir) / f"{name}.json"


# ── 1. _TOOLCHAIN_VOLUMES（docker_provider）──────────────────


class TestToolchainVolumes:
    def test_standard_mount_paths(self) -> None:
        """标准路径约定（ADR 决策 5）：RUSTUP/CARGO 整目录 + NVM/Go/pipx/uv。"""
        mounts = dict(DockerProvider._TOOLCHAIN_VOLUMES)
        assert mounts["agentos-rustup-home"] == "/usr/local/rustup"
        assert mounts["agentos-cargo-home"] == "/usr/local/cargo"  # 整目录
        assert mounts["agentos-nvm-dir"] == "/root/.nvm"
        assert mounts["agentos-gopath"] == "/root/go"
        assert mounts["agentos-pipx-home"] == "/root/.local/pipx"
        assert mounts["agentos-uv-tools"] == "/root/.local/share/uv/tools"

    def test_env_matches_mount_paths(self) -> None:
        """标准 env 与挂载点一一对应（同一份路径真值，防两表漂移）。"""
        mounts = dict(DockerProvider._TOOLCHAIN_VOLUMES)
        env = DockerProvider._TOOLCHAIN_ENV
        assert env["RUSTUP_HOME"] == mounts["agentos-rustup-home"]
        assert env["CARGO_HOME"] == mounts["agentos-cargo-home"]
        assert env["NVM_DIR"] == mounts["agentos-nvm-dir"]
        assert env["GOPATH"] == mounts["agentos-gopath"]
        assert env["PIPX_HOME"] == mounts["agentos-pipx-home"]
        assert env["UV_TOOL_DIR"] == mounts["agentos-uv-tools"]

    def test_cache_volumes_retire_cargo_registry_subdir(self) -> None:
        """迁移语义：agentos-cargo-registry 只挂 /registry 子目录的旧卷退役，
        CARGO_HOME 整目录卷（工具链卷）取代；其余依赖缓存语义保持。"""
        cache_paths = {p for _, p in DockerProvider._BUILD_CACHE_VOLUMES}
        assert "/usr/local/cargo/registry" not in cache_paths
        assert "/cargo-cache" in cache_paths  # sccache 编译缓存仍在缓存卷
        cache_names = {v for v, _ in DockerProvider._BUILD_CACHE_VOLUMES}
        assert "agentos-cargo-registry" not in cache_names

    def test_volume_names_disjoint(self) -> None:
        """工具链卷与依赖缓存卷同层分立（卷名不相交）。"""
        tool_names = {v for v, _ in DockerProvider._TOOLCHAIN_VOLUMES}
        cache_names = {v for v, _ in DockerProvider._BUILD_CACHE_VOLUMES}
        assert not (tool_names & cache_names)

    def test_build_run_args_mounts_both_sets(self) -> None:
        provider = _docker_provider()
        args = provider._build_run_args("cua-t", _wsl_ctx(None))
        vs = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        for vol, path in (*DockerProvider._TOOLCHAIN_VOLUMES, *DockerProvider._BUILD_CACHE_VOLUMES):
            assert f"{vol}:{path}" in vs, f"缺挂载 {vol}:{path}"

    def test_build_run_args_injects_toolchain_env(self) -> None:
        provider = _docker_provider()
        args = provider._build_run_args("cua-t", _wsl_ctx(None))
        es = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        for env_key, path in DockerProvider._TOOLCHAIN_ENV.items():
            assert f"{env_key}={path}" in es, f"缺标准 env {env_key}"


# ── 2. Dockerfile 种子层契约（镜像层种子；环境层零管理）──────


class TestDockerfileSeedContract:
    @staticmethod
    def _text() -> str:
        return _DOCKERFILE.read_text(encoding="utf-8")

    def test_rustup_seed_layer_present(self) -> None:
        text = self._text()
        assert "rustup-init" in text
        assert "--profile minimal" in text  # 体积控制
        # rust-toolchain.toml components 补齐
        assert "rustup component add rustfmt clippy" in text

    def test_env_points_at_volume_mounts(self) -> None:
        text = self._text()
        assert "RUSTUP_HOME=/usr/local/rustup" in text
        assert "CARGO_HOME=/usr/local/cargo" in text

    def test_pinned_version_matches_rust_toolchain_toml(self) -> None:
        """镜像种子钉版与仓根 rust-toolchain.toml 一致（镜像层一次预装；
        工作区 rust-toolchain.toml 命中同版本时任务内 rustup 不再触发下载）。"""
        toml_text = _RUST_TOOLCHAIN_TOML.read_text(encoding="utf-8")
        m = re.search(r'channel\s*=\s*"([^"]+)"', toml_text)
        assert m is not None, "rust-toolchain.toml 缺 channel 钉版"
        assert f"--default-toolchain {m.group(1)}" in self._text()

    def test_cargo_source_copied_into_cargo_home(self) -> None:
        text = self._text()
        assert re.search(
            r"COPY\s+config/toolchains/cargo-source\.toml\s+/usr/local/cargo/config\.toml",
            text,
        ), "单源文件未 COPY 进 CARGO_HOME"


# ── 3. 单源面（cargo 源）+ 配置面零注入 ──────────────────────


class TestCargoSourceSingleSource:
    def test_file_exists_with_official_sparse_semantics(self) -> None:
        text = _CARGO_SOURCE_TOML.read_text(encoding="utf-8")
        assert "[source.crates-io]" in text
        assert "sparse+https://index.crates.io/" in text
        # 坑面性质断言：无生效的源替换指令（replace-with 是「源与缓存目录
        # 不匹配」实证坑的根因；注释提及不受限，配置项出现即红）
        assert re.search(r"^\s*replace-with\s*=", text, re.MULTILINE) is None

    def test_dockerfile_references_single_source(self) -> None:
        assert "config/toolchains/cargo-source.toml" in TestDockerfileSeedContract._text()

    def test_wsl_native_uninjected_source_path_is_none(self) -> None:
        """未注入 cargo_source_config = None = 跳过 distro 侧播种（单源走注入
        交付，无仓根回落）。

        2026-11-07 分级单副本刀 5 收敛：插件禁止直读配置目录（ADR 2026-09-14
        §2.6 直读守卫），仓根回落删除；配置语义（官方 sparse 源）见
        test_file_exists_with_official_sparse_semantics。
        """
        provider = WslNativeProvider({"state_dir": str(_PLUGIN_DIR)})
        assert provider._cargo_source_path is None

    def test_seed_payload_carries_single_source_content(self, tmp_path: Path) -> None:
        """注入 cargo_source_config 时，workspace 侧被动播种把单源文件内容
        作为 payload 传给发行版侧写入（agent 后装 rustup 时源配置已在位）。"""
        source = tmp_path / "cargo-source.toml"
        source.write_text(_SAMPLE_CARGO_SOURCE, encoding="utf-8")
        provider = _wsl_provider(tmp_path, cargo_source_config=str(source))
        _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-ws1"))
        joined = "\n".join(" ".join(c) for c in provider._popen_run.calls)
        assert "sparse+https://index.crates.io/" in joined  # 单源内容进 seed argv


class TestConfigFaceZero:
    """配置面零注入（2026-11-10 修订二回退）：环境层零工具链管理。"""

    def test_isolation_config_has_no_toolchain_injection(self) -> None:
        """isolation_config.yaml 无 pin/cargo 源注入（探测+种子方案的配置面
        随方案一并退役；重引入须先过 ADR）。"""
        cfg = yaml.safe_load(_ISOLATION_CONFIG.read_text(encoding="utf-8"))
        providers = cfg["providers"]
        for section in ("wsl_native", "cua"):
            assert "rust_toolchain_pin" not in providers[section]
        assert "cargo_source_config" not in providers["wsl_native"]

    def test_pin_single_source_files_absent(self) -> None:
        """rust-toolchain-pin.toml / toolchain_pin.py 不存在（钉子单源机制
        整体退役——环境层无需知道版本号，rustup 首次调用按工作区钉子自装）。"""
        assert not (_REPO_ROOT / "config/toolchains/rust-toolchain-pin.toml").exists()
        assert not (_PLUGIN_DIR / "providers" / "toolchain_pin.py").exists()


# ── 4. 环境层零管理（嗅探退役保持 + 无探测/种子面 + 直通 READY）


class TestSniffCriterionRetired:
    def test_is_rust_task_absent(self) -> None:
        """嗅探判据整个退役——_is_rust_task 定义不存在（2026-11-10 修订二
        保持退役状态）。"""
        assert not hasattr(WslNativeProvider, "_is_rust_task")

    def test_decline_wording_absent(self) -> None:
        """「不承接 Rust 任务」类文案不存在（源码文本级断言）。"""
        source = (_PLUGIN_DIR / "providers" / "wsl_native_provider.py").read_text(encoding="utf-8")
        assert "不承接" not in source
        assert "路由容器后端" not in source


class TestNoActiveToolchainManagement:
    """环境层零探测零预装（CC/Codex 同构）：wsl_native 无主动工具链机制。"""

    @staticmethod
    def _source() -> str:
        return (_PLUGIN_DIR / "providers" / "wsl_native_provider.py").read_text(encoding="utf-8")

    def test_no_probe_or_seed_call_face(self) -> None:
        """源码无工具链调用面：无 command -v cargo、无 rustup 调用链
        （rustup toolchain/-init/default/component/下载源）。
        注：`$HOME/.rustup` 是 RUSTUP_HOME 持久 home 锚点值（保留接线），
        非调用面，不在禁列。"""
        source = self._source()
        assert "command -v cargo" not in source
        for banned in ("rustup toolchain", "rustup-init", "rustup default", "rustup component", "sh.rustup.rs"):
            assert banned not in source, f"provider 残留 rustup 调用面: {banned}"

    def test_probe_seed_symbols_absent(self) -> None:
        """探测/种子链路符号整体退役（源码文本 + 类属性双断言）。"""
        source = self._source()
        for banned in (
            "_TOOLCHAIN_PROBE_SH",
            "_TOOLCHAIN_SEED_SH",
            "_ensure_toolchain",
            "_probe_toolchain_version",
            "_load_toolchain_pin",
            "toolchain_seed_timeout",
            "rust_toolchain_pin",
        ):
            assert banned not in source, f"provider 残留探测/种子符号: {banned}"
            assert not hasattr(WslNativeProvider, banned)


class TestDirectCreate:
    """直通 READY：工作区文件内容不参与环境选择，create_environment 不 gate
    工具链（agent 运行中撞到依赖缺失自己装）。"""

    @staticmethod
    def _assert_no_toolchain_interaction(recorder: Any) -> None:
        """创建期 wsl 调用仅 mkdir + workspace 侧种子写入，无任何工具链
        探测/安装调用。"""
        joined_calls = [" ".join(c) for c in recorder.calls]
        assert joined_calls, "mkdir/种子写入应发生"
        for joined in joined_calls:
            assert "mkdir" in joined or "toolchain_env.sh" in joined, (
                f"创建期出现非创建类 wsl 调用（探测/种子残留面）: {joined[:120]}"
            )
        joined_all = "\n".join(joined_calls)
        assert "cargo --version" not in joined_all
        assert "rustup toolchain" not in joined_all

    def test_rust_toolchain_toml_workspace_direct_ready(self, tmp_path: Path) -> None:
        """工作区根含 rust-toolchain.toml 的 context 走 create_environment
        直通 READY——零探测、零预装，工作区文件内容不触发任何工具链交互。"""
        provider = _wsl_provider(tmp_path)
        (tmp_path / "ws" / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.88.0"\n', encoding="utf-8")
        recorder = _Recorder()
        provider._popen_run = recorder  # type: ignore[method-assign]
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-rust"))
        assert env.status == EnvironmentStatus.READY.value
        self._assert_no_toolchain_interaction(recorder)
        assert _metadata_file(provider, "cua-rust").exists() is True

    def test_cargo_toml_workspace_direct_ready(self, tmp_path: Path) -> None:
        """对照组（防拟合）：Cargo.toml 工作区同样直通 READY。"""
        provider = _wsl_provider(tmp_path)
        (tmp_path / "ws" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        recorder = _Recorder()
        provider._popen_run = recorder  # type: ignore[method-assign]
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-rust"))
        assert env.status == EnvironmentStatus.READY.value
        self._assert_no_toolchain_interaction(recorder)

    def test_metadata_language_rust_no_special_path(self, tmp_path: Path) -> None:
        """对照组（防拟合）：metadata language=rust 不触发任何特殊路径。"""
        provider = _wsl_provider(tmp_path)
        recorder = _Recorder()
        provider._popen_run = recorder  # type: ignore[method-assign]
        env = _run(
            provider.create_environment(_wsl_ctx(provider._workspace, metadata={"language": "rust"}), "cua-rust")
        )
        assert env.status == EnvironmentStatus.READY.value
        self._assert_no_toolchain_interaction(recorder)

    def test_workspace_still_validated(self, tmp_path: Path) -> None:
        """零管理 ≠ 零校验：工作区缺失仍 fail-closed（既有契约保持）。"""
        provider = _wsl_provider(tmp_path)
        env = _run(provider.create_environment(_wsl_ctx(workspace=None), "cua-x"))
        assert env.status == EnvironmentStatus.ERROR.value
        assert "工作空间为空" in str(env.provider_info.get("error"))


# ── 5. 持久化接线保留（agent 自装 → 后续任务可见）────────────


class TestWslToolchainEnv:
    def test_runtime_env_pairs_include_bash_env(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        provider._workspace_wsl = "/mnt/d/ws"
        provider._distro_home = None
        pairs = provider._runtime_env_pairs()
        assert "BASH_ENV=/mnt/d/ws/.task_runtime/toolchain_env.sh" in pairs

    def test_direct_pairs_point_at_distro_home(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        provider._workspace_wsl = "/mnt/d/ws"
        provider._distro_home = "/home/u"
        pairs = provider._runtime_env_pairs()
        assert "RUSTUP_HOME=/home/u/.rustup" in pairs
        assert "CARGO_HOME=/home/u/.cargo" in pairs

    def test_no_direct_pairs_without_home(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        provider._workspace_wsl = "/mnt/d/ws"
        provider._distro_home = None
        pairs = provider._runtime_env_pairs()
        assert not any(p.startswith(("RUSTUP_HOME=", "CARGO_HOME=")) for p in pairs)

    def test_no_pairs_without_workspace(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        assert provider._runtime_env_pairs() == []

    def test_bootstrap_content_contract(self) -> None:
        content: str = _WSL_MOD._TOOLCHAIN_BOOTSTRAP_SH
        # 工具链 home 指向发行版持久 home（按 $HOME 展开）
        assert 'export RUSTUP_HOME="$HOME/.rustup"' in content
        assert 'export CARGO_HOME="$HOME/.cargo"' in content
        # PATH 追加语义（env 前缀做不了追加，只能引导文件内 export）：
        # ~/.cargo/bin 即 $CARGO_HOME/bin（上行已导出）——agent 自装的
        # cargo/rustup 裸名可达
        assert 'export PATH="$CARGO_HOME/bin:$PATH"' in content

    def test_agent_installed_cargo_visible_to_later_tasks(self, tmp_path: Path) -> None:
        """持久化链完整性：BASH_ENV 引导（PATH 追加 $CARGO_HOME/bin）与
        RUSTUP_HOME/CARGO_HOME 直接 env 对锚定同一发行版持久 home
        （VHDX，跨任务/跨重启）——agent 运行中自装的 rustup/cargo 落持久层，
        后续任务零重装即可见。"""
        provider = _wsl_provider(tmp_path)
        provider._workspace_wsl = "/mnt/d/ws"
        provider._distro_home = "/home/u"
        pairs = provider._runtime_env_pairs()
        # 直接对把工具链标准路径钉到持久 home
        assert "RUSTUP_HOME=/home/u/.rustup" in pairs
        assert "CARGO_HOME=/home/u/.cargo" in pairs
        # BASH_ENV 引导文件按 $HOME 展开到同一锚点，PATH 追加自装 bin 面
        bootstrap: str = _WSL_MOD._TOOLCHAIN_BOOTSTRAP_SH
        assert 'export CARGO_HOME="$HOME/.cargo"' in bootstrap
        assert 'export PATH="$CARGO_HOME/bin:$PATH"' in bootstrap
        # 引导文件随任务运行时 env 注入（BASH_ENV 对），执行壳自动 source
        assert any(p.startswith("BASH_ENV=") for p in pairs)

    def test_ensure_distro_home_probes_once_and_caches(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        recorder = _Recorder(routes=[(lambda j: "printf" in j, (0, b"/home/u", b""))])
        provider._popen_run = recorder  # type: ignore[method-assign]
        assert _run(provider._ensure_distro_home()) == "/home/u"
        assert _run(provider._ensure_distro_home()) == "/home/u"
        assert len(recorder.calls) == 1  # 缓存生效，只探测一次

    def test_ensure_distro_home_failure_cached_as_empty(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        recorder = _Recorder(routes=[(lambda j: "printf" in j, (1, b"", b"boom"))])
        provider._popen_run = recorder  # type: ignore[method-assign]
        assert _run(provider._ensure_distro_home()) == ""
        assert provider._distro_home == ""  # 失败按空缓存（不反复重试）

    def test_ensure_distro_home_exception_cached_as_empty(self, tmp_path: Path) -> None:
        """探测异常（wsl 假死/超时）不抛出，按空缓存——执行路径不被拖死。"""
        provider = _wsl_provider(tmp_path)
        provider._popen_run = _Recorder(routes=[(lambda j: "printf" in j, TimeoutError("wsl 超时"))])
        assert _run(provider._ensure_distro_home()) == ""
        assert provider._distro_home == ""

    def test_exec_path_injects_toolchain_pairs(self, tmp_path: Path) -> None:
        """端到端：创建 → 执行，执行 argv 的 env 层带 BASH_ENV + 直接对。"""
        provider = _wsl_provider(tmp_path)
        provider._popen_run = _Recorder(routes=[(lambda j: "printf" in j, (0, b"/home/u", b""))])
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-ws1"))
        assert env.status == EnvironmentStatus.READY.value
        result = _run(
            provider.execute_in_environment("cua-ws1", {"type": "command", "command": "echo hi", "timeout": 5})
        )
        assert result.success is True
        exec_argv = provider._popen_run.calls[-1]
        joined = " ".join(exec_argv)
        assert "BASH_ENV=" in joined
        assert ".task_runtime/toolchain_env.sh" in joined
        assert "RUSTUP_HOME=/home/u/.rustup" in joined
        assert "CARGO_HOME=/home/u/.cargo" in joined


# ── 6. 创建时种子写入（workspace 侧被动播种，失败不阻断）────


class TestCreateSeeds:
    def test_create_writes_bootstrap_into_task_runtime(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path, cargo_source_config=str(tmp_path / "none.toml"))
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-ws1"))
        assert env.status == EnvironmentStatus.READY.value
        seed_calls = [c for c in provider._popen_run.calls if "toolchain_env.sh" in " ".join(c)]
        assert seed_calls, "种子写入未被调用"
        assert "python3" in seed_calls[0]
        # 引导文件内容（PATH 追加语义）经 JSON argv 传递
        assert "CARGO_HOME/bin" in " ".join(seed_calls[0])

    def test_seed_failure_keeps_env_ready(self, tmp_path: Path) -> None:
        """workspace 侧种子（BASH_ENV 引导文件）失败不阻断环境创建（被动
        文件播种，工具链 env 退化为直接 env 对）——环境层零管理口径下，
        环境可用性不与任何工具链面耦合；留痕由 error 日志承载。"""
        provider = _wsl_provider(tmp_path)
        provider._popen_run = _Recorder(
            routes=[
                (lambda j: "mkdir" in j, (0, b"", b"")),
                (lambda j: "toolchain_env.sh" in j, (1, b"", b"OSError: readonly")),
            ]
        )
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-ws1"))
        assert env.status == EnvironmentStatus.READY.value

    def test_seed_exception_keeps_env_ready(self, tmp_path: Path) -> None:
        provider = _wsl_provider(tmp_path)
        provider._popen_run = _Recorder(
            routes=[
                (lambda j: "mkdir" in j, (0, b"", b"")),
                (lambda j: "toolchain_env.sh" in j, TimeoutError("wsl 超时")),
            ]
        )
        env = _run(provider.create_environment(_wsl_ctx(provider._workspace), "cua-ws1"))
        assert env.status == EnvironmentStatus.READY.value


# ── 7. docker_provider 仓库根解析（触碰即清的活 bug 回归钉）──


class TestRepoRootResolution:
    def test_repo_root_is_real_repo_root(self) -> None:
        provider = _docker_provider()
        assert provider._repo_root == _REPO_ROOT
        # 生产配置用相对 dockerfile_path——必须解析到真实存在的 Dockerfile
        assert (provider._repo_root / "docker_assets/agentos/Dockerfile").is_file()
