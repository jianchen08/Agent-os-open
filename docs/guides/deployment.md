# 部署指南（0.2）

> 适用对象：把 AgentOS 0.2（Rust 微内核 + Python 插件 + React 前端）部署到
> 生产/常驻环境。所有环境变量名、默认值、文件路径均按当前代码逐一核实
> （来源标注在表中）。架构基线见 `docs/ARCHITECTURE.md`。

## 一、生产拓扑

```
┌────────────────────────────── 宿主机（或容器宿主） ──────────────────────────────┐
│                                                                                  │
│  agentos-kernel（Rust，单进程）           前端（静态产物 frontend/dist）             │
│  ├─ HTTP/WS API  127.0.0.1:9100           ├─ 生产：nginx 同源反代（deploy/nginx.conf）│
│  ├─ /health /api/v1/* /ws /metrics        │   开发：Vite dev server + 本机代理       │
│  ├─ tracing 日志（stdout + 文件双写）      └─ 浏览器只见反代一个源（连接两形态见下）  │
│  │                                                                                 │
│  ├─ 插件 sidecar（按需 spawn）────── plugins/shared/**/.venv（每插件独立 venv）      │
│  │    Python 子进程，stdout=JSON-RPC 通道，stderr=日志（内核转发进统一 sink）          │
│  │                                                                                │
│  └─ SQLite 库（默认项目根 agentos_kernel.db，WAL 模式，-wal/-shm 伴生）               │
│                                                                                   │
│  docker-compose.yml 仅承载 Redis（可选依赖，宿主 6480 → 容器 6379，                  │
│  参数化 REDIS_HOST_PORT，默认 6480）——内核与前端均跑宿主机，无前端/反代容器。          │
└───────────────────────────────────────────────────────────────────────────────────┘
```

要点：

- **内核是唯一常驻服务**，一切能力（LLM/工具/记忆/评估）由插件承载，内核按需
  spawn/回收插件 sidecar（空闲软卸载 GC）。
- 前端接入两形态：开发 = Vite dev server 代理（`VITE_PROXY_TARGET`），生产 =
  nginx **同源反代**（`deploy/nginx.conf`）——详见下文「前端连接两形态」；
  仅跨源直连才需要 `AGENTOS_CORS_ORIGINS` 白名单（见下表），内核对本地源
  （localhost/127.0.0.1/[::1] 任意端口）恒放行。
- 插件 sidecar 依赖各插件目录下的 `.venv`（uv 管理），**必须随代码一起部署**
  （见构建步骤第 3 步），内核不会回退到裸 `python`（fail-closed）。

### 前端连接两形态

| 形态 | 接入方式 | 配置面 |
|---|---|---|
| 开发调试 | 本机 `VITE_PROXY_TARGET=http://<server>:<AGENTOS_KERNEL_PORT>` + `pnpm dev`，Vite 把 `/api` `/ext` `/ws` `/media` `/uploads` 代理到内核 | `localhost` 源 CORS 免配置（内核对本地源恒放行）、WS ticket 免配置 |
| 生产 | 服务器 nginx **同源反代**：`deploy/nginx.conf`——静态 dist + SPA try_files 回退 + `/api` `/ext` `/ws` `/media` `/uploads` 反代 `127.0.0.1:<AGENTOS_KERNEL_PORT>`，TLS/wss 终结见文件内注释段 | 浏览器只见一个源，CORS/WS 免配置；前端 API 基址缺省空串走相对路径，**零重打包** |

**红线：不要用 `VITE_API_BASE_URL` 跨源直连内核**（如把 `http://<server>:9100`
烧进构建）：

1. dev CSP `connect-src 'self'` 拦截 http 跨主机请求（`frontend/vite.config.ts`）；
2. 附件/图片前端按同源相对路径取用，跨源基址下这些请求打到静态服务器必 404。

`start_web_02.sh` 已把该变量移出注入面（换 `VITE_PROXY_TARGET`，提交
4cde480bd）——生产构建保持基址为空 + 反代同源，是唯一不踩这两条死路的形态。

### 浏览器形态功能退化清单（知情面）

桌面壳（electron）独有能力在纯浏览器形态（远程服务器接入）如实缺失。属形态
差异非缺陷，逐项列出供部署方与用户知情：

- **窗口控制条消失**（最小化/最大化/关闭自绘条为桌面壳专属）；
- **系统通知降级应用内浮层**；
- **RJSF 目录选择器不渲染**（依赖本地文件系统访问，无浏览器等价物）；
- **进程指标显示「—」**（宿主进程采样面无数据源）；
- **「在宿主打开」在无头服务器如实失败**（服务器无桌面环境，工具报错而非静默）。

替代交互（下载/复制路径等）为后续立项（Linux 部署总方案批次 E）。

### 隔离后端选择（Windows = wsl_native，Linux = linux_native，Docker = 显式回退）

隔离后端按部署机器平台在**装载期自动选择**（ADR
[2026-10-09-isolation-backend-platform-adaptive-selection](../decisions/2026-10-09-isolation-backend-platform-adaptive-selection.md)、
[2026-10-12-linux-native-isolation-default](../decisions/2026-10-12-linux-native-isolation-default.md)），
零手工配置：

- **Windows**：`wsl_native`（`wsl_native.enabled: true` 收敛后的语义 = Windows
  上启用 wsl_native 为 CONTAINER 后端），隔离边界 = WSL2 VM；WSL 缺失时维持
  既有 fail-closed。
- **Linux 服务器**：`linux_native`（默认后端，`providers.linux_native.enabled`
  缺省 true），环境 = workspace 目录 + 一次性 bwrap 沙箱进程（根只读视图 +
  workspace 同路径 rw-bind + 独立 proc/dev/tmpfs/pid ns + 默认禁网），无常驻
  容器、无 daemon。服务器需装 **bubblewrap** 包（`apt install bubblewrap`）；
  bwrap 缺失时 fail-closed（错误消息带双出路指引），不静默回退。
- **Docker**：两种形态的消费——Linux 上显式 opt-out
  （`linux_native.enabled: false`）回 Docker；Windows 上 wsl_native 未启用时
  的兜底。走 Docker 需装 Docker daemon；隔离镜像 `agentos:latest` 首启
  auto_build（`auto_build: true`，dockerfile `docker/agentos/Dockerfile`，
  需 BuildKit）或预先构建/`docker pull` 就绪。

browser_* 通路差异：**wsl_native 档 v1 与 linux_native 档均未覆盖**容器内
浏览器 bridge（Windows 上浏览器任务走 cua 后端）；Linux 上需要容器内浏览器的
任务显式 opt-out 到 **Docker 后端原生覆盖**。

### config/mcp.json 的平台注意（Linux）

`config/mcp.json` 是全平台共享的外部 MCP 注册面。其中 `windows-mcp` 条目是
Windows GUI 自动化 MCP：**Linux 服务器建议从 `config/mcp.json` 移除
windows-mcp 条目（Windows GUI 自动化 MCP，Linux 无效且每次启动产生 spawn
失败日志）**。移除后热发现自动生效，受管插件目录 `mcp_windows-mcp` 会被
自动清理，无需重启内核。

不移除也不阻断部署：该条目在内核侧是零声明 dynamic MCP，spawn 失败走
「观测失败 ≠ 判定失败」降级路径——仅该 server 不可用（LLM 工具面无其工具），
其余 server 正常、内核正常启动；告警日志有界（G2 复验连续 5 轮失败后落
`verify_failed` 终态并停止自动重验）。追求干净启动日志与干净契约状态页
（`GET /api/v1/plugins/contract-status`）的服务器按上述建议移除即可。

## 二、构建步骤

> **CI 门（2026-10-28 裁定）**：部署产物构建/部署动作的前置 = 该 commit 的
> ci.yml + env-gate.yml 全绿（`.github/workflows/env-gate.yml` deploy-env
> 车道承载部署环境检测与 `deploy/build.sh` 容器构建实测）；
> `deploy/release_all.sh` preflight 已机械执法（scripts/check_ci_gate.py，
> 未绿/在跑/未跑过均拦）。

### 极简部署四步（脚本化路径，已实测）

2026-10-09 在 WSL2（ext4）上按本节四步全流程实测通过：干净 clone → 容器编译
内核 + 3 个 cdylib → venv 供给 → 裸起 → nginx 容器 → Windows 浏览器远程使用，
链路直达对话层。适合无 Rust/Node 环境的 Linux 部署机；手工等价步骤见本节
后续内容。

```bash
# ① 源码：干净 clone 到部署机【本地文件系统】（红线见本节末尾）
git clone <repo-url> /opt/agentos && cd /opt/agentos

# ② 编译（容器化，宿主机无需 Rust 工具链）：内核 + tool_core/sensitive_checker/
#    spill_guard 三个 cdylib .so（必须是 Linux 侧编译产物，Windows .dll 不被消费）
bash deploy/build.sh

# ③ venv 供给 + 环境配置：_host 共享 venv + 独占宿主插件逐个 uv sync
bash deploy/init.sh
sudo install -d -m 0755 /etc/agentos
sudo install -m 0600 deploy/agentos.env.example /etc/agentos/agentos.env  # 填两把密钥等必设项

# ④ 常驻启动：systemd（安装 deploy/agentos-kernel.service）或裸起
sudo systemctl enable --now agentos-kernel    # 或：bash deploy/start_server.sh

# 前端：nginx 容器同源反代（dist 仍需 Node 构建一次，见下文第 2 步，
# 可在任意机器构建后同步过来；挂载点对齐 nginx.conf 的 root，无需改 conf）
docker run -d --name agentos-web --restart unless-stopped --network host \
  -v "$PWD/frontend/dist":/opt/agentos/frontend/dist:ro \
  -v "$PWD/deploy/nginx.conf":/etc/nginx/conf.d/default.conf:ro nginx:1.27
```

- **8080 端口 sed 说明**：`--network host` 下容器共享宿主网络栈，
  `nginx.conf` 内 `proxy_pass http://127.0.0.1:9100` 原样可达内核，对外端口由
  `listen` 决定（缺省 80）。宿主 80 被占或需钉 8080 时，起容器前
  `sed -i 's/listen 80;/listen 8080;/' deploy/nginx.conf`；不要改用 `-p 8080:80`
  端口映射——bridge 网络里容器内 `127.0.0.1` 不指宿主，反代必断。
- **实测红线（已固化进脚本与 `deploy/agentos.env.example` 注释）**：
  - venv 必须逐插件 sync（`deploy/init.sh`）：内核 autoprovision 只建 venv 壳
    不装插件依赖（实测 spawn 即 pydantic missing）；host_group 合宿成员除外——
    经 `_host` 共享 venv spawn，自身 .venv 零消费。
  - `AGENTOS_ADMIN_PASSWORD` 常驻 env 时每次启动口令不符即强制重置（UI 改密
    被冲回）——改密后撤掉该变量，遗忘口令时再临时加回重启重置。
  - 空闲 300s 软卸载会让独立 sidecar 工具从 LLM 工具面脱落，模板建议
    `AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=86400`。
  - DB 与仓库必须在**本地文件系统**：网络盘上 SQLite 锁坏死（disk I/O error）。

### 手工构建（脚本化路径的等价展开）

前置：Rust 1.88（`rust-toolchain.toml` 钉版 1.88.0）、Python 3.12 +
[uv](https://docs.astral.sh/uv/)（插件 venv 统一经 uv 锁定构建，见第 3 步）、
git（源码 checkout 部署）、Node 22 + pnpm（前端；根目录 npm 工具链仅桌面壳
electron 需要）、**bubblewrap**（Linux 隔离默认后端依赖，`apt install
bubblewrap`；显式 opt-out 回 Docker 时才需要 Docker daemon + 隔离镜像
auto_build/`docker pull`，dockerfile `docker/agentos/Dockerfile` 需 BuildKit，
见「隔离后端选择」）。

```bash
# 1. 内核（Linux 产物 agentos-kernel；Windows 产物 agentos-kernel.exe）
cd kernel && cargo build --release --bin agentos-kernel

# 2. 前端静态产物（frontend/dist）
cd frontend
pnpm install --frozen-lockfile
npm run build          # = vite build

# 3. 插件 venv（全部 uv.lock 锁定构建，--frozen 禁止在线重解析）
uv python install 3.12
for lock in $(git ls-files 'plugins/**/uv.lock' | sort -u); do
  uv sync --frozen --project "$(dirname "$lock")"
done
```

发布产物（tag `v*` 触发 `.github/workflows/release.yml`）：Python sdist+wheel、
`agentos-kernel-linux-x86_64.tar.gz`（入 Release 前有 /health 冒烟）、
`agentos-kernel-windows-x86_64.zip`（编译+产物存在校验）。

> 二进制可独立分发，但**必须保留仓库布局**（或显式设环境变量）：插件目录
> `plugins/shared`、配置根 `config/` 默认按二进制位置向上推导（编译期
> `CARGO_MANIFEST_DIR` 基准），相对路径基准是**进程工作目录**。

## 三、关键环境变量

优先级约定：环境变量 > `config/kernel/storage.yaml` > 内置默认（存储域，见
`kernel/crates/engine/src/storage_factory.rs`）。

| 变量 | 作用 | 默认 | 来源 |
|---|---|---|---|
| `AGENTOS_USER_ROOT` | **用户空间根**：用户可写资产（插件/配置/数据/密钥）统一住这里，整体位于仓库之外——仓内 `config/`、`data/` 处于工作区还原的抹除风险面内，用户空间不受影响。 | OS 标准数据目录下 `agentos/`（Windows `%APPDATA%`、macOS `~/Library/Application Support`、Linux `$XDG_DATA_HOME`） | `kernel/crates/core/src/user_space.rs`（`user_root`）；ADR `docs/decisions/2026-09-13-unified-user-root.md` |
| `AGENTOS_USER_CONFIG_DIR` | 用户配置层根（分区覆盖；镜像 factory `config/` 的相对路径） | `<USER_ROOT>/config` | 同上（`user_config_dir`） |
| `AGENTOS_DATA_DIR` | 用户数据根（多租户树 / uploads / DB 默认位） | `<USER_ROOT>/data` | 同上（`user_data_dir`） |
| `AGENTOS_DB_PATH` | SQLite 库文件路径；`:memory:` = 内存库 | `<USER_ROOT>/data/agentos_kernel.db`（用户空间不可得时回落项目根） | `storage_factory.rs`（`ENV_DB_PATH`） |
| `AGENTOS_STORAGE_DRIVER` | 存储驱动：`sqlite` / `memory`（`postgres` 留桩，显式报错） | `sqlite` | `storage_factory.rs`（`ENV_STORAGE_DRIVER`） |
| `AGENTOS_KERNEL_PORT` | 内核监听端口 | `9100` | `kernel/crates/api/src/bin/agentos-kernel.rs` |
| `AGENTOS_BIND` | 监听地址；默认仅本机，设 `0.0.0.0` 显式开外网（启动打印告警） | `127.0.0.1` | 同上（`resolve_bind_host`） |
| `AGENTOS_KERNEL_HOST` | **弃用中**的监听地址别名（过渡期仍采纳，打弃用告警） | 未设 | 同上 |
| `AGENTOS_CORS_ORIGINS` | 生产 CORS 白名单：逗号分隔的**完整 origin**，精确匹配（无子域/前缀模糊） | 未设 = 仅本地源放行 | `kernel/crates/api/src/server.rs`（`origin_matches_allowlist`） |
| `AGENTOS_TOKEN_SECRET` | token 签名密钥；**未设 = 进程随机，重启即全量会话失效** | 未设（随机） | `kernel/crates/http/src/auth.rs`（`TOKEN_SECRET_ENV`） |
| `AGENTOS_ADMIN_PASSWORD` | 内置 admin 口令（播种口令唯一事实源，不置 `must_change_password`）；未设 = 首启生成随机口令并**仅打印一次**（置 `must_change_password`，首登强制改密），置非空值可重置已有 admin 口令 | 未设（随机） | `bin/agentos-kernel.rs`（`resolve_admin_password`） |
| `AGENTOS_PLUGINS_DIR` | 内置插件根目录（只读） | `<项目根>/plugins/shared` | 同上 |
| `AGENTOS_USER_PLUGINS_DIR` | 用户插件根目录（可写，第三方插件安装位；同 id 覆盖内置根） | `<USER_ROOT>/plugins` | 同上（`resolve_user_plugins_dir`） |
| `AGENTOS_CONFIG_ROOT` | 工厂配置根目录（只读基线，用户层优先见上行 `AGENTOS_USER_CONFIG_DIR`）。**装机形态例外**：electron 每次 spawn 内核都显式注入为用户空间配置根 `<USER_ROOT>/config`（包内 `resources/config` 降级为首次播种源）——配置真值可写、升级不丢，且按路径直读配置的插件能真正读到用户改的值 | `<项目根>/config`（装机：`<USER_ROOT>/config`） | 同上；ADR `docs/working/2026-10-17-installed-config-root-user-space-adr-draft.md`（待评审） |
| `AGENTOS_DB_AUTO_REBUILD` | `=1` 显式允许损坏库自动备份后重建空库继续启动；**默认坏库 fail-closed 拒启** | 未设（拒启） | `kernel/crates/engine/src/store.rs`；ADR `docs/decisions/2026-09-11-corrupt-db-fail-closed.md` |
| `AGENTOS_ALLOW_EMPTY_PLUGINS` | `=1` 插件 discover 失败时以空插件集启动（嵌入式/最小化部署逃生门） | 未设（discover 失败拒启） | `bin/agentos-kernel.rs` |
| `AGENTOS_GRANTS_STRICT` | `=1` 插件未声明 `granted_capabilities` 时反向能力调用一律拒绝 | 未设（未声明默认全授予） | 同上 |
| `AGENTOS_MAX_BLOCKING_THREADS` | tokio 阻塞池上限；未设时按设备档/核数推导：phone `2×核`→[8,24]、desktop `4×核`→[32,96]、cloud `4×核`→[32,128] | 推导值（桌面 16 核=64） | `kernel/crates/memory/src/lib.rs`（`BlockingPoolConfig::resolve`） |
| `AGENTOS_THREAD_KEEP_ALIVE_SECS` | 阻塞线程空闲保活；空闲即退休，退休页由周期 collect + `page_reclaim_on_free` 结算 | `300` | 同上 |
| `AGENTOS_MAX_INFLIGHT_TOOLS` | 全局同时执行中的工具调用上限（异步准入闸，不占线程；超出的调用排队挂 future）；未设时按设备档/核数推导：phone `2×核`→[8,32]、desktop `2×核`→[16,64]、cloud `2×核`→[16,96] | 推导值（桌面 16 核=32） | `kernel/crates/memory/src/lib.rs`（`resolve_max_inflight_tools`）+ `PluginInvokerImpl::with_tool_admission_limit` |
| `AGENTOS_MEM_COLLECT_SECS` | 后台内存清理周期（采样 mimalloc 统计 + `mi_collect(true)`） | `60` | `agentos-kernel` 内存维护循环（`BlockingPoolConfig::collect_interval`） |
| `AGENTOS_THREAD_STACK_KIB` | 线程栈大小 KiB | `2048` | 同上（`BlockingPoolConfig`） |
| `MIMALLOC_PAGE_RECLAIM_ON_FREE` | 线程退休后 abandoned 页跨 theap 认领；`-1` 完全禁止、`0` 仅本 theap | `1` | `kernel/crates/api/src/allocator.rs`（`install_global_allocator`，仅 env 未设时落值） |
| `AGENTOS_MEM_WATERMARK_MB` / `AGENTOS_MEM_WATERMARK_MIN` | 内存水位自愈（持续超限排空重启）；**默认关闭**，显式正值才启用 | 关 / 10 分钟 | 同上（`resolve_watermark_config`） |
| `RUST_LOG` | 内核 tracing EnvFilter（如 `info,agentos_mcp=debug`） | `info` | `tracing_subscriber::EnvFilter`；见 `docs/guides/logging.md` |

前端（`frontend/.env.example` → `.env.local`，构建期注入）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `VITE_API_BASE_URL` | 内核 API 基址；**跨源直连是红线（CSP + 附件相对路径双拦），见「前端连接两形态」**——生产同源反代保持空串走相对路径 | 空（相对路径）；`.env.example` 示例值 `http://127.0.0.1:9100` 仅本机开发用 |
| `VITE_WS_BASE_URL` | 内核 WS 基址（留空从 API 基址派生，http→ws） | 空（派生） |
| `VITE_PROXY_TARGET` | **开发期专用**：Vite dev server 代理目标（`pnpm dev` 时 `/api` `/ext` `/ws` `/media` `/uploads` 转发到此），缺省 `http://127.0.0.1:9100`；不进构建产物 | `http://127.0.0.1:9100`（`frontend/vite.config.ts`） |

> 前端连 `127.0.0.1` 而非 `localhost`：部分系统 `localhost` 先解析到 IPv6
> `::1`，内核仅监听 IPv4 时每请求先超时约 2s（见 `.env.example` 内注释）。
> 开发模式 `pnpm dev`（vite）默认端口 6390（`frontend/vite.config.ts`），
> 启动脚本/CI 冒烟以 `--port 5188` 覆盖。

### 用户空间（哪些东西不写回仓库）

用户在界面上做的改动默认**落在用户空间**，不覆写仓库里 git 跟踪的工厂文件
（ADR `docs/decisions/2026-09-13-unified-user-root.md`）：

```text
<USER_ROOT>/                 # 默认 <OS 数据目录>/agentos
├── plugins/                 # 用户插件（同 id 覆盖内置根，热发现秒级生效）
├── config/                  # 用户配置层（镜像工厂 config/ 的相对路径）
├── data/                    # 运行时数据（多租户树 / uploads / DB）
└── .env                     # 密钥与环境变量
```

- **覆盖语义 = 文件级整体替换**：用户层存在某文件时，工厂同路径文件完全不参与
  （不读取、不合并）——任一时刻一个路径只有一份生效文件。首次编辑某配置时内核
  先按工厂内容**播种**到用户层，用户拿到完整文件而非 diff 片段。
- **落用户空间的写面**：`PUT /api/v1/plugins/{id}/config/{file_id}`、
  `PUT /api/v1/config/pipelines/{name}`、`PUT /api/v1/plugins/{id}/enabled`、
  设置页的密钥写入（`.env`）。
- **内核保留文件仍不可由插件映射**：`plugin_allowlist` / `plugin_roots` / `auth` /
  `pipelines` / `steps` 的 denylist 对用户层与工厂层同样生效；这些文件的**合法编辑
  入口**只有内核自有的界面写面（管道配置页 / 插件启停），不是插件 manifest 映射。
- **升级不自动合并**：工厂后续新增的键不会静默并入用户层文件（静默合并 = 两处存值，
  与单真值 ADR 冲突）；需要新默认值时由用户在界面上重置该文件。
- **存量安装迁移**：默认库位置已从项目根迁到用户空间。若项目根仍有旧库而当前指向
  的库不是它，内核启动会打印一条携带迁移命令的告警（**不静默自动迁移**）：
  `python scripts/migrate_to_user_root.py`（幂等，支持 `--dry-run`）。

## 四、进程守护

### Linux systemd

**实物在 `deploy/`，勿再从文档抄写内嵌示例**：

- `deploy/agentos-kernel.service` —— systemd unit。安装 = 复制到
  `/etc/systemd/system/agentos-kernel.service`（示例按仓库落 `/opt/agentos`
  书写，路径按实际调整；内核二进制由 `cd kernel && cargo build --release`
  预先构建，即 ExecStart 指向的 `kernel/target/release/agentos-kernel`）。
  原理要点（保留）：`WorkingDirectory` 必须钉仓库根——插件目录/配置/日志均为
  相对路径基准，内核日志在 CWD 可写时落 CWD 相对 `logs/`，钉住即落
  `<repo>/logs/`，规避服务管理器下 CWD 漂移；`Restart=on-failure`；停机无需
  特殊配置——内核已内置 SIGTERM 优雅退出（drain + 逐 kill Python sidecar，
  总预算 2s）；stdout/stderr 交 journald 收口。
- `deploy/agentos.env.example` —— EnvironmentFile 模板（监听 / 路径必设三项
  `AGENTOS_PLUGINS_DIR`、`AGENTOS_CONFIG_ROOT`、`AGENTOS_USER_ROOT` / 密钥 /
  CORS / LLM keys 分节注释）。文件含明文口令，必须 0600：

```bash
sudo install -d -m 0755 /etc/agentos
sudo install -m 0600 deploy/agentos.env.example /etc/agentos/agentos.env
systemctl daemon-reload && systemctl enable --now agentos-kernel
journalctl -u agentos-kernel -f          # stdout 层日志
```

### Linux 裸起（无 systemd 环境）

`deploy/start_server.sh` —— 与 unit 等价的裸起脚本：env 注入面与
electron/kernel-manager.ts 同口径，部署差异为 `AGENTOS_BIND` 缺省 `0.0.0.0`
（远程可达）并显式钉 `AGENTOS_USER_ROOT`；**密钥必设**——
`AGENTOS_TOKEN_SECRET` / `AGENTOS_ADMIN_PASSWORD` 缺失直接报错退出（不静默
生成，理由见脚本内注释）；内置监督循环（内核请求重启的退出码 75 → 1s 重拉
不计失败；其它退出指数退避，连续 5 次熔断）；trap 转发 SIGTERM/SIGINT 随内核
退出。用法：设好两把密钥后 `bash deploy/start_server.sh`（日志落
`<repo>/logs/`，脚本 cd 仓库根）。

### Windows NSSM

```bat
nssm install AgentOSKernel "D:\agentos\kernel\target\release\agentos-kernel.exe"
nssm set AgentOSKernel AppDirectory D:\agentos
nssm set AgentOSKernel AppEnvironment "AGENTOS_BIND=127.0.0.1" "AGENTOS_DB_PATH=D:\agentos\data\agentos_kernel.db" "AGENTOS_TOKEN_SECRET=<change-me-long-random>" "AGENTOS_ADMIN_PASSWORD=<change-me>"
nssm set AgentOSKernel AppStdout D:\agentos\logs\kernel-stdout.log
nssm set AgentOSKernel AppStderr D:\agentos\logs\kernel-stderr.log
nssm set AgentOSKernel AppRotateFiles 1
nssm set AgentOSKernel AppRotateOnline 1
nssm start AgentOSKernel
```

> NSSM 自带轮转（AppRotateFiles）接管 stdout 重定向的尺寸问题；本仓开发态的
> `run_kernel_supervised.bat` 追加重定向（`.kernel_02.log`，无轮转）仅限本地
> 调试使用，生产勿直接复用。

## 五、备份与恢复

- **在线备份（推荐）**：SQLite `VACUUM INTO`（内核 db-admin 清理前快照同机制，
  见 `kernel/crates/db-admin/src/db_routes.rs`）：
  ```bash
  sqlite3 data/agentos_kernel.db "VACUUM INTO 'data/backup-$(date +%Y%m%d).db'"
  ```
  产出单文件一致性快照，可在内核运行时执行（事务外）。
- **冷备份**：停内核后把库三件（`agentos_kernel.db` + `-wal` + `-shm`）整体拷走。
- **清理前自动快照**：db-admin 全量清理执行数据前自动 `VACUUM INTO` 落
  `<库路径>.clear-backup-<毫秒ts>-<序号>`（失败即中止清理）；恢复 = 直接把备份
  文件改回库名（备份库含清前的 9 表数据）。
- **坏库现场（ADR 2026-09-11-corrupt-db-fail-closed）**：内核启动遇损坏类错误
  默认 **fail-closed 拒启**，现场已自动备份为 `<库路径>.corrupt-<ts>` 三件套，
  错误信息含备份路径。处置：离线用 `.corrupt-*` 备份排查恢复数据；确认弃数后设
  `AGENTOS_DB_AUTO_REBUILD=1` 重启（自动备份后重建空库继续启动）。

## 六、日志位置与轮转

| 通道 | 位置 | 轮转 |
|---|---|---|
| 内核 stdout 层 | 守护进程重定向目标（systemd→journald；NSSM→AppStdout；本仓开发脚本 `run_kernel_supervised.bat`→根目录 `.kernel_02.log` **追加重定向，无轮转，生产勿复用**） | 由守护方负责 |
| 内核文件层 | `logs/kernel.log.YYYY-MM-DD`（相对进程工作目录；`tracing-appender` daily 轮转，文件名取 **UTC 日期**；无 ANSI 色码，便于 grep） | 按天分文件；内核自身不做容量上限与过期删除，容量治理由外部（logrotate/tmpwatch/巡检脚本）承接 |
| Prompt 审计 | `data/logs/prompt_audit.log`（默认关，`AGENTOS_LOG_PROMPT_BODY=1` 开启） | 不轮转，见 `docs/guides/logging.md` |

> 历史排查资料中的 `.kernel_02.log` 是开发监督脚本的 stdout 重定向文件；
> 2026-09-10 起内核另有上述文件层双写，两者内容同源（同一 tracing sink）。

## 七、回滚步骤

1. **内核二进制**：发布产物按 tag 归档在 GitHub Release（Linux tar.gz / Windows
   zip）。回滚 = 停进程 → 替换二进制 → 启动。回滚前先做第五章备份——SQLite 库
   schema 只保证向前兼容，跨版本回滚不承诺旧二进制读新库。
2. **前端**：静态产物无状态，替换 `frontend/dist` 目录即可（CDN 场景回源）。
3. **插件**：插件目录热重载生效；回滚 = 还原该插件目录（manifest+实现+uv.lock）
   后等 watcher 自动重注册，无需重启内核。
4. **配置**：`config/` 改动即热生效（管道/插件配置），回滚 = 还原文件；
   `default_profile.yaml` 损坏会进入保守全禁（所有插件停注册），按启动 error
   日志修复。
5. 数据不可逆操作（db-admin 清理）自带 `clear-backup-*` 快照，误清即用其恢复。

## 八、安全清单（上线前逐项核对）

- [ ] `AGENTOS_BIND` 保持默认 `127.0.0.1`；确需外网可达（`0.0.0.0`）时置于可信
      网络并配反代/TLS——内核启动会打印非回环绑定告警。
- [ ] 跨域部署设 `AGENTOS_CORS_ORIGINS` 为精确 origin 白名单（逗号分隔，无通配）。
- [ ] `AGENTOS_TOKEN_SECRET` 显式注入（长随机串）——缺省随机时每次重启全部会话失效。
- [ ] 首启随机 admin 口令只在控制台打印一次，登录后前端强制改密
      （`must_change_password`）；显式设 `AGENTOS_ADMIN_PASSWORD` 播种的口令是
      操作员自选事实源，不置改密标记；遗忘时设 `AGENTOS_ADMIN_PASSWORD=<新口令>`
      重启重置。
- [ ] `/health` 为无鉴权存活探针；`/api/v1/*` 业务面与 `/metrics`（Prometheus）
      均在鉴权/token 门后，反代不要把 `/metrics` 暴露到公网。
- [ ] 插件 venv 只从锁文件构建（`uv sync --frozen`），禁止生产在线重解析依赖。
- [ ] 数据目录（`data/`、库三件、`.corrupt-*`/`clear-backup-*` 备份）纳入备份与
      权限收敛（仅服务账号可读）。
