# SQLite 备份与恢复操作指南

> 适用：0.2 架构默认存储（SQLite 单文件承载全量数据：runs / messages / traces /
> blobs / memory / users）。工具入口：`scripts/backup_db.py`（2026-09-11 第七波
> 建制项新增）。PostgreSQL driver 为留桩，接入后本指南需同步更新。

## 一、备份

### 命令

```bash
# 打一份一致性快照到 data/backups/，并轮转保留最新 7 份
uv run python scripts/backup_db.py

# 对最新快照做 PRAGMA integrity_check（恢复演练前的最低校验）
uv run python scripts/backup_db.py --verify
```

### 机制与口径

- **快照方式**：`VACUUM INTO`（与内核 db-admin 清库前的备份同机制）。
  源库以只读连接打开，**对运行中的内核安全**：不阻塞请求、不改动源库，
  产出单一自洽快照文件（含 WAL 中已提交内容）。
- **库路径解析**（与内核 `storage_factory.rs` 同优先级）：
  `AGENTOS_DB_PATH` 环境变量 > `config/kernel/storage.yaml` 的 `storage.sqlite.path`
  > 默认项目根 `agentos_kernel.db`（相对路径按项目根展开）。
- **driver 防呆**：`AGENTOS_STORAGE_DRIVER` 或 storage.yaml 的 driver 非
  `sqlite`（如 `memory`）时直接报错拒绝——内存库无文件可备份，备份错对象
  等于假安全。
- **轮转**：快照名内嵌 UTC 毫秒时间戳（`<库名>-<YYYYMMDDTHHMMSSmmm>.db`），
  按时间保留最新 7 份，更旧的自动删除。备份目录 `data/backups/` 已被
  `.gitignore` 覆盖（`data/`），不入库。

### 触发方式与建议频率

- **当前形态**：手动 / 外部调度（cron / 任务计划）。建议**每日一次**
  低峰期执行，例如 cron（Linux）：

  ```cron
  30 3 * * * cd /path/to/repo && uv run python scripts/backup_db.py >> logs/backup.log 2>&1
  ```

  Windows 可用任务计划程序等价配置。建议对 cron 输出做失败告警（退出码非 0）。
- **内核启动时自动触发**：方案已登记（拍板项 P-6），涉及内核文件，未在本刀
  实施；落地前以手动/cron 为准。
- **恢复演练**：建议每季度用 `--verify` + 一次真实恢复流程（见下）验证
  备份可用——没演练过的备份不算是备份。

## 二、恢复

前提：确认要恢复的快照通过了 `--verify`（integrity_check=ok）。

1. **停内核**：停止 agentos-kernel 进程（避免恢复文件被运行中进程的 WAL
   覆盖）。确认方式：进程列表无 kernel 进程，或项目根无新增
   `agentos_kernel.db-wal` 写入。
2. **替换文件**：用快照覆盖主库文件，并删除伴生 WAL/SHM（属于旧实例的
   残留，混用会导致恢复不完整）：

   ```bash
   cp data/backups/<快照名>.db agentos_kernel.db
   rm -f agentos_kernel.db-wal agentos_kernel.db-shm
   ```

   注意：若内核配置的库路径不是项目根默认值（env / storage.yaml 覆盖），
   替换对象以 `scripts/backup_db.py` 解析出的同一路径为准。
3. **起内核**：正常启动。
4. **验证**：`GET /health` 返回正常；登录检查账号数据回溯到快照时间点；
   会话/任务列表可见历史数据。

## 三、边界说明

- 备份只覆盖 **SQLite 主库**。blob 大对象若未来外置到文件系统/对象存储，
  需另行纳入备份面（当前 0.2 blob 落库，主库快照即全量）。
- `agentos_kernel.db.clear-backup-*`（清库前自动备份）与 `*.pre-clean-backup`
  是内核/db-admin 的就地备份，与本工具的 `data/backups/` 轮转体系互不管理。
- 快照含用户账号数据（哈希后的口令），`data/backups/` 请勿外传；对备份
  文件的访问权限应与生产数据同级对待。
