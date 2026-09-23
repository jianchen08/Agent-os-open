# scripts/ 运维口径

## 内核日志压缩轮转（compress_kernel_logs.py）

内核日志 tracing-appender 按天滚动写 `kernel.log.YYYY-MM-DD`（纯 UTC 命名，
无压缩，实测 ~37MB/天）。本脚本把 **>3 天**的旧日志 gzip 到同目录 `archive/`
子目录（对 tracing-appender 的 `max_log_files(30)` 清理不可见，不占明文额度），
归档保留 **30 天**，超龄自动删除。安全契约：

- 只认 `kernel.log.<日期>` 文件名；`kernel.log` 裸名、`kernel.liveness`、
  `kernel_stderr.log` 等一律不碰；
- 今日/昨日「最新」文件不碰（写入者持有句柄；daily 轮转下写者只可能持这两份）；
- 先写临时文件 → 读回校验字节数 → 原子改名 → 最后删原文件；失败自动回滚
  `.gz`，不留双份不丢数据；
- 默认 dry-run，`--apply` 才执行。

### 手动触发

```bat
:: dev 仓库日志（dry-run 预览）
python scripts\compress_kernel_logs.py

:: dev 仓库日志（执行）
python scripts\compress_kernel_logs.py --apply

:: 装机版日志（执行；目录 = 安装 resources\kernel\logs）
python scripts\compress_kernel_logs.py --apply --dir "C:\Users\<user>\AppData\Local\Programs\agent-os\resources\kernel\logs"

:: 自定义窗口：>7 天才压、归档留 14 天
python scripts\compress_kernel_logs.py --apply --compress-age-days 7 --retain-days 14
```

退出码：0 = 全部成功；1 = 有失败项（stderr 逐条留痕，可接监控）。

### 自动触发

- **dev**：`start_web_02.bat` Step 2.5 在每次启动内核前对本仓 `logs/` 跑
  `--apply`（失败仅告警不阻断启动）；
- **装机版**：无 launcher，按需二选一——
  1. Windows 计划任务每日执行（示例，SYSTEM 权限可选）：
     `schtasks /Create /TN "AgentOS Log Compress" /SC DAILY /ST 09:30 /TR "python C:\path\to\scripts\compress_kernel_logs.py --apply --dir \"C:\Users\<user>\AppData\Local\Programs\agent-os\resources\kernel\logs\""`
  2. 或人工在磁盘巡检时手动执行上面的手动命令。

### 行为测试

`tests/test_compress_kernel_logs.py`（临时目录 fixture：dry-run 零副作用、
压缩/边界年龄、活动日志保护、保留窗、失败回滚、CLI 退出码）。
