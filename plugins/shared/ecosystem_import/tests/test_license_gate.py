# @feature: FP-0.2.外部生态导入 A批 | @ci: python-coverage
"""许可证闸门行为测试（fail-closed：清单内放行，缺失/清单外一律拒收）。"""

from __future__ import annotations

import pytest
from license_gate import ALLOWED_LICENSES, check_license

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0"])
def test_check_license_allows_allowlisted_values(value: str) -> None:
    gate = check_license(value)

    assert gate.allowed is True
    assert "放行" in gate.reason
    # 性质：回填的许可证值必落在放行清单内（不会原样透传未知形态）
    assert gate.license in ALLOWED_LICENSES


@pytest.mark.parametrize("value", ["mit", "apache-2.0", "  BSD-3-Clause  "])
def test_check_license_is_case_and_space_insensitive(value: str) -> None:
    gate = check_license(value)

    assert gate.allowed is True
    assert gate.license in ALLOWED_LICENSES


@pytest.mark.parametrize(
    ("value", "mentioned"),
    [
        ("Proprietary", "Proprietary"),
        ("GPL-3.0", "GPL-3.0"),
        ("MIT License", "MIT License"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_check_license_rejects_non_allowlisted(value: str | None, mentioned: str | None) -> None:
    gate = check_license(value)

    assert gate.allowed is False
    # 性质：拒收必带可读理由；「缺失」与「清单外」两种理由可辨别
    assert gate.reason.startswith("拒收")
    if mentioned is None:
        assert gate.license is None
        assert "缺失" in gate.reason
    else:
        assert mentioned in gate.reason
