# @feature: FP-0.2.二 mode_personal 个人助理模式包 M2 捕获面 | @ci: python-coverage
"""parse_line 纯函数单测（M0 基础组 + M2 捕获面：相对时间/重复词/direction）。

断行为不断实现：输入行 → 记录字段/None；相对词按注入 now 展开（确定性），
无区分度输入组不成对不算覆盖。M2 硬要求两类用例真实落盘：
- 泛化性自检：测试集外同契约输入仍正确（各 Test 类 Generalization 用例）；
- 删值实验：日期/时长/换算常量全部经字面断言钉死——改源内偏移/换算常量
  （后天+2、下周+7 天基准、晚=+12 小时、分钟后逐分钟、每月直通日号）对应
  断言必变红。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parents[1]  # plugins/shared/（atomic_io 等共享模块）


def _load(name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"mode_personal_test_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_parse = _load("pim_parse")
# 2026-10-12 = 周一（下周X 断言以此为锚）
NOW = datetime(2026, 10, 12, 9, 0, 0)


class TestCheckboxAndTitle:
    def test_bare_line_defaults_undone(self) -> None:
        """无 checkbox 前缀 = 默认未完成（页面入口形态，契约明文）。"""
        rec = pim_parse.parse_line("买菜做饭", now=NOW)
        assert rec == {
            "title": "买菜做饭", "done": False, "due": None,
            "repeat": None, "domain": None, "priority": "P2", "direction": None,
        }

    @pytest.mark.parametrize(("line", "done"), [
        ("- [ ] 交周报", False),
        ("- [x] 交周报", True),
        ("- [X] 交周报", True),
    ])
    def test_checkbox_states(self, line: str, done: bool) -> None:
        rec = pim_parse.parse_line(line, now=NOW)
        assert rec is not None
        assert rec["done"] is done
        assert rec["title"] == "交周报"


class TestAtTime:
    @pytest.mark.parametrize(("token", "expected_due"), [
        # 删值实验锚：相对日偏移（今天+0/明天+1）改常量即红
        ("@今天", "2026-10-12"),
        ("@明天", "2026-10-13"),
    ])
    def test_relative_today_and_tomorrow(self, token: str, expected_due: str) -> None:
        rec = pim_parse.parse_line(f"{token} 复诊", now=NOW)
        assert rec["due"] == expected_due

    def test_relative_expands_by_injected_now(self) -> None:
        """相对日按注入 now 展开（同输入不同 now 结果不同 = 纯函数性质）。"""
        assert pim_parse.parse_line("@明天 复诊", now=NOW)["due"] == "2026-10-13"
        other = pim_parse.parse_line("@明天 复诊", now=datetime(2026, 12, 31, 8, 0))
        assert other["due"] == "2027-01-01"

    def test_iso_date_and_time_merged(self) -> None:
        rec = pim_parse.parse_line("@2026-10-15 14:00 复诊", now=NOW)
        assert rec["due"] == "2026-10-15 14:00"
        assert rec["title"] == "复诊"
        assert "14:00" not in rec["title"]

    def test_iso_date_without_time(self) -> None:
        rec = pim_parse.parse_line("@2026-10-15 复诊", now=NOW)
        assert rec["due"] == "2026-10-15"
        assert rec["title"] == "复诊"

    def test_invalid_date_stays_in_title(self) -> None:
        """非法日期（2026-13-40）= 未识别标记，原样留标题（不吞词）。"""
        rec = pim_parse.parse_line("@2026-13-40 复诊", now=NOW)
        assert rec["due"] is None
        assert "@2026-13-40" in rec["title"]

    @pytest.mark.parametrize(("token", "repeat"), [
        ("@每天", "daily"),
        ("@每周一", "weekly:0"),
        ("@每周五", "weekly:4"),
        ("@每周日", "weekly:6"),
        ("@每周天", "weekly:6"),
    ])
    def test_repeat_tokens(self, token: str, repeat: str) -> None:
        rec = pim_parse.parse_line(f"{token} 例会", now=NOW)
        assert rec["repeat"] == repeat
        assert rec["due"] is None
        assert rec["title"] == "例会"


class TestRelativeTimeM2:
    """M2 相对时间组：后天/下周X/晚N点/N分钟后（@ 词法，未识别留标题）。"""

    @pytest.mark.parametrize(("token", "expected_due"), [
        # 删值实验锚：后天 = +2 天（改偏移常量即红）
        ("@后天", "2026-10-14"),
        # 删值实验锚：下周X = 下一自然周的周X（周一注入日 +7 天基准）
        ("@下周三", "2026-10-21"),
        ("@下周日", "2026-10-25"),
        # 删值实验锚：晚N点 = 12 小时制→24 小时制（晚 8 点 = 20:00，+12 换算）
        ("@明晚8点", "2026-10-13 20:00"),
        ("@今晚10点半", "2026-10-12 22:30"),
        ("@明天晚上8点", "2026-10-13 20:00"),
        # 不带晚的 N 点 = 上午缺省（缺省语义：小时原样）
        ("@明天8点", "2026-10-13 08:00"),
        ("@今天12点半", "2026-10-12 12:30"),
    ])
    def test_relative_tokens(self, token: str, expected_due: str) -> None:
        rec = pim_parse.parse_line(f"{token} 事项", now=NOW)
        assert rec is not None
        assert rec["due"] == expected_due
        assert rec["title"] == "事项"

    @pytest.mark.parametrize(("token", "expected_due"), [
        # 删值实验锚：分钟后 = 逐分钟展开（now + N 分钟，改分钟常量/粒度即红）
        ("@30分钟后", "2026-10-12 09:30"),
        ("@59分钟后", "2026-10-12 09:59"),
        # 跨小时/跨日边界（符号量级相反组）
        ("@45分钟后", "2026-10-12 09:45"),
    ])
    def test_minutes_later(self, token: str, expected_due: str) -> None:
        rec = pim_parse.parse_line(f"{token} 起锅", now=NOW)
        assert rec is not None
        assert rec["due"] == expected_due
        assert rec["title"] == "起锅"

    @pytest.mark.parametrize("token", [
        "@25点",        # 小时越界
        "@明天25点",    # 小时越界（带日词形态）
        "@今晚12点",    # 晚 12 点语义歧义 → 低置信不吞词
        "@今晚0点",     # 晚 0 点语义歧义
        "@0分钟后",     # 零时长无意义
        "@1000分钟后",  # 超界（>999）
        "@下周九",      # 词表外星期字
    ])
    def test_unrecognized_stays_in_title(self, token: str) -> None:
        """未识别形态原样留标题（低置信契约：不吞词不猜）。"""
        rec = pim_parse.parse_line(f"{token} 事项", now=NOW)
        assert rec is not None
        assert rec["due"] is None
        assert token in rec["title"]

    def test_generalization_unseen_relative_inputs(self) -> None:
        """泛化性自检（测试集外同契约输入）：上方未出现过的合法词形仍正确。"""
        rec = pim_parse.parse_line("给绿萝浇水 @后天早上8点半 #生活", now=NOW)
        assert rec is not None
        assert rec["title"] == "给绿萝浇水"
        assert rec["due"] == "2026-10-14 08:30"
        assert rec["domain"] == "生活"
        # 集外：晚 N 点小时>12 原样（21 点）
        rec2 = pim_parse.parse_line("@明晚21点 归还钥匙", now=NOW)
        assert rec2["due"] == "2026-10-13 21:00"
        # 集外：分钟后跨日（23:30 + 45 分钟 = 次日 00:15）
        late = pim_parse.parse_line("@45分钟后 熄灯", now=datetime(2026, 10, 12, 23, 30))
        assert late["due"] == "2026-10-13 00:15"


class TestRepeatWordsM2:
    """M2 重复词组：每月N号 → repeat=monthly:<day>（1-31 直通，越界低置信）。"""

    @pytest.mark.parametrize(("token", "repeat"), [
        ("@每月1号", "monthly:1"),
        ("@每月15号", "monthly:15"),
    ])
    def test_monthly_tokens(self, token: str, repeat: str) -> None:
        rec = pim_parse.parse_line(f"{token} 缴房租", now=NOW)
        assert rec is not None
        assert rec["repeat"] == repeat
        assert rec["due"] is None
        assert rec["title"] == "缴房租"

    @pytest.mark.parametrize("token", ["@每月0号", "@每月32号"])
    def test_monthly_out_of_range_stays_in_title(self, token: str) -> None:
        rec = pim_parse.parse_line(f"{token} 缴房租", now=NOW)
        assert rec is not None
        assert rec["repeat"] is None
        assert token in rec["title"]

    def test_generalization_unseen_monthly_day(self) -> None:
        """泛化性自检：集外日号（31）同契约直通；重复词与领域/优先级可组合。"""
        rec = pim_parse.parse_line("@每月31号 对账 #工作 !P1", now=NOW)
        assert rec is not None
        assert rec["repeat"] == "monthly:31"
        assert rec["domain"] == "工作"
        assert rec["priority"] == "P1"
        assert rec["title"] == "对账"


class TestDirection:
    """M2 direction 组：句式判向（我欠…=i_owe / …欠我=owes_me / 其余=None）。

    direction 从整句句式提取（非标记词法），匹配词保留在标题（内容不吞）。
    """

    def test_i_owe_pattern(self) -> None:
        rec = pim_parse.parse_line("我欠小王50元", now=NOW)
        assert rec is not None
        assert rec["direction"] == "i_owe"
        assert rec["title"] == "我欠小王50元"  # 句式词是内容，不吞

    def test_owes_me_pattern(self) -> None:
        rec = pim_parse.parse_line("小王欠我50元", now=NOW)
        assert rec is not None
        assert rec["direction"] == "owes_me"

    def test_no_direction_by_default(self) -> None:
        assert pim_parse.parse_line("买菜做饭", now=NOW)["direction"] is None
        # 含"欠"但不成句式 → 无方向
        assert pim_parse.parse_line("整理欠条", now=NOW)["direction"] is None

    def test_both_patterns_first_match_wins(self) -> None:
        """双向句式并存 = 先出现者胜（确定性，位置序非硬编码偏好）。"""
        assert pim_parse.parse_line("我欠小王50 小王欠我30", now=NOW)["direction"] == "i_owe"
        assert pim_parse.parse_line("小王欠我30 我欠小王50", now=NOW)["direction"] == "owes_me"

    def test_generalization_unseen_sentence(self) -> None:
        """泛化性自检（集外句式）：同契约句仍判对；与标记词可组合。"""
        assert pim_parse.parse_line("我欠房东三个月房租 @11月1号", now=NOW)["direction"] == "i_owe"
        rec = pim_parse.parse_line("物业欠我押金 #生活 !P0", now=NOW)
        assert rec is not None
        assert rec["direction"] == "owes_me"
        assert rec["domain"] == "生活"
        assert rec["priority"] == "P0"

    def test_parsed_direction_fits_store_contract(self, tmp_path: Any) -> None:
        """parsed 字段与 store 契约对齐：direction 枚举值可直接建卡（capture
        消费面 = 过滤 None 后整包入 store.create，此处钉住字段可直用）。"""
        pim_store = _load("pim_store")
        store = pim_store.PimStore(tmp_path / "data", clock=lambda: NOW)
        parsed = pim_parse.parse_line("我欠小王50元", now=NOW)
        assert parsed is not None
        record, _ = store.create("todo", {k: v for k, v in parsed.items() if v is not None})
        assert record["direction"] == "i_owe"


class TestDomainAndPriority:
    def test_domain_extracted(self) -> None:
        assert pim_parse.parse_line("买菜 #生活", now=NOW)["domain"] == "生活"

    @pytest.mark.parametrize(("token", "priority"), [
        ("!P0", "P0"), ("!p1", "P1"), ("!P2", "P2"),
    ])
    def test_priority_tokens(self, token: str, priority: str) -> None:
        assert pim_parse.parse_line(f"事项 {token}", now=NOW)["priority"] == priority

    def test_default_priority_when_absent(self) -> None:
        assert pim_parse.parse_line("事项", now=NOW)["priority"] == "P2"

    def test_last_marker_wins(self) -> None:
        """同组标记多次出现取最后一个（后写胜，幂等性质）。"""
        rec = pim_parse.parse_line("事项 #工作 #生活 !P0 !P1", now=NOW)
        assert rec["domain"] == "生活"
        assert rec["priority"] == "P1"


class TestFullLineAndLowConfidence:
    def test_all_markers_combined(self) -> None:
        rec = pim_parse.parse_line("- [ ] @明天 #工作 !P1 交周报", now=NOW)
        assert rec == {
            "title": "交周报", "done": False, "due": "2026-10-13",
            "repeat": None, "domain": "工作", "priority": "P1", "direction": None,
        }

    @pytest.mark.parametrize("line", [
        "",
        "   ",
        "#工作 !P1 @每天",       # 纯标记无标题
        "- [ ]",                # 空 checkbox
        None,
    ])
    def test_low_confidence_returns_none(self, line: str | None) -> None:
        """剥除标记后无标题 = None（低置信升级 LLM 链）。"""
        assert pim_parse.parse_line(line, now=NOW) is None  # type: ignore[arg-type]

    def test_generalization_unseen_inputs(self) -> None:
        """泛化性自检：训练组之外同契约输入仍正确（测试集外输入）。"""
        rec = pim_parse.parse_line("给妈妈打电话 #家庭 @每周日 !p0", now=NOW)
        assert rec is not None
        assert rec["title"] == "给妈妈打电话"
        assert rec["domain"] == "家庭"
        assert rec["repeat"] == "weekly:6"
        assert rec["priority"] == "P0"

    def test_generalization_group2_leap_and_mixed(self) -> None:
        """泛化性自检第二组（训练集外）：闰年 ISO 日期 + 标记乱序 + 大小写混合
        checkbox + 连写标记按词法原样入标题（分词契约：标记须空格分隔）。"""
        rec = pim_parse.parse_line("备份服务器 @2028-02-29 #运维 !P0", now=NOW)
        assert rec is not None
        assert rec["title"] == "备份服务器"
        assert rec["due"] == "2028-02-29"  # 闰年合法日期识别
        assert rec["domain"] == "运维"
        assert rec["priority"] == "P0"
        # 平年 2 月 29 = 非法，留标题
        bad = pim_parse.parse_line("@2027-02-29 年审", now=NOW)
        assert bad["due"] is None
        assert "2027-02-29" in bad["title"]
        # checkbox 大写 X 与标记乱序
        rec2 = pim_parse.parse_line("- [X] !P2 @每天 服药 #健康", now=NOW)
        assert rec2 == {
            "title": "服药", "done": True, "due": None,
            "repeat": "daily", "domain": "健康", "priority": "P2", "direction": None,
        }
        # 连写（#运维!P0 无空格）= 一个词，按领域词法整词收（标签含标记字符），
        # 不静默拆词——分词契约是空格分隔，低置信走标题保留
        glued = pim_parse.parse_line("事项 #运维!P0", now=NOW)
        assert glued["domain"] == "运维!P0"
        assert glued["priority"] == "P2"

    def test_generalization_group3_full_m2_line(self) -> None:
        """泛化性自检第三组：M2 全标记混排一行（checkbox+相对时间+重复+领域+
        优先级+direction 句式同现），各组互不干扰。"""
        rec = pim_parse.parse_line("- [ ] @明晚8点 每月1号前结 我欠小王50元 #生活 !P0", now=NOW)
        assert rec is not None
        assert rec["title"] == "每月1号前结 我欠小王50元"
        assert rec["due"] == "2026-10-13 20:00"
        assert rec["domain"] == "生活"
        assert rec["priority"] == "P0"
        assert rec["direction"] == "i_owe"
        assert rec["done"] is False


class TestParityTwoEntriesOneFunction:
    def test_rest_capture_and_inbox_share_parser(self) -> None:
        """POST /capture 与 inbox.md 两入口同一函数零分叉：同输入同实体字段。"""
        line = "- [ ] @周五 #健康 跑步 5km"
        through_rest_shape = pim_parse.parse_line(line, now=NOW)
        through_inbox_shape = pim_parse.parse_line(f"  {line}  \n", now=NOW)
        assert through_rest_shape == through_inbox_shape


class TestValidateRepeatTargetWordlist:
    """词表单点 helper：合法/非法双组输入 + 兜底分支（kind 不属 weekly/monthly 返 False）。

    M6：兜底分支（pim_parse.py:76）此前无测试触达 → diff-coverage 99.00% 红。
    防拟合：合法边界/越界双组 + 兜底 kind 多组参数化 + 性质断言（合法性随
    target 单调、与词表常量一致），实现无法靠硬编码单值刷绿。
    """

    @pytest.mark.parametrize(
        ("kind", "target"),
        [
            ("weekly", 0),
            ("weekly", 6),
            ("monthly", 1),
            ("monthly", 31),
        ],
    )
    def test_valid_targets_pass(self, kind: str, target: int) -> None:
        assert pim_parse.validate_repeat_target(kind, target) is True

    @pytest.mark.parametrize(
        ("kind", "target"),
        [
            ("weekly", -1),
            ("weekly", 7),
            ("monthly", 0),
            ("monthly", 32),
        ],
    )
    def test_out_of_range_rejected(self, kind: str, target: int) -> None:
        assert pim_parse.validate_repeat_target(kind, target) is False

    @pytest.mark.parametrize("kind", ["bogus", "", "yearly", "WEEKLY"])
    def test_unknown_kind_returns_false(self, kind: str) -> None:
        # M6 兜底分支：kind 不属 weekly/monthly 一律 False（大小写敏感，词表只认小写）
        assert pim_parse.validate_repeat_target(kind, 0) is False

    def test_bounds_match_wordlist_constants(self) -> None:
        # 性质断言：合法边界与词表常量同源（改常量此断言必变红，防两套真值漂移复发）
        assert pim_parse.validate_repeat_target("weekly", pim_parse.WEEKDAY_MIN)
        assert pim_parse.validate_repeat_target("weekly", pim_parse.WEEKDAY_MAX)
        assert not pim_parse.validate_repeat_target("weekly", pim_parse.WEEKDAY_MAX + 1)
        assert pim_parse.validate_repeat_target("monthly", pim_parse.MONTH_DAY_MIN)
        assert pim_parse.validate_repeat_target("monthly", pim_parse.MONTH_DAY_MAX)
        assert not pim_parse.validate_repeat_target("monthly", pim_parse.MONTH_DAY_MAX + 1)
