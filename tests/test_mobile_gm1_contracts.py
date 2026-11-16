# @feature: FP-0.2.移动端适配 GM1 契约 | @ci: python-coverage
"""G-M1 契约测试（Phase 1 §1.0 车道，门禁反推实现步）。

已实现族的类即该实现步的**回归验收面**（实现与删 skip 同刀 commit）；
尚未实现的功能以 skip 门控（skip 原因携带 lane id 与转绿条件），
现役 CI 全绿（skip 不计红）。

接缝说明：各契约按当前架构公理指定**临时接缝**（SDK 纯函数 / 系统插件
模块），实现步若经 ADR 调整落点，契约断言随实现搬家、断言本身不松。

车道与状态（详见 docs/working/CI车道_台账.md）：
- G-M1.1 合宿数=f(核数,内存档) 表驱动 —— **已转绿**：实现落
  agentos_plugin_sdk.device_profile（plugins/sdk），本类为回归面。
- G-M1.2 能力门禁表解析器 —— **已转绿**：实现落
  plugins/shared/capability_gates/gates.py（平铺布局；契约原按旧桶式
  路径 plugins/shared/system/capability_gates 预选接缝，随 ADR
  2026-10-13-plugin-flat-layout 调整，断言未松），本类为回归面。
- G-M1.3 rootfs manifest schema —— 不在本文件：schema/验面已随
  deploy/artifacts_manifest.py 落地为绿测试（tests/test_mobile_artifacts_manifest.py）；
  "digest 不匹配=拒绝启动"的启动面集成归 G-M2 AVD 车道与装机步验收。
- G-M1.4 sync 事件 schema（Phase 2b） —— **已转绿**（2026-10-15 2b 同步实现刀，
  实现落共享裸模块 plugins/shared/sync_event.py + hlc_clock.py，本类为回归面）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestCohostProfileContract:
    """原则 P2：宿主数=f(设备档案)，启动时探测；手机 1-2 / 桌面 4 / 云独立档。

    契约只钉档位边界与单调性，不钉 f() 在手机档 1-2 内的取值自由度；
    云独立档=显式 tier 时硬件维度不影响计划（同 tier 同计划）。
    桌面钉值=桌面合宿收敛现实（槽容量 20 + pipeline 归并 light →
    2×light + light_stable + longwait = 4），与已部署桌面/内核运行时执法同源。
    """

    @staticmethod
    def _plan(cores: int, memory_gb: float, tier: str | None = None) -> int:
        from agentos_plugin_sdk.device_profile import DeviceProfile, plan_hosts

        profile = DeviceProfile(cores=cores, memory_gb=memory_gb, tier=tier)
        plan = plan_hosts(profile)
        assert plan.host_count >= 1, "任何档案至少 1 个宿主"
        return plan.host_count

    def test_phone_low_profile_yields_one_or_two_hosts(self) -> None:
        assert 1 <= self._plan(cores=4, memory_gb=4.0) <= 2

    def test_phone_boundary_8c8g_inclusive_is_phone_tier(self) -> None:
        """边界含于手机档：≤8核且≤8GB（P2 原则原文），不得越到桌面档。"""
        assert 1 <= self._plan(cores=8, memory_gb=8.0) <= 2

    @pytest.mark.parametrize(
        ("cores", "memory_gb"),
        [(8, 16.0), (16, 8.0), (16, 32.0)],
        ids=["mem-overflow", "core-overflow", "both-above"],
    )
    def test_desktop_tier_is_four(self, cores: int, memory_gb: float) -> None:
        """任一维度越过手机上界即桌面档=4（合宿收敛现实钉值，对齐部署面）。"""
        assert self._plan(cores, memory_gb) == 4

    def test_explicit_cloud_tier_is_hardware_independent(self) -> None:
        """云独立档：显式 tier 时同档同计划，硬件维度不得影响结果。"""
        small = self._plan(cores=4, memory_gb=4.0, tier="cloud")
        large = self._plan(cores=64, memory_gb=256.0, tier="cloud")
        assert small == large

    def test_resource_monotonicity(self) -> None:
        """性质：资源升档宿主数不回退（低配不得比高配排更多宿主）。"""
        low = self._plan(cores=4, memory_gb=4.0)
        boundary = self._plan(cores=8, memory_gb=8.0)
        high = self._plan(cores=16, memory_gb=32.0)
        assert low <= boundary <= high


class TestCapabilityGatesContract:
    """能力门禁表 config/plugins/capability_gates.yaml（原则 P1：不裁集合、运行时降级）。

    schema：顶层 mapping plugin_id -> {min_profile, degrade_reason}；
    min_profile 词表 {"phone","desktop","cloud"}；degrade_reason 非空字符串。
    行为契约：文件缺失=fail-open（全量默认不降级）；字段非法=fail-closed 报错。
    """

    @staticmethod
    def _write_gates(tmp_path: Path, payload: object) -> Path:
        import yaml

        path = tmp_path / "capability_gates.yaml"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return path

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, object]]:
        import sys

        gates_dir = Path(__file__).resolve().parent.parent / "plugins" / "shared" / "capability_gates"
        if str(gates_dir) not in sys.path:
            sys.path.insert(0, str(gates_dir))
        from gates import load_capability_gates

        return load_capability_gates(path)

    def test_missing_file_fails_open(self, tmp_path: Path) -> None:
        """文件不存在=全量默认不降级（返回空门禁集，不抛错）。"""
        gates = self._load(tmp_path / "capability_gates.yaml")
        assert gates == {}

    def test_valid_table_parses_with_vocabulary(self, tmp_path: Path) -> None:
        """合法表解析出 plugin→gate；性质：min_profile 在词表内且 reason 非空。"""
        path = self._write_gates(
            tmp_path,
            {
                "llm": {"min_profile": "desktop", "degrade_reason": "venv 200MB 超 手机档预算"},
                "browser": {"min_profile": "cloud", "degrade_reason": "需桌面级网络栈"},
                "monitoring": {"min_profile": "phone", "degrade_reason": "低端机轮询降频"},
            },
        )

        gates = self._load(path)

        assert set(gates) == {"llm", "browser", "monitoring"}
        for gate in gates.values():
            assert gate["min_profile"] in {"phone", "desktop", "cloud"}
            assert isinstance(gate["degrade_reason"], str)
            assert gate["degrade_reason"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"llm": {"min_profile": "watch", "degrade_reason": "x"}},
            {"llm": {"min_profile": "phone"}},
            {"llm": {"min_profile": "phone", "degrade_reason": ""}},
            {"llm": {"min_profile": 3, "degrade_reason": "x"}},
            {"llm": {"min_profile": "phone", "degrade_reason": "x", "unknown_field": 1}},
        ],
        ids=[
            "min-profile-out-of-vocabulary",
            "missing-degrade-reason",
            "empty-degrade-reason",
            "min-profile-wrong-type",
            "unknown-field",
        ],
    )
    def test_invalid_fields_fail_closed(self, tmp_path: Path, payload: dict) -> None:
        """字段非法（词表外/缺字段/空 reason/类型违约/未知字段）一律报错。

        接缝未定版，错误类型只约束 ValueError 族（schema 校验错惯例），
        排除 yaml 解析错冒充 schema 错。
        """
        import yaml

        path = self._write_gates(tmp_path, payload)

        with pytest.raises((ValueError, TypeError, KeyError)) as excinfo:
            self._load(path)
        assert type(excinfo.value) is not yaml.YAMLError, "必须是 schema 校验错而非解析错"

    def test_degraded_lookup_semantics(self, tmp_path: Path) -> None:
        """门禁命中=降级，未命中=不降级：解析结果是查表语义而非全量清单。"""
        path = self._write_gates(
            tmp_path,
            {"llm": {"min_profile": "desktop", "degrade_reason": "内存超预算"}},
        )

        gates = self._load(path)

        assert "llm" in gates
        assert "monitoring" not in gates


class TestSyncEventContract:
    """Phase 2b 同步事件契约（就绪约束 #3：全 ULID 主键+单调时间戳）。

    schema：sync_event 必含 ulid（主键）/device_id/asset_type/version/updated_at；
    cursor 拉取语义：since cursor 之后严格递增返回；幂等重放：同 ULID 二次
    apply 不产生重复。

    接缝注记（2026-10-15 2b 落地）：临时接缝 plugins/shared/system/sync_engine
    随 ADR 2026-10-13-plugin-flat-layout 调整为共享根裸模块
    plugins/shared/sync_event.py（G-M1.2 capability_gates 同款先例），
    断言未松。
    """

    @staticmethod
    def _event(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "ulid": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "device_id": "device-a",
            "asset_type": "config",
            "version": 3,
            "updated_at": "2026-10-13T08:00:00+00:00",
            "payload": {"llm.yaml": "providers: {}"},
        }
        base.update(overrides)
        return base

    @staticmethod
    def _validate(raw: dict[str, object]) -> object:
        import sys

        shared_root = Path(__file__).resolve().parent.parent / "plugins" / "shared"
        if str(shared_root) not in sys.path:
            sys.path.insert(0, str(shared_root))
        from sync_event import validate_event

        return validate_event(raw)

    @pytest.mark.parametrize(
        "missing_field",
        ["ulid", "device_id", "asset_type", "version", "updated_at"],
    )
    def test_missing_required_field_rejected(self, missing_field: str) -> None:
        """五必填缺一即拒（fail-closed），逐字段参数化。

        接缝未定版，错误类型只约束 ValueError 族（schema 校验错惯例）。
        """
        raw = self._event()
        del raw[missing_field]

        with pytest.raises((ValueError, TypeError, KeyError)):
            self._validate(raw)

    def test_ulid_format_property(self) -> None:
        """性质：ulid 必须是 26 位 Crockford Base32（跨端 ULID 主键的前提）。"""
        crockford = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        event = self._validate(self._event())
        assert len(event.ulid) == 26
        assert set(event.ulid.upper()) <= crockford

    def test_valid_event_roundtrip(self) -> None:
        event = self._validate(self._event())
        assert event is not None

    @staticmethod
    def _store() -> object:
        import sys

        shared_root = Path(__file__).resolve().parent.parent / "plugins" / "shared"
        if str(shared_root) not in sys.path:
            sys.path.insert(0, str(shared_root))
        from sync_event import SyncStore

        return SyncStore()

    def test_cursor_pull_is_monotonic(self) -> None:
        """性质：since cursor 之后的事件严格大于 cursor，且按 cursor 升序返回；
        推进 cursor 连续拉取不回放旧事件。"""
        store = self._store()
        ulids = [
            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "01ARZ3NDEKTSV4RRFFQ69G5FAW",
            "01ARZ3NDEKTSV4RRFFQ69G5FBX",
        ]
        for i, ulid in enumerate(ulids):
            store.upsert(self._event(ulid=ulid, version=i + 1))

        first = store.pull(since_cursor=0)
        assert [e.cursor for e in first] == sorted(e.cursor for e in first)
        assert all(e.cursor > 0 for e in first)

        watermark = first[-1].cursor
        rest = store.pull(since_cursor=watermark)
        assert all(e.cursor > watermark for e in rest)

    def test_replay_same_ulid_is_idempotent(self) -> None:
        """性质：同 ULID 重放（断网续传重发场景）不产生重复副本。"""
        store = self._store()
        event = self._event(ulid="01ARZ3NDEKTSV4RRFFQ69G5FAV")

        store.upsert(event)
        first_pull = store.pull(since_cursor=0)
        store.upsert(event)
        second_pull = store.pull(since_cursor=0)

        assert len(first_pull) == 1
        assert len(second_pull) == 1
        assert second_pull[0].ulid == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
