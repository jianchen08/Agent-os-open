"""许可证闸门（治理面，fail-closed）——外部包进系统前的第一道拒收位。

放行清单 = 宽松许可（MIT / Apache-2.0 / BSD-2/3-Clause / ISC / CC0-1.0）；
``Proprietary``、缺失、清单外一律拒收，拒收理由里回带发现的许可证字符串
（调用方可读、可归因）。

匹配口径：去首尾空白后大小写不敏感比对，回填清单规范拼写（``mit`` → ``MIT``）
——清单外的近似形态（``MIT License`` 等单值不可判定语义）不猜，一律拒收。

「缺席」与「已知不可用」两分：``allow_undeclared`` 显式开关**只覆盖缺席**
（``license`` 为 None/空串）——未知许可证（如 84% 的 Claude Code 插件未在清单
声明 license）经显式开关放行；``Proprietary`` 等清单外已知形态即使开关打开
也照旧拒收（开关不是免死金牌）。``license_file`` 只是事实位：发现许可证文件
也不自动采用，只在理由里如实提示（见 ADR 2026-10-17-undeclared-license-import-policy）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ALLOWED_LICENSES", "LicenseGate", "check_license"]

ALLOWED_LICENSES: tuple[str, ...] = (
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "CC0-1.0",
)


@dataclass(frozen=True)
class LicenseGate:
    """闸门结论（``license`` 为清单规范拼写；缺失时 None）。"""

    license: str | None
    allowed: bool
    reason: str


def check_license(value: str | None, *, license_file: str | None = None, allow_undeclared: bool = False) -> LicenseGate:
    """裁决许可证值能否进系统（纯函数，无副作用）。

    Args:
        value: 清单声明的许可证（None/空串 = 未声明）。
        license_file: 源目录/仓库根发现的许可证文件名（事实位，仅用于拒收理由提示）。
        allow_undeclared: 显式开关：True 时放行「未声明」；不影响清单内外判定。
    """
    raw = (value or "").strip()
    if not raw:
        hint = f"发现 {license_file}（未自动采用）" if license_file else "未发现 LICENSE 文件"
        if allow_undeclared:
            return LicenseGate(None, True, f"放行：许可证未声明（显式开关 allow_undeclared_license；{hint}）")
        return LicenseGate(None, False, f"拒收：许可证缺失（manifest 未声明 license；{hint}）")
    for allowed in ALLOWED_LICENSES:
        if raw.casefold() == allowed.casefold():
            return LicenseGate(allowed, True, f"放行：{allowed}")
    return LicenseGate(raw, False, f"拒收：{raw} 不在允许清单（{'/'.join(ALLOWED_LICENSES)}）内")
