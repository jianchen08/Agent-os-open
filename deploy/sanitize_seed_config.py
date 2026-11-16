#!/usr/bin/env python
"""手机端种子配置脱敏面（坑㊳配套：真 key 永不进 APK，G-M0 构建车道）。

两面共用同一判定（词表与 kernel config_service.is_secret_key 同源）：
- 脱敏面 sanitize_config_dir：对 config 目录下 .yaml/.yml 递归检查，键名含
  api_key/apikey/secret/token/password 且值为明文字符串（非占位符/掩码/示例值/
  空值）→ 清为空串。只操作 staging overlay 副本，绝不写回仓内 config 工作面
  （脱敏是打包语义，不是源配置语义——源配置的 key 形态契约由 ${VAR} 占位制
  承载，见 llm.yaml 与前端 config.ts）。
- 断言面 assert_seed_no_plaintext_keys：流式读取打包后的 .tar/.tar.zst 种子，
  对 opt/agentos/config/** 条目跑同一判定，发现任何明文 key 即返回违约清单
  （调用方=build_mobile.sh rootfs 步，非零退出拦截分发）——"产物内不得出现
  真 key" 的 fail-closed 关口。

安全值形态 is_safe_secret_value：空串 / ``${VAR}`` 整串占位符 / ``***`` 或
``****`` 掩码（内核 GET 掩码视图形态）/ ``your-`` 前缀（.env.example 示例值，
与 llm 侧 _expand_env_vars 未配置判定同口径）。

YAML 解析失败按违约处理（fail-closed）：解析不了的配置无法证明无 key，
不得静默放行进种子。
"""

from __future__ import annotations

import argparse
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


def seed_config_violations(seed_path: Path, prefix: str = SEED_CONFIG_PREFIX) -> list[str]:
    """流式扫描种子内 config 条目，返回 "<条目>#<jsonpath>" 违约清单。"""
    violations: list[str] = []
    with _open_seed_reader(seed_path) as raw, tarfile.open(fileobj=raw, mode="r|") as tf:
        for member in tf:
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            if not name.startswith(prefix):
                continue
            if not name.lower().endswith(CONFIG_SUFFIXES):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                violations.append(f"{name}#<unreadable>")
                continue
            try:
                data = yaml.safe_load(fh.read().decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                violations.append(f"{name}#<yaml-parse-failed: {exc}>")
                continue
            for hit in violations_in_data(data):
                violations.append(f"{name}#{hit}")
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
