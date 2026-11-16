"""npm 语义版本区间判定（engines.dsh 安装拦截与索引最新版本挑选的底座）。

npm node-semver 子集：本仓只消费两个面——``engines.dsh`` 兼容声明（安装前
拦截，设计 §4.1）与索引版本清单里的「最新精确版本」挑选（禁 dist-tag）。
Python 侧无成熟 npm 语义库（packaging 是 PEP 440 语义，prerelease 序与
caret/tilde 算子都不同），自持一个经过测试的最小实现。

支持语法：``||``（或）、空格/逗号分隔比较器（与）、``>=/./<=/</=``、
caret ``^``、tilde ``~``、``*``/空串（无约束）、裸版本（精确）。
prerelease 规则照 npm：区间比较器全部不含 prerelease 时，prerelease 版本
不满足区间；比较器含 prerelease 时仅与同 [major,minor,patch] 元组的
prerelease 版本参与匹配。
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(
    r"^[vV]?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(.*)$")


def parse_version(version: str) -> tuple[int, int, int, tuple]:
    """'1.2.3-rc.2' → (1, 2, 3, ('rc', 2))；缺省段补零；非法抛 ValueError。

    prerelease 段按 ``.`` 拆分：数字子段转 int（数值比较），其余保字符串
    （字典序比较）——npm semver 的 compareIdentifiers 规则。
    """
    m = _VERSION_RE.match(version.strip())
    if not m:
        raise ValueError(f"invalid semver: {version!r}")
    major = int(m.group("major"))
    minor = int(m.group("minor") or 0)
    patch = int(m.group("patch") or 0)
    pre_raw = m.group("pre")
    pre: list = []
    if pre_raw:
        for part in pre_raw.split("."):
            pre.append(int(part) if part.isdigit() else part)
    return (major, minor, patch, tuple(pre))


def _compare_prerelease(a: tuple, b: tuple) -> int:
    """prerelease 标识序列比较：无 prerelease > 有；逐段比，数字段数值、字符串段字典序、
    数字 < 字符串（npm 规则）；前缀相等时更长一段更大。"""
    if a == b:
        return 0
    if not a and b:
        return 1
    if a and not b:
        return -1
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return 1 if x > y else -1
        if isinstance(x, int):
            return -1  # 数字段 < 字符串段
        if isinstance(y, int):
            return 1
        return 1 if x > y else -1
    return 1 if len(a) > len(b) else -1


def compare_versions(a: str, b: str) -> int:
    """完整版本比较：先数值元组，后 prerelease。返回 -1/0/1。"""
    va, vb = parse_version(a), parse_version(b)
    na, nb = va[:3], vb[:3]
    if na != nb:
        return 1 if na > nb else -1
    return _compare_prerelease(va[3], vb[3])


def _caret_floor_ceil(version: str) -> tuple[str, str]:
    """^M.m.p → 下界=原版本，上界：<M+1.0.0（M>0）；<0.m+1.0（M=0，p 任意）；
    ^0.0.p → <0.0.(p+1)。照 npm caret-decoder 规则。"""
    major, minor, patch, pre = parse_version(version)
    if major > 0:
        ceil = f"{major + 1}.0.0"
    elif minor > 0:
        ceil = f"0.{minor + 1}.0"
    else:
        ceil = f"0.0.{patch + 1}"
    return (version, "<" + ceil)


def _tilde_floor_ceil(version: str) -> tuple[str, str]:
    """~M.m.p → >=M.m.p <M.(m+1).0；~M.m → >=M.m <M.(m+1).0；~M → >=M <(M+1).0。"""
    major, minor, patch, _pre = parse_version(version)
    return (version, f"<{major}.{minor + 1}.0")


def _comparator_bounds(spec: str) -> tuple[str, str, bool]:
    """单个比较器 → (op, version, has_prerelease_comparator)。

    caret/tilde 展开为两个 AND 比较器时返回带内部标记的复合（此函数只处理
    单比较器，caret/tilde 在 satisfies 里先展开）。
    """
    m = _COMPARATOR_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid comparator: {spec!r}")
    op = m.group(1) or "="
    ver = m.group(2).strip()
    if not ver or ver == "*":
        return ("*", "0.0.0", False)
    has_pre = parse_version(ver)[3] != ()
    return (op, ver, has_pre)


def _cmp_parsed(op: str, ver_parsed: tuple, target: str) -> bool:
    """已解析版本 vs 目标串的单比较器判定（元组直比，零字符串往返）。"""
    c = _cmp_to_parsed(ver_parsed, parse_version(target))
    if op == ">=":
        return c >= 0
    if op == ">":
        return c > 0
    if op == "<=":
        return c <= 0
    if op == "<":
        return c < 0
    return c == 0  # = 与裸版本


def _cmp_to_parsed(a: tuple, b: tuple) -> int:
    na, nb = a[:3], b[:3]
    if na != nb:
        return 1 if na > nb else -1
    return _compare_prerelease(a[3], b[3])


def satisfies(version: str, range_spec: str) -> bool:
    """版本是否满足 npm 区间。``range_spec`` 空串/``*`` = 无约束（恒真）。

    prerelease 排除律：候选版本是 prerelease 且本次匹配的比较器（含展开后的
    caret/tilde 下界）全部无 prerelease → 不匹配。
    """
    range_spec = (range_spec or "").strip()
    if range_spec in ("", "*", "x", "X"):
        return True
    vp = parse_version(version)
    is_pre = vp[3] != ()

    for or_branch in range_spec.split("||"):
        comparators: list[tuple[str, str, bool]] = []
        tokens = [t for t in re.split(r"[,\s]+", or_branch.strip()) if t]
        for tok in tokens:
            head = tok[:1] if tok[:1] in ("^", "~") else None
            if head:
                floor, ceil = (_caret_floor_ceil if head == "^" else _tilde_floor_ceil)(tok[1:] or tok)
                comparators.append(_comparator_bounds(">=" + floor))
                comparators.append(_comparator_bounds(ceil))
            else:
                comparators.append(_comparator_bounds(tok))
        ok = True
        for op, target, _comp_has_pre in comparators:
            if op == "*":
                continue
            if not _cmp_parsed(op, vp, target):
                ok = False
                break
        if ok and is_pre:
            # prerelease 排除律：任一参与比较的比较器带同元组 prerelease 即豁免；
            # 无 prerelease 比较器 → 本分支不匹配
            exempt = False
            for op, target, comp_has_pre in comparators:
                if op == "*" or not comp_has_pre:
                    continue
                tp = parse_version(target)
                if tp[:3] == vp[:3] and _cmp_parsed(op, vp, target):
                    exempt = True
                    break
            if not exempt:
                ok = False
        if ok:
            return True
    return False


def latest_version(versions: list[str]) -> str | None:
    """版本清单 → 最新精确版本（发行版优先；全 prerelease 取最高 prerelease）。

    非 semver 串静默跳过（索引脏数据不容错成异常）；全非法返回 None。
    """
    best_release: tuple | None = None
    best_release_ver: str | None = None
    best_pre: tuple | None = None
    best_pre_ver: str | None = None
    for v in versions or []:
        try:
            parsed = parse_version(v)
        except ValueError:
            continue
        if parsed[3]:
            if best_pre is None or compare_versions(v, best_pre_ver) > 0:  # type: ignore[arg-type]
                best_pre, best_pre_ver = parsed, v
        elif best_release is None or compare_versions(v, best_release_ver) > 0:  # type: ignore[arg-type]
            best_release, best_release_ver = parsed, v
    if best_release_ver is not None:
        return best_release_ver
    return best_pre_ver
