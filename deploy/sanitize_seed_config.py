#!/usr/bin/env python
"""手机端种子配置脱敏面（坑㊳配套：真 key 永不进 APK，G-M0 构建车道）。

脱敏面与断言面共用同一 key 形判定（词表与 kernel config_service.is_secret_key 同源）：
- 脱敏面 sanitize_config_dir：对 config 目录下 .yaml/.yml 递归检查，键名含
  api_key/apikey/secret/token/password 且值为明文字符串（非占位符/掩码/示例值/
  空值）→ 清为空串。只操作 staging overlay 副本，绝不写回仓内 config 工作面
  （脱敏是打包语义，不是源配置语义——源配置的 key 形态契约由 ${VAR} 占位制
  承载，见 llm.yaml 与前端 config.ts）。
- 断言面 seed_config_violations：流式读取打包后的 .tar/.tar.zst 种子，四路
  fail-closed（调用方=build_mobile.sh rootfs 步，非零退出拦截分发）：
  1) opt/agentos/config/** yaml 条目的明文 key（原 2026-10 面非 git 目录键）；
  2) 部署真值禁入路径（FORBIDDEN_SEED_PATH_PREFIXES：fmin local mcp 真值
     配置/审计日志，2026-11-18 个人信息出仓处置）；
  3) 出厂画像对账：config/users/**/profile.md 必须与仓内出厂模板逐字节一致
     （种子面出现用户真值画像即违约）；
  4) config 文本面公网 IPv4：可路由公网地址违约（保留/私网/环回段放行；
     桌面闸 scripts/sanitize_packaged_config.mjs 同律，禁两处漂移）。

安全值形态 is_safe_secret_value：空串 / ``${VAR}`` 整串占位符 / ``***`` 或
``****`` 掩码（内核 GET 掩码视图形态）/ ``your-`` 前缀（.env.example 示例值，
与 llm 侧 _expand_env_vars 未配置判定同口径）。

YAML 解析失败按违约处理（fail-closed）：解析不了的配置无法证明无 key，
不得静默放行进种子。
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

# 键名词表：键名小写后含任一片段即视为 secret 形字段（与 kernel
# config_service.is_secret_key 同表，禁两处漂移）。
SECRET_KEY_PARTS: tuple[str, ...] = ("api_key", "apikey", "secret", "token", "password")

# 指针引用词形（env 变量名/字段名引用，见 is_safe_secret_value docstring）。
_IDENTIFIER_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,23}$")

# 种子内 config 条目前缀（overlay 注入路径，见 build_mobile.sh 部署面拷贝）。
SEED_CONFIG_PREFIX = "opt/agentos/config/"

CONFIG_SUFFIXES = (".yaml", ".yml")

# IPv4/画像面扫描的文本后缀（yaml 键面之外的第二、四路断言输入）。
TEXT_SCAN_SUFFIXES = (".yaml", ".yml", ".json", ".md", ".txt")

# 部署真值禁入路径（前缀匹配；2026-11-18 个人信息出仓处置——真值配置与
# 审计日志属部署机私有信息，example 为出厂形态）。与 .gitignore 的
# deploy/remote/local_mcp_server 条目同律，此面为种子侧 fail-closed 兜底。
FORBIDDEN_SEED_PATH_PREFIXES: tuple[str, ...] = (
    "opt/agentos/deploy/remote/local_mcp_server/config.json",
    "opt/agentos/deploy/remote/local_mcp_server/logs/",
)

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def is_secret_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in SECRET_KEY_PARTS)


def is_safe_secret_value(value: str) -> bool:
    """明文判定反向面：占位符/掩码/示例值/空/指针引用 = 安全，其余字符串 = 明文。

    指针引用豁免：值是「真值在哪」的命名引用而非真值本体——env 变量名
    （bridge auth.token_env: AGENTOS_BRIDGE_TOKEN）或字段名引用（godot
    auth.secret_key: godot_api_key）。仓库标识符约定 = 3~24 位字母/数字/下划线；
    provider 控制台签发的真 key 形态（连字符/混排/超长：sk-…、JWT、hex 串）
    不会命中该词形。残余风险如实记录：≤24 位纯标识符形态的短 key 在词形上
    与命名引用不可区分——此类 key 依契约本就不该出现在 config（源配置 key
    形态 = ${VAR} 占位制），词形豁免不构成其入包通道。
    """
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.startswith("${") and stripped.endswith("}"):
        return True
    if set(stripped) == {"*"} and len(stripped) >= 3:
        return True
    if stripped.startswith("your-"):
        return True
    return _IDENTIFIER_REF_RE.fullmatch(stripped) is not None


def _walk_violations(node: Any, path: str) -> Iterator[str]:
    """递归收集明文 secret 值的位置（jsonpath 形态，供报告定位）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if is_secret_key(str(key)) and isinstance(value, str) and not is_safe_secret_value(value):
                yield child
            else:
                yield from _walk_violations(value, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_violations(item, f"{path}[{i}]")


def violations_in_data(data: Any) -> list[str]:
    return list(_walk_violations(data, ""))


def sanitize_yaml_file(path: Path) -> list[str]:
    """原位清写单个 yaml 的明文 secret 值；返回清写位置清单（未动返回空）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    hits = violations_in_data(data)
    if not hits:
        return []
    _clear_violations(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return hits


def _clear_violations(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if is_secret_key(str(key)) and isinstance(value, str) and not is_safe_secret_value(value):
                node[key] = ""
            else:
                _clear_violations(value)
    elif isinstance(node, list):
        for item in node:
            _clear_violations(item)


def sanitize_config_dir(config_dir: Path) -> list[str]:
    """脱敏整棵 config 目录；返回 "<相对路径>#<jsonpath>" 清单（含解析失败即抛）。"""
    cleared: list[str] = []
    for path in sorted(config_dir.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in CONFIG_SUFFIXES):
            continue
        rel = path.relative_to(config_dir).as_posix()
        try:
            for hit in sanitize_yaml_file(path):
                cleared.append(f"{rel}#{hit}")
        except yaml.YAMLError as exc:
            raise ValueError(f"{rel}: yaml 解析失败（fail-closed，不得入种子）: {exc}") from exc
    return cleared


def _open_seed_reader(path: Path):
    """按魔数选解释器：zstd 魔数 → zstandard 流；否则按纯 tar 打开（测试/明文包）。"""
    with path.open("rb") as fh:
        magic = fh.read(4)
    if magic == b"\x28\xb5\x2f\xfd":
        import zstandard  # 纯 tar 测试路径不依赖 zstandard（懒导入）

        return zstandard.open(path, "rb")
    return path.open("rb")


def is_public_routable_ipv4(literal: str) -> bool:
    """公网可路由判定：ipaddress.is_global（TEST-NET/私网/环回/链路本地面均
    False；可路由公网地址 True）。种子/桌面双闸同律。"""
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return False
    return ip.is_global


def default_repo_users_dir() -> Path:
    """出厂画像真值位：脚本位于 deploy/ → 仓内 config/users/。"""
    return Path(__file__).resolve().parents[1] / "config" / "users"


def seed_config_violations(
    seed_path: Path,
    prefix: str = SEED_CONFIG_PREFIX,
    repo_users_dir: Path | None = None,
) -> list[str]:
    """流式扫描种子，四路断言（明文 key / 禁入路径 / 画像对账 / 公网 IPv4）。

    返回 "<条目>#<定位>" 违约清单。画像对账缺席不判（种子未含 users 面属
    打包完整性问题，由 build_mobile.sh 部署面完整性检查承载）。
    """
    if repo_users_dir is None:
        repo_users_dir = default_repo_users_dir()
    violations: list[str] = []
    with _open_seed_reader(seed_path) as raw, tarfile.open(fileobj=raw, mode="r|") as tf:
        for member in tf:
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            # 第二路：部署真值禁入路径（范围=全种子，不限 config 前缀）。
            if name.startswith(FORBIDDEN_SEED_PATH_PREFIXES):
                violations.append(f"{name}#<forbidden-path>")
                continue
            if not name.startswith(prefix):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                violations.append(f"{name}#<unreadable>")
                continue
            payload = fh.read()
            # 第三路：出厂画像对账（users/**/profile.md ≡ 仓内出厂模板）。
            rel = name[len(prefix):]
            if rel.startswith("users/") and rel.endswith("profile.md"):
                factory = repo_users_dir / rel[len("users/"):]
                factory_bytes = factory.read_bytes() if factory.is_file() else None
                if factory_bytes != payload:
                    violations.append(f"{name}#<profile-factory-drift>")
                continue
            # 第一路（原面）：yaml 明文 key。
            if name.lower().endswith(CONFIG_SUFFIXES):
                try:
                    data = yaml.safe_load(payload.decode("utf-8"))
                except (yaml.YAMLError, UnicodeDecodeError) as exc:
                    violations.append(f"{name}#<yaml-parse-failed: {exc}>")
                    continue
                for hit in violations_in_data(data):
                    violations.append(f"{name}#{hit}")
            # 第四路：config 文本面公网 IPv4（保留/私网/环回段放行）。
            if name.lower().endswith(TEXT_SCAN_SUFFIXES):
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # 二进制面无法判定，yaml 面已各自执法
                for m in _IPV4_RE.finditer(text):
                    if is_public_routable_ipv4(m.group(0)):
                        violations.append(f"{name}#<public-ipv4:{m.group(0)}>")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="种子配置脱敏（sanitize）与产物断言（assert-seed）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sanitize = sub.add_parser("sanitize", help="原位清写 config 目录明文 key（只动副本）")
    p_sanitize.add_argument("--config-dir", type=Path, required=True)

    p_assert = sub.add_parser("assert-seed", help="断言种子产物内无明文 key（违约=退出 1）")
    p_assert.add_argument("--seed", type=Path, required=True)
    p_assert.add_argument("--prefix", default=SEED_CONFIG_PREFIX)

    args = parser.parse_args(argv)
    if args.command == "sanitize":
        cleared = sanitize_config_dir(args.config_dir)
        for item in cleared:
            print(f"[sanitize] cleared {item}", file=sys.stderr)
        print(f"[sanitize] ok（清写 {len(cleared)} 处）")
        return 0
    violations = seed_config_violations(args.seed, args.prefix)
    if violations:
        for item in violations:
            print(f"[assert-seed][FAIL] 明文 key: {item}", file=sys.stderr)
        return 1
    print("[assert-seed] ok（产物 config 面零明文 key）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
