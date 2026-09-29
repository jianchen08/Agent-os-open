#!/usr/bin/env python3
"""存量 LLM 配置漂移迁移（2026-09-28 方案批次 D，docs/working/LLM配置改不生效修复方案_20260928.md）。

背景：缺陷版本里设置页把模型配置写进**安装包** config（factory），运行时读的却是
用户空间叠加结果——两侧 llm.yaml / .env 已漂移（本机实证：用户空间独有
webchat-deepseek，factory 独有 MiniMax-M3.1-Flash-Preview 与最新 tiers 切换）。

本脚本把两侧收敛回「用户空间单源」：
1. llm.yaml：以用户空间为基并入 factory 独有条目（models/providers 键级并集、
   冲突用户赢；defaults 按 --defaults-policy 取侧，默认 factory——缺陷期内 UI 保存
   全落 factory，那里是最近的用户意图），备份后**删除用户副本再经内核 PUT** 落盘：
   PUT 对缺失的用户文件自动播种 + 接管账本登记（ADR 2026-09-14 三件套），
   杜绝「无凭证接管」。
2. .env：保守并集——factory 独有变量**追加**进用户 .env（只增不改，绝不覆盖）。

用法（内核须在跑；干跑先看台账加 --dry-run）：
  python scripts/migrate_llm_config_ownership.py \
      --base-url http://127.0.0.1:9101 \
      --factory-config "C:/Users/<u>/AppData/Local/Programs/agent-os/resources/config" \
      --password <admin 口令> \
      [--user-config "C:/Users/<u>/AppData/Roaming/agentos/config"] \
      [--defaults-policy factory|user] [--dry-run]

幂等：账本已登记 plugins/llm/llm.yaml 时文件面跳过；.env 并集天然幂等。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

OWNERSHIP_LEDGER = ".ownership.json"
CONFIG_REL = "plugins/llm/llm.yaml"


# ── 纯函数（单测面：tests/gates/test_migrate_llm_config_ownership.py） ──


def deep_merge_config(
    user: dict[str, Any], factory: dict[str, Any], defaults_policy: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """以用户空间为基并入 factory 独有条目，返回 (merged, report)。

    - models/providers：键级并集，factory 独有条目补入；同名冲突**用户赢**
      （用户空间是架构真值，factory 侧同名条目视为过期出厂默认）。
    - defaults：按 policy 取侧（factory=缺陷期内 UI 保存落点的最新用户意图）。
    - 其余顶层键：factory 独有则补入，冲突用户赢。
    """
    merged = copy.deepcopy(user)
    report: dict[str, Any] = {
        "factory_only_models": [],
        "factory_only_providers": [],
        "conflict_models_user_won": [],
        "conflict_providers_user_won": [],
        "factory_only_top_keys": [],
        "defaults_from": "user",
    }
    for section, added_key, conflict_key in (
        ("models", "factory_only_models", "conflict_models_user_won"),
        ("providers", "factory_only_providers", "conflict_providers_user_won"),
    ):
        user_section = merged.get(section)
        factory_section = factory.get(section)
        if not isinstance(factory_section, dict):
            continue
        if not isinstance(user_section, dict):
            merged[section] = copy.deepcopy(factory_section)
            report[added_key] = sorted(factory_section)
            continue
        for key, value in factory_section.items():
            if key not in user_section:
                user_section[key] = copy.deepcopy(value)
                report[added_key].append(key)
            elif user_section[key] != value:
                report[conflict_key].append(key)
    for key, value in factory.items():
        if key in ("models", "providers", "defaults"):
            continue
        if key not in merged:
            merged[key] = copy.deepcopy(value)
            report["factory_only_top_keys"].append(key)
    factory_defaults = factory.get("defaults")
    if defaults_policy == "factory" and isinstance(factory_defaults, dict):
        if merged.get("defaults") != factory_defaults:
            merged["defaults"] = copy.deepcopy(factory_defaults)
            report["defaults_from"] = "factory"
    return merged, report


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE 行解析（首 个 = 切分；注释/空行跳过）。"""
    vars_: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        vars_[key.strip()] = value.strip()
    return vars_


def merge_env(factory_text: str, user_text: str) -> tuple[str, list[str]]:
    """.env 保守并集：factory 独有变量追加到用户文本尾部（只增不改）。

    返回 (新用户文本, 追加的变量名列表)；无追加时原样返回。
    """
    user_vars = parse_env_text(user_text)
    factory_vars = parse_env_text(factory_text)
    missing = [k for k in factory_vars if k not in user_vars]
    if not missing:
        return user_text, []
    lines = [user_text.rstrip("\n"), "", "# ── migrated from factory .env (2026-09-28 批次 D) ──"]
    appended: list[str] = []
    for raw_line in factory_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in missing:
            lines.append(raw_line)
            appended.append(key)
    return "\n".join(lines) + "\n", appended


def has_ownership(ledger: list[dict[str, Any]], rel: str) -> bool:
    return any(entry.get("path") == rel for entry in ledger)


# ── 内核 HTTP（登录 → GET/PUT 插件配置） ──


class KernelClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = self._login(username, password)

    def _login(self, username: str, password: str) -> str:
        body = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/v1/auth/login", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        token = data.get("access_token")
        if not token:
            raise SystemExit("登录失败：响应无 access_token")
        return str(token)

    def _request(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, dict[str, Any], str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}"), ""
        except urllib.error.HTTPError as e:
            return e.code, {}, e.read().decode()[:300]

    def get_config(self, plugin: str, file_id: str) -> tuple[str, dict[str, Any]]:
        status, data, err = self._request(f"/api/v1/plugins/{plugin}/config/{file_id}")
        if status != 200:
            raise SystemExit(f"GET 配置失败 {status}: {err}")
        return str(data.get("etag", "")), dict(data.get("data") or {})

    def put_config(self, plugin: str, file_id: str, etag: str, data: dict[str, Any]) -> None:
        status, _, err = self._request(
            f"/api/v1/plugins/{plugin}/config/{file_id}",
            method="PUT",
            body={"data": data, "if_match": etag},
        )
        if status != 200:
            raise SystemExit(f"PUT 配置失败 {status}: {err}")


# ── 主流程 ──


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = path.with_name(f"{path.name}.bak-migrate-{stamp}")
    shutil.copy2(path, target)
    return target


def load_ledger(user_config: Path) -> list[dict[str, Any]]:
    ledger = user_config / OWNERSHIP_LEDGER
    if not ledger.is_file():
        return []
    return list(json.loads(ledger.read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:9101")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.environ.get("AGENTOS_ADMIN_PASSWORD", ""))
    parser.add_argument("--factory-config", required=True, help="安装包 config 目录（resources/config）")
    parser.add_argument("--user-config", default="", help="用户空间 config 目录（缺省 AGENTOS_USER_ROOT/config 或 %%APPDATA%%/agentos/config）")
    parser.add_argument("--plugin", default="llm_service")
    parser.add_argument("--file-id", default="llm")
    parser.add_argument("--defaults-policy", choices=["factory", "user"], default="factory")
    parser.add_argument("--skip-env", action="store_true", help="跳过 .env 并集")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("缺少管理口令：--password 或环境变量 AGENTOS_ADMIN_PASSWORD")

    user_config = Path(
        args.user_config
        or os.path.join(os.environ.get("AGENTOS_USER_ROOT", ""), "config")
        or os.path.join(os.environ.get("APPDATA", ""), "agentos", "config")
    )
    if not user_config.is_dir():
        raise SystemExit(f"用户 config 目录不存在: {user_config}（--user-config 显式指定）")
    factory_config = Path(args.factory_config)
    if not factory_config.is_dir():
        raise SystemExit(f"factory config 目录不存在: {factory_config}")

    user_file = user_config / CONFIG_REL
    factory_file = factory_config / CONFIG_REL

    print(f"== LLM 配置漂移迁移（dry_run={args.dry_run}） ==")
    print(f"user    = {user_file}")
    print(f"factory = {factory_file}")

    ledger = load_ledger(user_config)
    if has_ownership(ledger, CONFIG_REL):
        print(f"[skip] 账本已登记 {CONFIG_REL}（已接管，文件面无需迁移）")
        merged: dict[str, Any] | None = None
    elif not user_file.is_file():
        print("[skip] 用户空间无副本（无漂移；内核 PUT 首写时自动播种）")
        merged = None
    elif not factory_file.is_file():
        print("[skip] factory 侧无文件（无从并入）")
        merged = None
    else:
        user_data = yaml.safe_load(user_file.read_text(encoding="utf-8")) or {}
        factory_data = yaml.safe_load(factory_file.read_text(encoding="utf-8")) or {}
        merged, report = deep_merge_config(user_data, factory_data, args.defaults_policy)
        print(f"[merge] factory 独有 models: {report['factory_only_models'] or '无'}")
        print(f"[merge] factory 独有 providers: {report['factory_only_providers'] or '无'}")
        print(f"[merge] 同名冲突（用户赢）models={report['conflict_models_user_won'] or '无'} providers={report['conflict_providers_user_won'] or '无'}")
        print(f"[merge] defaults 取侧: {report['defaults_from']}（--defaults-policy={args.defaults_policy}）")
        print(f"[merge] factory 独有顶层键: {report['factory_only_top_keys'] or '无'}")
        if args.dry_run:
            print("[dry-run] 文件面未落盘")
        else:
            user_bak = _backup(user_file)
            print(f"[backup] {user_bak}")
            user_file.unlink()

    if merged is not None and not args.dry_run:
        kernel = KernelClient(args.base_url, args.username, args.password)
        etag, _view = kernel.get_config(args.plugin, args.file_id)
        kernel.put_config(args.plugin, args.file_id, etag, merged)
        ledger_after = load_ledger(user_config)
        if not has_ownership(ledger_after, CONFIG_REL):
            raise SystemExit("PUT 成功但账本未见登记——种子/登记链路异常，请人工核查")
        print(f"[done] 经内核 PUT 落盘并登记接管（{CONFIG_REL}）")

    if not args.skip_env:
        user_env = user_config.parent / ".env"
        factory_env = factory_config.parent / ".env"
        if user_env.is_file() and factory_env.is_file():
            added = merge_env(factory_env.read_text(encoding="utf-8"), user_env.read_text(encoding="utf-8"))
            if added[1]:
                print(f"[env] factory 独有变量 {len(added[1])} 个: {', '.join(added[1])}")
                if args.dry_run:
                    print("[dry-run] .env 未落盘")
                else:
                    env_bak = _backup(user_env)
                    user_env.write_text(added[0], encoding="utf-8")
                    print(f"[env] 已并入（备份 {env_bak}）")
            else:
                print("[env] 用户 .env 已是超集，无需并入")
        else:
            print("[env] 两侧 .env 不齐（跳过）")

    print("== 完成 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
