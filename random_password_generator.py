"""随机密码生成器（命令行小工具）。

提供密码学安全的随机密码生成能力，底层使用 :mod:`secrets` 模块。
支持：

* 自定义长度与数量
* 可选是否包含数字、特殊符号
* 可选输出到文件或控制台

典型用法::

    python random_password_generator.py --length 20 --count 3
    python random_password_generator.py --length 12 --no-symbols --output pwds.txt
"""
from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path
from typing import Sequence

__all__ = [
    "generate_password",
    "generate_passwords",
    "build_arg_parser",
    "main",
]

# 字符集常量（私有）
_LOWERCASE: str = string.ascii_lowercase
_UPPERCASE: str = string.ascii_uppercase
_DIGITS: str = string.digits
_SYMBOLS: str = "!@#$%^&*()-_=+[]{};:,.<>?/~"


def _build_alphabet(use_numbers: bool, use_symbols: bool) -> str:
    """根据开关拼接可用字符集。

    Args:
        use_numbers: 是否包含数字。
        use_symbols: 是否包含特殊符号。

    Returns:
        可供采样的字符集合字符串。

    Raises:
        ValueError: 当数字和符号都被禁用时（仍保留字母，理论上不会触发），
            保留作为防御性校验。
    """
    alphabet = _LOWERCASE + _UPPERCASE
    if use_numbers:
        alphabet += _DIGITS
    if use_symbols:
        alphabet += _SYMBOLS
    if not alphabet:
        # 理论上不会触发（字母本身已非空），但保留防御性校验
        raise ValueError("字符集为空，无法生成密码")
    return alphabet


def generate_password(
    length: int,
    *,
    use_symbols: bool = True,
    use_numbers: bool = True,
) -> str:
    """生成单个密码学安全的随机密码。

    使用 :func:`secrets.choice` 从配置的字符池中独立采样每一位。

    Args:
        length: 密码长度，必须 >= 1。
        use_symbols: 是否包含特殊符号，默认 True。
        use_numbers: 是否包含数字，默认 True。

    Returns:
        长度为 ``length`` 的随机密码字符串。

    Raises:
        ValueError: 当 ``length`` 小于 1 时抛出。
    """
    if length < 1:
        raise ValueError(f"密码长度必须 >= 1，当前为 {length}")

    # RED 阶段占位：尚未实现
    raise NotImplementedError("generate_password 尚未实现（TDD Red 阶段）")


def generate_passwords(
    count: int,
    length: int,
    *,
    use_symbols: bool = True,
    use_numbers: bool = True,
) -> list[str]:
    """批量生成密码。

    Args:
        count: 生成数量，必须 >= 1。
        length: 单个密码长度，必须 >= 1。
        use_symbols: 是否包含特殊符号。
        use_numbers: 是否包含数字。

    Returns:
        长度为 ``count`` 的密码列表。

    Raises:
        ValueError: 当 ``count`` 或 ``length`` 小于 1 时抛出。
    """
    if count < 1:
        raise ValueError(f"生成数量必须 >= 1，当前为 {count}")
    if length < 1:
        raise ValueError(f"密码长度必须 >= 1，当前为 {length}")

    return [
        generate_password(length, use_symbols=use_symbols, use_numbers=use_numbers)
        for _ in range(count)
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="random_password_generator",
        description="使用 secrets 模块生成密码学安全的随机密码。",
    )
    parser.add_argument(
        "--length",
        type=int,
        default=16,
        help="单个密码的长度（默认 16）。",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="生成密码的数量（默认 1）。",
    )
    parser.add_argument(
        "--no-symbols",
        action="store_true",
        help="不包含特殊符号。",
    )
    parser.add_argument(
        "--no-numbers",
        action="store_true",
        help="不包含数字。",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="可选的输出文件路径；不指定则输出到控制台。",
    )
    return parser


def _write_output(passwords: Sequence[str], output: str | None) -> None:
    """将密码列表写出到控制台或文件。"""
    if output is None:
        for pwd in passwords:
            print(pwd)
        return

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 显式指定 utf-8 + newline，避免 Windows 平台双倍换行
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for pwd in passwords:
            f.write(pwd + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 可选参数列表；为 ``None`` 时使用 ``sys.argv[1:]``。

    Returns:
        进程退出码，0 表示成功，非 0 表示异常退出。
    """
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        passwords = generate_passwords(
            count=args.count,
            length=args.length,
            use_symbols=not args.no_symbols,
            use_numbers=not args.no_numbers,
        )
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    _write_output(passwords, args.output)
    return 0


# ---------------------------------------------------------------------------
# 单元测试（test_xxx 形式）
# ---------------------------------------------------------------------------

def _is_only_from_alphabet(password: str, alphabet: str) -> bool:
    return all(ch in alphabet for ch in password)


def test_generate_password_default_length() -> None:
    """默认调用应返回非空字符串。"""
    pwd = generate_password(16)
    assert isinstance(pwd, str)
    assert len(pwd) == 16


def test_generate_password_respects_length() -> None:
    """长度参数必须严格生效。"""
    for length in (1, 8, 32, 64):
        pwd = generate_password(length)
        assert len(pwd) == length, f"期望长度 {length}，实际 {len(pwd)}"


def test_generate_password_no_symbols_no_numbers() -> None:
    """排除数字和符号后，密码只能由字母组成。"""
    pwd = generate_password(40, use_symbols=False, use_numbers=False)
    assert _is_only_from_alphabet(pwd, _LOWERCASE + _UPPERCASE)


def test_generate_password_with_numbers_no_symbols() -> None:
    """启用数字、关闭符号时，密码仅含字母+数字。"""
    pwd = generate_password(60, use_symbols=False, use_numbers=True)
    assert _is_only_from_alphabet(pwd, _LOWERCASE + _UPPERCASE + _DIGITS)
    assert not any(ch in _SYMBOLS for ch in pwd)


def test_generate_password_no_numbers_keeps_symbols() -> None:
    """关闭数字、启用符号时，密码不含数字。"""
    pwd = generate_password(60, use_numbers=False, use_symbols=True)
    assert not any(ch in _DIGITS for ch in pwd)


def test_generate_password_invalid_length() -> None:
    """非法长度必须抛 ValueError。"""
    try:
        generate_password(0)
    except ValueError:
        return
    raise AssertionError("期望抛出 ValueError，但未抛出")


def test_generate_passwords_count_and_lengths() -> None:
    """批量接口数量与长度必须正确。"""
    pwds = generate_passwords(5, 12)
    assert len(pwds) == 5
    assert all(len(p) == 12 for p in pwds)


def test_generate_passwords_invalid_count() -> None:
    """非法数量必须抛 ValueError。"""
    try:
        generate_passwords(0, 8)
    except ValueError:
        return
    raise AssertionError("期望抛出 ValueError，但未抛出")


def test_main_writes_to_console(capsys) -> None:
    """默认输出应打印到 stdout。"""
    code = main(["--length", "10", "--count", "2"])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert all(len(line) == 10 for line in out)


def test_main_writes_to_file(tmp_path) -> None:
    """指定 --output 时应写入文件。"""
    out_file = tmp_path / "pwds.txt"
    code = main(["--length", "8", "--count", "3", "--output", str(out_file)])
    assert code == 0
    contents = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 3
    assert all(len(line) == 8 for line in contents)


def test_main_invalid_length_returns_error(capsys) -> None:
    """非法长度应返回非 0 退出码。"""
    code = main(["--length", "0"])
    assert code != 0


if __name__ == "__main__":
    # 直接运行脚本：执行命令行入口
    raise SystemExit(main())