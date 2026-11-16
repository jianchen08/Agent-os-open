#!/usr/bin/env python3
"""observability-config 门禁：deploy/observability 观测配方契约检查（stdlib-only）。

性能监测补强 R1（docs/working/性能监测补强_20261028.md §四 O1/O2）配套：观测
栈配置（Prometheus 抓取 / Grafana provisioning / 看板 / 令牌刷新脚本）被改动
时，关键契约不得静默漂移——抓取路径与鉴权形态、回环绑定、指标名三方对齐。

零第三方依赖（不引 PyYAML——消费本门禁的恒跑 job 是裸 setup-python）：对
自有 YAML 做行级契约断言（文件归本配方所有，断言的是我们自己写下的关键行）；
JSON 走 stdlib json 真解析。

检查面：
  1. prometheus.yml：抓取 job/路径/bearer 令牌文件/装机目标端口在位
  2. docker-compose.yml：镜像钉版本 + 端口回环绑定 + secrets 只读挂载
  3. grafana provisioning：datasource 指向 prometheus 服务名、看板目录挂载点
  4. 看板 JSON：真解析 + 常用指标面板与 smoke 清单对齐
  5. 令牌面：secrets/ 下无任何 git 跟踪文件（令牌永不入库）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OBS = REPO_ROOT / "deploy" / "observability"

# 与 scripts/check_kernel_metrics_smoke.py EXPECTED_METRICS 对齐（看板必须至少
# 覆盖 R1 新增的三个内核进程指标——看板缺面板 = 指标落了没人看）。
REQUIRED_DASHBOARD_METRICS: tuple[str, ...] = (
    "kernel_proc_cpu_percent",
    "kernel_proc_handle_count",
    "kernel_mcp_reverse_queued",
)

failures: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"[observability-config] {mark} {desc}" + (f"——{detail}" if detail and not ok else ""))
    if not ok:
        failures.append(desc)


def read(rel: str) -> str:
    path = OBS / rel
    if not path.is_file():
        failures.append(f"{rel} 缺失")
        print(f"[observability-config] ✗ {rel} 缺失")
        return ""
    return path.read_text(encoding="utf-8")


def main() -> int:
    # 1. prometheus.yml——抓取契约（路径/鉴权/目标）
    prom = read("prometheus.yml")
    check("prometheus.yml 抓取内核 job 在位", "job_name: agentos-kernel" in prom)
    check("prometheus.yml metrics_path=/metrics", "metrics_path: /metrics" in prom)
    check(
        "prometheus.yml bearer_token_file 令牌面（access token 30min 过期配刷新脚本）",
        "bearer_token_file:" in prom and "metrics_token" in prom,
    )
    check("prometheus.yml 抓取装机业务面 9101", "host.docker.internal:9101" in prom)

    # 2. docker-compose.yml——镜像钉版 + 回环绑定 + secrets 只读
    compose = read("docker-compose.yml")
    check("compose Prometheus 镜像钉版本", "image: prom/prometheus:v2.53.0" in compose)
    check("compose Grafana 镜像钉版本", "image: grafana/grafana:11.1.0" in compose)
    check(
        "compose 端口全部回环绑定（不外曝）",
        '"127.0.0.1:9090:9090"' in compose and '"127.0.0.1:3000:3000"' in compose,
    )
    check(
        "compose secrets 只读挂载进 Prometheus",
        "./secrets:/etc/prometheus/secrets:ro" in compose,
    )

    # 3. Grafana provisioning
    ds = read("grafana/provisioning/datasources/prometheus.yaml")
    check("datasource 指向 compose 服务名 prometheus:9090", "url: http://prometheus:9090" in ds)
    provider = read("grafana/provisioning/dashboards/provider.yaml")
    check("看板 provider 挂载点 /var/lib/grafana/dashboards", "path: /var/lib/grafana/dashboards" in provider)

    # 4. 看板 JSON——真解析 + 指标面板对齐
    dash_raw = read("grafana/dashboards/agentos-overview.json")
    if dash_raw:
        try:
            dashboard = json.loads(dash_raw)
            exprs = json.dumps(dashboard, ensure_ascii=False)
            for metric in REQUIRED_DASHBOARD_METRICS:
                check(f"看板含指标面板 {metric}", metric in exprs)
            check("看板 schemaVersion 声明（provisioning 兼容）", "schemaVersion" in dashboard)
        except json.JSONDecodeError as exc:
            check("看板 JSON 真解析", False, str(exc))

    # 5. 令牌永不入库
    tracked = subprocess.run(
        ["git", "ls-files", "deploy/observability/secrets/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    check("secrets/ 无 git 跟踪文件（令牌面不入库）", tracked.returncode == 0 and not tracked.stdout.strip())

    if failures:
        print(f"[observability-config] 红：{len(failures)} 项契约漂移: {failures}", file=sys.stderr)
        return 1
    print("[observability-config] 绿：观测配方契约完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
