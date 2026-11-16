# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""npm_semver（npm 语义版本区间判定）——engines.dsh 安装拦截与索引最新精确版本挑选的底座。

契约（npm node-semver 行为，engines/range 消费口径）：
1. 版本解析：``major.minor[-prerelease]``（prerelease 以 ``.`` 分段，数字段按数值、
   字符串段按字典序比较；``rc.2 < rc.5``、``alpha.1 < rc.1``、release > 同元组 prerelease）；
2. 区间语法：``||`` = 或；逗号/空格分隔的比较器 = 与；比较器形态 ``>=/./<=/</=``、
   caret ``^``、tilde ``~``、裸版本（精确）；
3. prerelease 排除律（npm 关键语义）：区间所有比较器都不含 prerelease 时，
   prerelease 版本**不**满足该区间——``>=0.2.1`` 不匹配 ``0.2.1-rc.5``；
   比较器带 prerelease 且 [major,minor,patch] 元组相同才参与匹配——
   ``>=0.2.1-rc.2`` 匹配 ``0.2.1-rc.5``（rc.5 > rc.2），不匹配 ``0.2.2-alpha.1``；
4. caret/tilde 展开：``^0.2.1`` = ``>=0.2.1 <0.3.0``（0.x 锁 minor）；``~4.0.5`` = ``>=4.0.5 <4.1.0``。

测试纪律：字面值配性质断言、≥2 组有区分度输入；真实 npm 包元数据形态
（dsh-context engines.dsh 实测值）作回归样本。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from npm_semver import compare_versions, latest_version, parse_version, satisfies  # noqa: E402

pytestmark = pytest.mark.unit


class TestParseAndCompare:
    def test_release_ordering(self) -> None:
        assert compare_versions("1.2.3", "1.2.4") < 0
        assert compare_versions("4.0.5", "4.0.5") == 0
        assert compare_versions("4.0.5", "4.0.4") > 0
        assert compare_versions("0.2.1", "0.1.9") > 0

    def test_prerelease_ordering(self) -> None:
        # rc.5 > rc.2（数字段数值比较）；alpha < rc（字典序 a < r）
        assert compare_versions("0.2.1-rc.5", "0.2.1-rc.2") > 0
        assert compare_versions("0.2.1-alpha.1", "0.2.1-rc.1") < 0
        # 同元组 release > prerelease
        assert compare_versions("0.2.1", "0.2.1-rc.5") > 0
        # 低元组 release > 高元组 prerelease（先比数值元组）
        assert compare_versions("0.2.1-rc.5", "0.2.0-rc.2") > 0

    def test_parse_two_segment_version_pads_patch(self) -> None:
        # npm 宽容形态：缺省段补零
        assert parse_version("4.0") == (4, 0, 0, ())
        assert parse_version("0.2.1-rc.5") == (0, 2, 1, ("rc", 5))


class TestSatisfiesPrereleaseRule:
    """prerelease 排除律——engines.dsh 拦截判定的语义核心。"""

    def test_no_prerelease_comparator_excludes_prerelease_version(self) -> None:
        # 真值实证：node-semver@7.7.3 satisfies 同判（2026-11-04）
        assert satisfies("0.2.1-rc.5", ">=0.2.1") is False
        assert satisfies("0.2.1", ">=0.2.1") is True
        # 异元组 prerelease 比较器同样不豁免（npm 真值 >=0.2.0-rc.2 vs 0.2.1-rc.5 = false）
        assert satisfies("0.2.1-rc.5", ">=0.2.0-rc.2") is False

    def test_prerelease_comparator_matches_same_tuple(self) -> None:
        # dsh-context 实测 engines.dsh（npmmirror 2026-11-04；node-semver 真值同判）
        engines = ">=0.2.0-rc.2 || >=0.2.1-rc.2 || >=0.2.2-alpha.3"
        assert satisfies("0.2.1-rc.5", engines) is True
        assert satisfies("0.2.1-rc.1", engines) is False  # rc.1 < rc.2 且无同元组分支
        assert satisfies("0.2.2-alpha.4", engines) is True
        assert satisfies("0.1.0-rc.8", engines) is False

    def test_caret_range_basics(self) -> None:
        assert satisfies("4.0.5", "^4.0.1") is True
        assert satisfies("4.1.0", "^4.0.1") is True
        assert satisfies("5.0.0", "^4.0.1") is False
        # 0.x caret 锁 minor
        assert satisfies("0.3.0", "^0.2.1") is False
        assert satisfies("0.2.9", "^0.2.1") is True

    def test_tilde_and_exact_and_bare(self) -> None:
        assert satisfies("4.0.6", "~4.0.5") is True
        assert satisfies("4.1.0", "~4.0.5") is False
        assert satisfies("4.0.5", "4.0.5") is True
        assert satisfies("4.0.5", "*") is True
        assert satisfies("4.0.5", "") is True  # 无声明 = 无约束

    def test_and_comparators(self) -> None:
        # 与链中无同元组 prerelease 比较器 → prerelease 版本不匹配（npm 真值）
        assert satisfies("0.2.1-rc.5", ">=0.2.0-rc.2 <0.3.0-0") is False
        assert satisfies("0.3.0", ">=0.2.0-rc.2 <0.3.0-0") is False
        # 同元组比较器在链内 → 豁免成立
        assert satisfies("0.2.1-rc.5", ">=0.2.1-rc.2 <0.3.0-0") is True


class TestLatestVersion:
    def test_prefers_release_over_prerelease(self) -> None:
        # 发行版优先：索引「最新精确版本」不自动装 prerelease（npm latest 语义，
        # 全 prerelease 时才取最高 prerelease）
        assert latest_version(["0.2.1-rc.4", "0.2.0", "0.2.1-rc.5"]) == "0.2.0"
        assert latest_version(["1.0.0", "1.1.0-beta.1", "0.9.0"]) == "1.0.0"
        assert latest_version(["1.1.0-beta.1", "1.1.0-beta.2"]) == "1.1.0-beta.2"  # 全 prerelease 取最高

    def test_skips_garbage_entries(self) -> None:
        assert latest_version(["not-a-version", "1.2.3"]) == "1.2.3"
        assert latest_version([]) is None
        assert latest_version(["also-bad"]) is None
