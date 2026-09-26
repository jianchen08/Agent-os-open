# 部署指南（0.2）

> 适用对象：把 AgentOS 0.2（Rust 微内核 + Python 插件 + React 前端）部署到
> 生产/常驻环境。所有环境变量名、默认值、文件路径均按当前代码逐一核实
> （来源标注在表中）。架构基线见 `docs/ARCHITECTURE.md`。

## 一、生产拓扑

```
┌────────────────────────────── 宿主机（或容器宿主） ──────────────────────────────┐
│                                                                                  │
│  agentos-kernel（Rust，单进程）           前端（静态产物 frontend/dist）             │
│  ├─ HTTP/WS API  127.0.0.1:9100           ├─ 任意静态服务器/CDN 托管，或            │
│  ├─ /health /api/v1/* /ws /metrics        │   vite preview（开发/小规模）           │
│  ├─ tracing 日志（stdout + 文件双写）      └─ 浏览器直连内核 API（CORS 白名单放行）    │
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
- 前端与内核**同源不同端口**是默认形态；跨域部署必须设 `AGENTOS_CORS_ORIGINS`
  白名单（见下表），内核对本地源（localhost/127.0.0.1/[::1] 任意端口）恒放行。
- 插件 sidecar 依赖各插件目录下的 `.venv`（uv 管理），**必须随代码一起部署**
  （见构建步骤第 3 步），内核不会回退到裸 `python`（fail-closed）。

## 二、构建步骤

前置：Rust 1.85（`rust-toolchain.toml` 钉版）、Python 3.11 + [uv](https://docs.astral.sh/uv/)、
Node 22 + pnpm（前端；根目录 npm 工具链仅桌面壳 electron 需要）。

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
| `AGENTOS_ADMIN_PASSWORD` | 内置 admin 口令；未设 = 首启生成随机口令并**仅打印一次**，置非空值可重置已有 admin 口令 | 未设（随机） | `bin/agentos-kernel.rs`（`resolve_admin_password`） |
| `AGENTOS_PLUGINS_DIR` | 内置插件根目录（只读） | `<项目根>/plugins/shared` | 同上 |
| `AGENTOS_USER_PLUGINS_DIR` | 用户插件根目录（可写，第三方插件安装位；同 id 覆盖内置根） | `<USER_ROOT>/plugins` | 同上（`resolve_user_plugins_dir`） |
| `AGENTOS_CONFIG_ROOT` | 工厂配置根目录（只读基线，用户层优先见上行 `AGENTOS_USER_CONFIG_DIR`） | `<项目根>/config` | 同上 |
| `AGENTOS_DB_AUTO_REBUILD` | `=1` 显式允许损坏库自动备份后重建空库继续启动；**默认坏库 fail-closed 拒启** | 未设（拒启） | `kernel/crates/engine/src/store.rs`；ADR `docs/decisions/2026-09-11-corrupt-db-fail-closed.md` |
| `AGENTOS_ALLOW_EMPTY_PLUGINS` | `=1` 插件 discover 失败时以空插件集启动（嵌入式/最小化部署逃生门） | 未设（discover 失败拒启） | `bin/agentos-kernel.rs` |
| `AGENTOS_GRANTS_STRICT` | `=1` 插件未声明 `granted_capabilities` 时反向能力调用一律拒绝 | 未设（未声明默认全授予） | 同上 |
| `AGENTOS_MAX_BLOCKING_THREADS` | tokio 阻塞池上限 | `512` | 同上（`resolve_max_blocking_threads`） |
| `AGENTOS_THREAD_STACK_KIB` | 线程栈大小 KiB | `2048` | 同上（`resolve_thread_stack_size`） |
| `AGENTOS_MEM_WATERMARK_MB` / `AGENTOS_MEM_WATERMARK_MIN` | 内存水位自愈（持续超限排空重启）；**默认关闭**，显式正值才启用 | 关 / 10 分钟 | 同上（`resolve_watermark_config`） |
| `RUST_LOG` | 内核 tracing EnvFilter（如 `info,agentos_mcp=debug`） | `info` | `tracing_subscriber::EnvFilter`；见 `docs/guides/logging.md` |

前端（`frontend/.env.example` → `.env.local`，构建期注入）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `VITE_API_BASE_URL` | 内核 API 基址 | `http://127.0.0.1:9100` |
| `VITE_WS_BASE_URL` | 内核 WS 基址 | `ws://127.0.0.1:9100` |

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

```ini
# /etc/systemd/system/agentos-kernel.service
[Unit]
Description=AgentOS Kernel (agentos-kernel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agentos
# WorkingDirectory 必须是项目根：插件目录/配置/日志均为相对路径基准
WorkingDirectory=/opt/agentos
Environment=AGENTOS_BIND=127.0.0.1
Environment=AGENTOS_KERNEL_PORT=9100
Environment=AGENTOS_DB_PATH=/opt/agentos/data/agentos_kernel.db
# 生产必设：固定 token 密钥（否则重启全员掉线）与 admin 口令
Environment=AGENTOS_TOKEN_SECRET=<change-me-long-random>
Environment=AGENTOS_ADMIN_PASSWORD=<change-me>
ExecStart=/opt/agentos/kernel/target/release/agentos-kernel
Restart=always
RestartSec=3
# stdout/stderr 交 journald 收口（内核日志文件层另见「日志位置与轮转」）

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now agentos-kernel
journalctl -u agentos-kernel -f          # stdout 层日志
```

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
      （`must_change_password`）；遗忘时设 `AGENTOS_ADMIN_PASSWORD=<新口令>` 重启重置。
- [ ] `/health` 为无鉴权存活探针；`/api/v1/*` 业务面与 `/metrics`（Prometheus）
      均在鉴权/token 门后，反代不要把 `/metrics` 暴露到公网。
- [ ] 插件 venv 只从锁文件构建（`uv sync --frozen`），禁止生产在线重解析依赖。
- [ ] 数据目录（`data/`、库三件、`.corrupt-*`/`clear-backup-*` 备份）纳入备份与
      权限收敛（仅服务账号可读）。
