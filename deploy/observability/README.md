# AgentOS 观测配方（Prometheus + Grafana）

> 内核 `/metrics` 已是 Prometheus exposition 格式（受保护路径，Bearer 鉴权）。
> 本配方提供按需拉起的抓取+看板栈：不进产品、不改内核，桌面装机默认不带。
> 设计真值：`docs/working/性能监测补强_20261028.md` §四 O1。

## 前置条件

- 内核在跑（dev 形态 HTTP 端口 `9100`，装机业务面 `9101`；以
  `config/kernel/api_config.yaml` 为准）。
- 仓根 `.env` 含 `AGENTOS_ADMIN_PASSWORD`（连接安全铁律：口令只从 `.env`/环境
  读，不落本目录任何配置）。

## 拉起步骤

```bash
cd deploy/observability

# 1. 铸令牌：用 .env 的 admin 口令登录内核，把 access token 写进 secrets/
python refresh_metrics_token.py --base http://127.0.0.1:9101 --out secrets/metrics_token

# 2. 拉起 Prometheus + Grafana（Windows 下用 Docker Desktop / WSL）
docker compose up -d
#   Prometheus → http://localhost:9090
#   Grafana    → http://localhost:3000（admin/admin，首登改密）

# 3. 看板自动 provisioning（grafana/dashboards/agentos-overview.json）
```

access token 30 分钟过期（`kernel/crates/api/src/auth.rs` `ACCESS_TOKEN_TTL_SECS`），
长期抓取需定时刷新令牌：

- Windows 计划任务：每 25 分钟跑一次步骤 1（脚本幂等，只写文件）。
- Linux systemd timer / crontab 同理；compose 内亦可常驻
  `python refresh_metrics_token.py --interval 1500 --base http://host.docker.internal:9101`。

## 端口与目标

| 形态 | 内核地址 | 抓取目标写法 |
|---|---|---|
| dev | `http://127.0.0.1:9100` | `prometheus.yml` targets 改 9100；compose 里用 `host.docker.internal` |
| 装机 | `http://127.0.0.1:9101` | 默认配置即此形态 |

`prometheus.yml` 默认 `bearer_token_file: /etc/prometheus/secrets/metrics_token`
（compose 已挂 `./secrets`）。不用 compose 裸跑 Prometheus 时，把该路径改成
本机 `secrets/metrics_token` 的绝对路径。

## 看板指标口径

- 已确认存在的内核指标样例：`kernel_mcp_reverse_queued` / `kernel_mcp_reverse_active`
  （`main.rs` agg_flush 每秒落 gauge）。
- R1 新增：`kernel_proc_cpu_percent` / `kernel_proc_handle_count`（内核自身进程，
  见方案 §四 T5）。
- 完整指标清单以运行时为准：`GET /ext/metrics_admin/list`（带 Bearer），
  看板面板按需增补 query，不在此处猜名。

## 校验

```bash
docker run --rm -v "$PWD/prometheus.yml:/prometheus.yml" prom/prometheus:v2.53.0 \
  promtool check config /prometheus.yml
# 或本机 promtool：promtool check config prometheus.yml
```

## 安全注意

- `/metrics` 暴露内部运行指标：令牌文件勿提交（`secrets/` 已 gitignore 本配方
  目录内产物）、Grafana 首登改密、栈只绑本机回环（compose 已 `127.0.0.1:` 前缀）。
- 外发告警（邮件/webhook）属 Grafana 侧配置，按需自加，本配方不预置。
