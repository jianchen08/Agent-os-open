# @feature: FP-GATE 覆盖率棘轮门禁 render.card 词表同步闸单测 | @ci: python-coverage
"""generate_render_card_vocabulary.py / check_render_card_vocabulary_sync.py 单测。

真源/生成物/闸三方契约（断输入→输出，不钉实现）：
- 真源 = 插件侧两声明面（plugin.json render.card + dsh_adapter translator 卡型
  映射）并集；两面各自非空且互有独占值（plugin 面独占 form 族、translator 面
  独占 diff）=「diff 非单侧值」的双面实证；
- 生成确定性：同声明重复渲染零 diff（幂等 = 闸绿前提）；
- 随动（删值实验正向）：声明面增/删卡型 → collect_cards 集合随动（前端类型
  面经生成物随动的编译期执法由 PAYLOAD_BUILDERS Record 承担，见 vitest）；
- DSH 面投影（AR-M5 遗留②）：collect_dsh_cards = translator 映射面单独投影
  （字典序、并集子集），dshAdapter 贡献装载校验消费；DSH 面漂移未再生成
  同样闸红；
- 注入假卡型（注入实验）：合法格式词表外值 → 扫描随动收录（真值=声明面
  并集），未再生成 → 闸红；再生成后前端侧红由 tsc 执法（类型面扩张而
  payload 构造器缺位）；非法格式值 → 渲染边界拒绝产出（fail-closed）；
- 闸行为：幂等绿 / 手改生成物红 / 生成物缺失红 / 声明漂移未再生成红；
- 负控制（防恒绿）：真实仓生成一致 → 闸子进程实跑退出 0。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

# 动态加载被测脚本（与 test_check_kernel_port_sync.py 同构）
_SPEC = importlib.util.spec_from_file_location(
    "generate_render_card_vocabulary", SCRIPTS / "generate_render_card_vocabulary.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
gen = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gen
_SPEC.loader.exec_module(gen)


# ── 真源契约：两声明面并集，diff 双面化 ───────────────────────────────


def test_plugin_face_and_translator_face_both_contribute() -> None:
    """两面各自非空且互有独占值：词表并集的每一面都不可缺（缺面即漂移源）。"""
    plugin_cards = gen._scan_plugin_json(gen.PLUGINS_DIR)
    translator_cards = gen._scan_dsh_translator(gen.DSH_TRANSLATOR_PY)
    assert plugin_cards, "plugin.json 声明面不得为空"
    assert translator_cards, "translator 卡型映射面不得为空"
    # form 族仅插件清单面声明（第一方表单卡），diff 仅 translator 面产出
    # （DSH edit/write toolview 映射）——两面独占值即「diff 非单侧值」实证
    assert "form" in plugin_cards
    assert "form" not in translator_cards
    assert "diff" in translator_cards
    assert "diff" not in plugin_cards


def test_collect_cards_is_union_sorted() -> None:
    cards = gen.collect_cards()
    assert cards == sorted(cards), "词表须字典序（生成确定性）"
    assert set(cards) == gen._scan_plugin_json(gen.PLUGINS_DIR) | gen._scan_dsh_translator(
        gen.DSH_TRANSLATOR_PY
    )


def test_real_repo_dsh_face_projection_subset_sorted() -> None:
    """真实仓 DSH 面投影（collect_dsh_cards，AR-M5 遗留②）：非空、字典序、
    并集子集；translator 面独占 diff 在位、plugin 面独占 form 不在——
    面边界即 dshAdapter 贡献装载校验边界。"""
    dsh_cards = gen.collect_dsh_cards()
    assert dsh_cards, "DSH 映射面投影不得为空"
    assert dsh_cards == sorted(dsh_cards), "DSH 面子集须字典序（生成确定性）"
    assert set(dsh_cards) <= set(gen.collect_cards()), "面投影不得出并集界"
    assert "diff" in dsh_cards
    assert "form" not in dsh_cards
    assert "DSH_RENDER_CARDS" in gen.FRONTEND_TS.read_text(encoding="utf-8")


def test_render_deterministic() -> None:
    """性质断言：同词表同输出（无时间戳等扰动），重复渲染零 diff。"""
    cards, dsh_cards = gen.collect_cards(), gen.collect_dsh_cards()
    assert gen.render_ts(cards, dsh_cards) == gen.render_ts(cards, dsh_cards)


def test_render_rejects_empty_and_invalid_values() -> None:
    """fail-closed：任一面空词表 / 非法卡型值（大写、数字头、空格、连字符）
    拒绝渲染，绝不产出坏生成物。"""
    cards, dsh_cards = gen.collect_cards(), gen.collect_dsh_cards()
    with pytest.raises(ValueError, match="词表为空"):
        gen.render_ts([], dsh_cards)
    with pytest.raises(ValueError, match="词表为空"):
        gen.render_ts(cards, [])
    for bad in ("Hologram", "1card", "bad card", "card-dash"):
        with pytest.raises(ValueError, match="卡型值非法"):
            gen.render_ts(sorted(gen._scan_plugin_json(gen.PLUGINS_DIR) | {bad}), dsh_cards)
        with pytest.raises(ValueError, match="卡型值非法"):
            gen.render_ts(cards, sorted(gen._scan_dsh_translator(gen.DSH_TRANSLATOR_PY) | {bad}))


# ── 随动 + 注入（删值实验双向，tmp 声明面夹具）───────────────────────


def _make_fixture(plugins_dir: Path, translator_py: Path) -> tuple[Path, Path]:
    """构造最小插件侧声明面：一个 plugin.json 声明 + translator 卡型映射。"""
    plugin_dir = plugins_dir / "fixture_plugin"
    plugin_dir.mkdir(parents=True)
    manifest = {
        "id": "fixture_plugin",
        "capabilities": {
            "tools": [
                {"name": "t1", "render": {"card": "table"}},
                {"name": "t2", "render": {"card": "form"}},
            ]
        },
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    translator_py.write_text(
        "DSH_TOOLVIEW_COMPONENT_MAP = {'edit': {'card': 'diff'}, 'read': {'card': 'read'}}\n"
        "_DSH_FALLBACK_MAPPING = {'card': 'generic'}\n",
        encoding="utf-8",
    )
    return plugins_dir, translator_py


def test_truth_change_moves_generated_file(tmp_path: Path) -> None:
    """随动（删值实验正向）：插件声明删卡型 → 生成物随动收缩。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    translator_py = plugins_dir / "translator.py"
    plugins_dir, translator_py = _make_fixture(plugins_dir, translator_py)
    out = tmp_path / "vocab.generated.ts"

    before = gen.write_outputs(out, plugins_dir, translator_py)
    assert "table" in before
    assert "diff" in before

    manifest_path = plugins_dir / "fixture_plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"]["tools"] = [
        t for t in manifest["capabilities"]["tools"] if t["render"]["card"] != "table"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    after = gen.write_outputs(out, plugins_dir, translator_py)
    assert "table" not in after
    assert "form" in after
    assert "table" not in out.read_text(encoding="utf-8")


def test_fake_card_injection_follows_scan_and_gate_red_without_regen(tmp_path: Path) -> None:
    """注入假卡型（注入实验，合法格式值）：声明面注入词表外值 → 扫描随动
    收录（真值=声明面，并集语义）；落盘生成物未再生成 → 闸红（退出 1）。
    再生成后前端侧的红由 tsc 执法（类型面扩张而 payload 构造器缺位，
    见 vitest 词汇表 lockstep 测试），本层不重复执法。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    assert gen.check_files(out, plugins_dir, translator_py) == 0

    manifest_path = plugins_dir / "fixture_plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"]["tools"].append({"name": "bad", "render": {"card": "hologram"}})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert "hologram" in gen.collect_cards(plugins_dir, translator_py), "扫描须随声明面注入随动"
    assert gen.check_files(out, plugins_dir, translator_py) == 1, "未再生成 → 闸必须红"

    gen.write_outputs(out, plugins_dir, translator_py)
    assert "hologram" in out.read_text(encoding="utf-8")
    assert gen.check_files(out, plugins_dir, translator_py) == 0


def test_malformed_card_injection_red_at_render_boundary(tmp_path: Path) -> None:
    """注入非法格式卡型（大写）→ 渲染边界拒绝产出，闸同路径异常外溢
    （非零退出）——坏值进不了前端类型面（fail-closed）。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)

    manifest_path = plugins_dir / "fixture_plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"]["tools"].append({"name": "bad", "render": {"card": "Hologram"}})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="卡型值非法"):
        gen.write_outputs(out, plugins_dir, translator_py)
    with pytest.raises(ValueError, match="卡型值非法"):
        gen.check_files(out, plugins_dir, translator_py)


def test_valid_card_change_gate_red_without_regen(tmp_path: Path) -> None:
    """合法卡型漂移未再生成 → 闸红（退出 1）。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    assert gen.check_files(out, plugins_dir, translator_py) == 0

    manifest_path = plugins_dir / "fixture_plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"]["tools"] = [
        t for t in manifest["capabilities"]["tools"] if t["render"]["card"] != "table"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert gen.check_files(out, plugins_dir, translator_py) == 1
    gen.write_outputs(out, plugins_dir, translator_py)
    assert gen.check_files(out, plugins_dir, translator_py) == 0


def test_dsh_face_change_moves_generated_file_and_gate_red(tmp_path: Path) -> None:
    """随动（删值实验，DSH 映射面，AR-M5 遗留②）：translator 卡型改名 →
    并集与 DSH 面子集同步随动；未再生成 → 闸红（手抄 VALID_CARDS 时代
    挡不住的漂移源）。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    translator_py = plugins_dir / "translator.py"
    plugins_dir, translator_py = _make_fixture(plugins_dir, translator_py)
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    assert gen.check_files(out, plugins_dir, translator_py) == 0

    translator_py.write_text(
        "DSH_TOOLVIEW_COMPONENT_MAP = {'edit': {'card': 'patch'}, 'read': {'card': 'read'}}\n"
        "_DSH_FALLBACK_MAPPING = {'card': 'generic'}\n",
        encoding="utf-8",
    )

    assert "patch" in gen.collect_dsh_cards(translator_py)
    assert "diff" not in gen.collect_dsh_cards(translator_py)
    assert "patch" in gen.collect_cards(plugins_dir, translator_py), "DSH 面漂移须随动并集"
    assert gen.check_files(out, plugins_dir, translator_py) == 1, "DSH 面漂移未再生成 → 闸必须红"

    gen.write_outputs(out, plugins_dir, translator_py)
    assert "'patch'," in out.read_text(encoding="utf-8")
    assert gen.check_files(out, plugins_dir, translator_py) == 0


# ── 闸行为：幂等绿 / 手改红 / 缺失红 ─────────────────────────────────


def test_write_outputs_idempotent(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    first = out.read_bytes()
    gen.write_outputs(out, plugins_dir, translator_py)
    assert out.read_bytes() == first
    assert gen.check_files(out, plugins_dir, translator_py) == 0


def test_check_red_on_hand_edited_generated_file(tmp_path: Path) -> None:
    """手改生成物（增/删一个卡型字面量）→ 闸红。"""
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    tampered = out.read_text(encoding="utf-8").replace("'diff',", "")
    assert tampered != out.read_text(encoding="utf-8"), "漂移值须与真值可区分"
    out.write_text(tampered, encoding="utf-8")
    assert gen.check_files(out, plugins_dir, translator_py) == 1


def test_check_red_on_missing_file(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    out = tmp_path / "vocab.generated.ts"
    gen.write_outputs(out, plugins_dir, translator_py)
    out.unlink()
    assert gen.check_files(out, plugins_dir, translator_py) == 1


def test_scan_fail_closed_on_empty_or_broken_faces(tmp_path: Path) -> None:
    """扫描面破坏（清单为空 / translator 缺映射 / 映射条目缺 card）→ 抛错。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="扫描面为空"):
        gen._scan_plugin_json(empty)

    plugins_dir = tmp_path / "plugins" / "shared"
    plugins_dir.mkdir(parents=True)
    plugins_dir, translator_py = _make_fixture(plugins_dir, plugins_dir / "translator.py")
    translator_py.write_text("OTHER = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="卡型映射缺失或为空"):
        gen._scan_dsh_translator(translator_py)

    translator_py.write_text(
        "DSH_TOOLVIEW_COMPONENT_MAP = {'edit': {}}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="条目缺 card"):
        gen._scan_dsh_translator(translator_py)


# ── 真实仓负控制（防恒绿）─────────────────────────────────────────────


def test_real_repo_gate_green_and_generated_header_present() -> None:
    """真实仓状态：闸子进程实跑退出 0；生成物带 DO-NOT-EDIT 头。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_render_card_vocabulary_sync.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DO NOT EDIT" in gen.FRONTEND_TS.read_text(encoding="utf-8")


def test_real_repo_vocabulary_has_both_faces() -> None:
    """真实仓词表 = 两声明面并集（11 值现值），类型随动的真源锚。"""
    cards = set(gen.collect_cards())
    assert cards == gen._scan_plugin_json(gen.PLUGINS_DIR) | gen._scan_dsh_translator(
        gen.DSH_TRANSLATOR_PY
    )
    assert "diff" in cards
    assert "form" in cards
