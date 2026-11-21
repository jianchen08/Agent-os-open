#!/usr/bin/env python3
"""装机应用直覆盖增量更新（开发版 → 装机位，免重打包）。

2026-11-19 首轮实跑沉淀（踩坑→固化序，详见 docs/working/装机直覆盖更新_台账.md）：

  ① 应用必须先关（弹窗确认即可，进程在就拒绝/ kill）——覆盖半开状态会撞文件锁
     与验证状态半写；
  ② 覆盖序 = 插件 → 配置 → 内核 exe → **最后**重生成 deploy-manifest.json。
     顺序颠倒必踩「清单哈希漂移」拒启（实锤：先生成清单再覆盖 exe/插件 →
     启动报 3 项关键文件 mismatch）；
  ③ deploy-manifest 重生成后必须剔除 loader 受管目录（`.mcp-managed` 标记，
     如 mcp_windows_mcp）——它们由内核 loader 按 config/mcp.json 每轮再生/清除，
     存在性随启动状态漂移，进清单 = 永久间歇性 mismatch（实锤两轮）；
  ④ 内核 exe 覆盖用「改名腾位」：运行中映像跟随改名文件，新件落到原路径
     （Windows 允许 rename 运行中 exe，不允许 overwrite）；
  ⑤ 装机启动 fail-closed 面有三道，逐道核对：
     - 部署回读闸（deploy-verify）：哈希不符弹「安装位与发布清单不符」→ 回到 ②③；
     - 服务依赖解析（requires_services）：点分命名空间须内核
       entry_satisfied 三形态并容（commit 4c5458beaa，ADR
       2026-11-19-service-declaration-dotted-ns 运行时口径）——老内核 exe 配新
       插件声明 = 启动即 exit 1；
     - manifest schema 闸：老内核 exe 不认新 manifest 字段（如 visibility）→
       插件被 skip → 下游管道编译失败。内核 exe 必须先于/伴随插件升级；
  ⑥ 起后验证：进程存活 + 装机内核日志「Discovered N plugin manifests」+
     /health 200 + 无「服务依赖解析失败 / unknown field」。

用法：
  python scripts/overlay_installed_update.py                    # 默认安装位
  python scripts/overlay_installed_update.py --install-root D:\\agentos-r297-main
  python scripts/overlay_installed_update.py --skip-launch      # 只覆盖不启动

环境：Windows（robocopy）；开发仓 = 本脚本所在仓；Python 原生（无 MSYS）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INSTALL_ROOT = Path(r"D:\agentos-r297-main")
APP_EXE_NAME = "灵汐助手.exe"
KERNEL_REL = Path("kernel/agentos-kernel.exe")
MANIFEST_REL = Path("deploy-manifest.json")
PREV_MANIFEST_REL = Path("deploy-manifest.prev.json")
#: loader 受管目录判定：plugin.json 同级存在 .mcp-managed 标记文件即受管
MANAGED_MARKER = ".mcp-managed"
ROBOCOPY_EXCLUDE_DIRS = [".venv", "__pycache__", "target", ".pytest_cache", "node_modules", ".ruff_cache"]
HEALTH_TIMEOUT_S = 5
BOOT_WAIT_S = 120

# 发现缓存摘要面外的运行期/开发期写入目录（2026-11-20 启动优化 A 口径，两侧
# 同律——kernel 侧 walk_face/is_runtime_dir_name 与 deploy/materialize_seed.py
# 同规则）：.venv*（venv 供给）、__pycache__（解释器字节码）、.audit_tmp（运行
# 期审计暂存）、data（插件运行数据）、tests（开发面）。装机面此类文件可达数十
# 万，登记进缓存会让 boot 走查全爬；真实源码（plugin.json/*.py）照常入摘要。
RUNTIME_FACE_DIRS = ("__pycache__", ".audit_tmp", "data", "tests")
VENV_DIR_PREFIX = ".venv"
PYC_SUFFIX = ".pyc"


def is_runtime_face_artifact(rel: str) -> bool:
    """插件面相对路径是否在发现摘要面外（任一路径段为运行期/开发期写入目录，
    或 basename 以 .pyc 结尾）。"""
    segs = rel.split("/")
    if segs[-1].endswith(PYC_SUFFIX):
        return True
    return any(
        seg in RUNTIME_FACE_DIRS or seg.startswith(VENV_DIR_PREFIX) for seg in segs
    )


def build_face_discovery_cache(face: Path) -> dict:
    """烤制插件面发现缓存 dict（schema 对齐 plugin-loader loader/cache.rs
    DiscoveryCacheFile；字节确定性由调用方 json.dumps(sort_keys) 保证）。
    摘要面外条目（is_runtime_face_artifact）不入册。"""
    files_map: dict[str, str] = {}
    links_map: dict[str, str] = {}
    plugin_dirs: dict[str, str] = {}
    for item in sorted(face.rglob("*")):
        rel = item.relative_to(face).as_posix()
        if rel == "discovery-cache.json":
            continue
        if is_runtime_face_artifact(rel):
            continue
        if item.is_symlink():
            links_map[rel] = item.readlink().as_posix()
            continue
        if item.is_dir():
            if (item / "plugin.json").is_file():
                plugin_dirs[rel] = "plugin.json"
            continue
        files_map[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
    return {
        "version": 1,
        "generator": "overlay_installed_update.py",
        "face_root": str(face),
        "files": files_map,
        "links": links_map,
        "plugin_dirs": {str(face): dict(sorted(plugin_dirs.items()))},
    }


def log(msg: str) -> None:
    print(f"[overlay-update] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"FAIL: {msg}")
    sys.exit(code)


def run(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    return proc


def robocopy(src: Path, dst: Path, *, extra_xd: list[str] | None = None,
             mirror: bool = False, list_only: bool = False) -> bool:
    """真增量覆盖（新/异才拷），返回「是否存在待拷变更」。

    - 缺省无 /IS /IT：内容相同不拷——rc bit0=有拷贝（=有变更）；
    - list_only=True 加 /L：干跑只列待拷清单不落盘（对运行中实例零副作用的比对通道）；
    - mirror=True 加 /MIR：纯生成物目录（前端 dist）镜像删陈旧哈希块，
      源须为构建输出（调用方守卫新鲜度）；
    - rc >= 8 = 失败（多为文件锁：有实例复活占用）。
    """
    cmd = ["robocopy", str(src), str(dst), "/MIR" if mirror else "/E",
           "/NFL", "/NDL", "/NJH", "/NP", "/R:1", "/W:1"]
    if list_only:
        cmd.append("/L")
    for d in ROBOCOPY_EXCLUDE_DIRS + (extra_xd or []):
        cmd += ["/XD", d]
    proc = run(cmd, timeout=1800)
    if proc.returncode >= 8:
        raise RuntimeError(
            f"robocopy {src} -> {dst} 失败（rc={proc.returncode}，多为文件锁：有实例复活）:\n{proc.stdout[-2000:]}"
        )
    return bool(proc.returncode & 1)


def app_running() -> bool:
    proc = run(["tasklist", "/FI", f"IMAGENAME eq {APP_EXE_NAME}"])
    return APP_EXE_NAME in proc.stdout


def kill_app(install_root: Path) -> None:
    """杀应用 electron 树 + 装机内核（按路径过滤，不伤 dev/TB 实例），并等其退净。

    教训（2026-11-19 二轮实证）：只杀 electron 后装机内核自愈重生（13184），
    文件锁导致 robocopy 失败——内核必须按安装位路径点名清除并等待退净。
    """
    run(["taskkill", "/IM", APP_EXE_NAME, "/F"])
    # 装机内核按路径点名（Get-Process 过滤 Path 前缀），全树强杀
    ps = (
        "Get-Process agentos-kernel -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Path -like '{install_root}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }"
    )
    run(["powershell", "-NoProfile", "-Command", ps])
    deadline = time.time() + 15
    while time.time() < deadline:
        if not app_running() and not install_kernel_running(install_root):
            time.sleep(2)
            return
        run(["powershell", "-NoProfile", "-Command", ps])
        time.sleep(2)
    if app_running() or install_kernel_running(install_root):
        die("应用/装机内核 15s 未退净（自愈复活过快），重跑或手工关闭")


def install_kernel_running(install_root: Path) -> bool:
    proc = run([
        "powershell", "-NoProfile", "-Command",
        "(Get-Process agentos-kernel -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Path -like '{install_root}*' }} | Measure-Object).Count",
    ])
    return proc.stdout.strip() not in ("", "0")


def overlay_and_manifest(install_root: Path, *, build_frontend: bool = False) -> None:
    """覆盖全流程带重试：文件锁多为实例自愈复活，杀净重拷 3 试内必赢窗口。"""
    last_err: RuntimeError | None = None
    for attempt in range(1, 4):
        try:
            _overlay_once(install_root, build_frontend=build_frontend)
            return
        except RuntimeError as exc:
            last_err = exc
            if attempt < 3:
                log(f"覆盖第 {attempt} 次失败（文件锁），重杀一轮重试 …")
                kill_app(install_root)
    die(str(last_err))


def _overlay_once(install_root: Path, *, build_frontend: bool = False) -> None:
    resources = install_root / "resources"
    if not resources.is_dir():
        die(f"安装位 resources 不存在: {resources}")

    # ① 插件面（排除开发专用目录；受管目录照拷——其存在性由 loader 裁决）
    log("覆盖 plugins/shared …")
    robocopy(REPO_ROOT / "plugins" / "shared", resources / "plugins" / "shared")

    # ② 配置面
    log("覆盖 config …")
    robocopy(REPO_ROOT / "config", resources / "config")

    # ②.5 前端 dist 面（UI 更新到装机的通道）：构建新鲜度守卫——src 比 dist 新
    # 则拒绝（陈旧 dist 镜像会倒删装机新产物），--build-frontend 可就地构建。
    dist = REPO_ROOT / "frontend" / "dist"
    src_root = REPO_ROOT / "frontend" / "src"
    if not dist.is_dir() or not (dist / "index.html").is_file():
        need_build = True
    else:
        dist_mtime = max(f.stat().st_mtime for f in dist.rglob("*") if f.is_file())
        src_mtime = max((f.stat().st_mtime for f in src_root.rglob("*") if f.is_file()), default=0)
        need_build = src_mtime > dist_mtime
    if need_build:
        if not build_frontend:
            die("frontend/dist 缺失或比 src 旧——先 npm run build --prefix frontend，或加 --build-frontend")
        log("构建前端（npm run build --prefix frontend）…")
        # Windows 原生 Python 的 subprocess 不解析 .cmd 垫片（npm 裸名 WinError 2）
        proc = run(["cmd", "/c", "npm", "run", "build", "--prefix", str(REPO_ROOT / "frontend")], timeout=1800)
        if proc.returncode != 0:
            die("前端构建失败:\n" + proc.stdout[-1500:] + proc.stderr[-800:])
    log("覆盖 frontend/dist …")
    robocopy(dist, resources / "frontend" / "dist", mirror=True)

    # ③ 内核 exe（改名腾位：运行中映像跟随改名文件；备份带时间戳）
    src_exe = REPO_ROOT / "kernel" / "target" / "release" / "agentos-kernel.exe"
    dst_exe = resources / KERNEL_REL
    if not src_exe.is_file():
        die(f"仓内内核 exe 缺失（先 cargo build --release --bin agentos-kernel）: {src_exe}")
    dst_exe.parent.mkdir(parents=True, exist_ok=True)
    if dst_exe.exists():
        # 备份名唯一化：同分钟重跑/残留同名不撞（FileExistsError 实证）
        base = time.strftime('%m%d_%H%M%S')
        backup = dst_exe.with_name(f"agentos-kernel.exe.old-{base}")
        n = 1
        while backup.exists():
            backup = dst_exe.with_name(f"agentos-kernel.exe.old-{base}-{n}")
            n += 1
        dst_exe.rename(backup)
        log(f"旧内核改名: {backup.name}")
    dst_exe.write_bytes(src_exe.read_bytes())
    log(f"内核 exe 覆盖: {dst_exe}（{dst_exe.stat().st_size}B）")

    # ④ 清单重生成（必须在全部覆盖之后——顺序颠倒=哈希漂移拒启）
    prev = resources / MANIFEST_REL
    if prev.exists():
        (resources / PREV_MANIFEST_REL).write_bytes(prev.read_bytes())
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build_head = run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip()
    if not build_head:
        die("git rev-parse HEAD 失败（清单 build_head 不可缺）")
    proc = run([
        sys.executable, str(REPO_ROOT / "deploy" / "verify_deploy_manifest.py"), "build",
        "--root", str(resources), "--out", str(resources / MANIFEST_REL),
        "--profile", "windows", "--version", version, "--build-head", build_head,
    ], timeout=1800)
    if proc.returncode != 0:
        die(f"清单生成失败:\n{proc.stdout[-1500:]}{proc.stderr[-500:]}")

    # ⑤ 剔除 loader 受管目录条目（存在性随启动漂移，进清单=间歇性 mismatch）
    manifest_path = resources / MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files: dict[str, dict] = manifest.get("files", {})

    def is_managed(rel_key: str) -> bool:
        return (resources / rel_key).parent.joinpath(MANAGED_MARKER).exists() or f"/{MANAGED_MARKER}" in rel_key

    removed = [k for k in list(files) if is_managed(k)]
    for k in removed:
        del files[k]
    if "critical" in manifest:
        manifest["critical"] = [x for x in manifest["critical"] if not is_managed(x)]
    for count_key in ("file_count", "total_files"):
        if count_key in manifest:
            manifest[count_key] = len(files)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"清单就绪: {len(files)} files / {len(manifest.get('critical', []))} critical"
        f"（剔除受管 {len(removed)} 项: {sorted({r.rsplit('/', 1)[0] for r in removed})}）")

    # ⑥ 烤制发现缓存（T4/R-2 层 2「不变不重来」的桌面接线）：种子未变=免扫描+
    # 免 G2 spawn 复验（cache_verified）。盘上任何缓存外文件=回退全量（无害但
    # 加速失效），故必须穷举全脸且放在覆盖完全结束之后。schema 对齐
    # plugin-loader loader/cache.rs DiscoveryCacheFile + build_discovery_cache。
    face = resources / "plugins" / "shared"
    cache_path = face / "discovery-cache.json"
    cache = build_face_discovery_cache(face)
    cache_path.write_text(json.dumps(cache, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
                          encoding="utf-8")
    log(f"发现缓存烤制: {len(cache['files'])} files / "
        f"{len(next(iter(cache['plugin_dirs'].values())))} plugin dirs（不变不重来；"
        f"运行期/开发期写入面 .venv*/__pycache__/.audit_tmp/data/tests 摘要面外）")


def launch_and_verify(install_root: Path) -> str:
    exe = install_root / APP_EXE_NAME
    subprocess.Popen([str(exe)], cwd=str(install_root))
    log(f"已拉起 {exe.name}，等待内核 …")
    deadline = time.time() + BOOT_WAIT_S
    kernel_pid = None
    while time.time() < deadline and not kernel_pid:
        time.sleep(10)
        # ExecutablePath 含安装位根 = 本次覆盖的内核（与 dev/其他实例区分）
        proc = run([
            "powershell", "-NoProfile", "-Command",
            "(Get-Process agentos-kernel -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Path -like '" + str(install_root) + "*' }).Id",
        ])
        kernel_pid = proc.stdout.strip()
    if not kernel_pid:
        die(f"{BOOT_WAIT_S}s 内未见装机内核拉起（部署回读闸/依赖解析弹窗见屏幕；"
            f"日志 {install_root / 'resources' / 'kernel' / 'logs'}）")
    log(f"装机内核 pid={kernel_pid}")

    # /health 探活（装机内核端口 9101；从日志监听行解析以防端口变更）
    log_path = install_root / "resources" / "kernel" / "logs" / f"kernel.log.{time.strftime('%Y-%m-%d')}"
    port = "9101"
    if log_path.exists():
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if "监听地址" in line and "http://127.0.0.1:" in line:
                port = line.rsplit(":", 1)[1].strip()
                break
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200 and '"ok"' in body:
                    log(f"health OK @ :{port} {body[:80]}")
                    return port
        except Exception:  # noqa: BLE001 —— 启动期探活失败属预期，轮询
            time.sleep(5)
    die(f"/health 不绿 @ :{port}（内核起了但未就绪，查 {log_path}）")
    return port


def validate_all(install_root: Path, port: str) -> None:
    """G2 契约复验收口：覆盖必然改变插件字节 → 必须触发 validate-all。

    教训（2026-11-19 三轮实证）：缺此步时启动期 G2 校验未完成的插件被
    TOOL_TEMPORARILY_UNAVAILABLE 临时拦截（bash_tool 实锤），业务侧首用即报错。
    drifted/error 记 WARN 不拦更新（design_generate_mcp 等既有 disabled 件
    恒 error，属存量噪声；drifted 多为 HEAD 快照落后一拍，下轮自然收敛）。
    """
    env_path = Path.home() / "AppData" / "Roaming" / "agentos" / ".env"
    password = ""
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("AGENTOS_ADMIN_PASSWORD="):
                password = line.split("=", 1)[1].strip()
                break
    if not password:
        log("WARN: user_root .env 无 AGENTOS_ADMIN_PASSWORD，跳过 validate-all（首用工具若报 TOOL_TEMPORARILY_UNAVAILABLE 请手动触发）")
        return
    # 登录重试退避：装机版开机窗口期 /auth/login 可能长时间无响应
    # （auth.rs 记载 6~14.5 分钟形态；health 正常但 login 挂起），5 次×60s 兜底。
    token = ""
    for attempt in range(1, 6):
        login = run([
            "curl", "-s", "-m", "30", "-X", "POST", f"http://127.0.0.1:{port}/api/v1/auth/login",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"username": "admin", "password": password}),
        ], timeout=40)
        try:
            token = json.loads(login.stdout).get("access_token", "")
        except ValueError:
            token = ""
        if token:
            break
        log(f"登录第 {attempt} 次未成（开机窗口期已知形态），60s 后重试 …")
        time.sleep(60)
    if not token:
        log("WARN: 登录 5 次未成，跳过 validate-all（可稍后手动 POST /api/v1/plugins/validate-all）")
        return
    proc = run([
        "curl", "-s", "-m", "600", "-X", "POST", f"http://127.0.0.1:{port}/api/v1/plugins/validate-all",
        "-H", f"Authorization: Bearer {token}",
    ], timeout=660)
    try:
        d = json.loads(proc.stdout)
        log(f"validate-all: checked={d.get('checked')} clean={d.get('clean')} "
            f"drifted={d.get('drifted')} errors={d.get('errors')}")
        if d.get("errors"):
            log(f"WARN errors 插件: {[r.get('plugin_id') for r in d.get('reports', []) if r.get('status') == 'error']}")
    except ValueError:
        log(f"WARN validate-all 响应不可解析: {proc.stdout[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="装机应用直覆盖增量更新（免重打包）")
    parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT), help="安装位根（缺省 %(default)s）")
    parser.add_argument("--skip-launch", action="store_true", help="只覆盖不拉起（人工启动验收）")
    parser.add_argument("--kill-app", action="store_true", help="应用在跑时先强杀（默认拒绝执行）")
    parser.add_argument("--build-frontend", action="store_true",
                        help="frontend/dist 缺失或比 src 旧时就地构建（默认拒绝，要求先手工构建）")
    args = parser.parse_args()
    install_root = Path(args.install_root)

    if app_running():
        if not args.kill_app:
            die(f"{APP_EXE_NAME} 在运行——先关闭应用再覆盖（或加 --kill-app）")
        log("强杀运行中应用 …")
        kill_app(install_root)

    overlay_and_manifest(install_root, build_frontend=args.build_frontend)

    if not args.skip_launch:
        port = launch_and_verify(install_root)
        validate_all(install_root, port)
        log("═══ 直覆盖更新完成：覆盖 + 清单 + 启动 + 探活 + G2 复验 全绿 ═══")
    else:
        log("═══ 覆盖完成（--skip-launch，人工启动验收）═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
