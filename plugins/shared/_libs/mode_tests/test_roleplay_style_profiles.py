# @feature: FP-0.2.〇 RP引擎(状态通道schema化) | @vision: V1 可进化 | @ci: python-coverage
"""mode_roleplay 文风标准档（styles/）schema 契约测试（C1 整合刀 2026-10-13）。

四档出厂种子逐文件真实读取（不 mock 文件面），锁定契约：
- 五字段缺一不可（id/name/traits/authors/sample_note），id 与文件名一致且唯一；
- traits/authors 为非空字符串列表；sample_note 80-150 字（原创整合文案约束，
  空窗或超长都说明档位文案失守，考据回链与许可纪律见各 yaml 头注释）；
- 档数 3-5（C1 任务书定档）；C2 开演向导按此 schema 消费。

test_mode_seeds.py 同款装载形态：真实文件读取，无 mock。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_MODES_DIR = Path(__file__).resolve().parents[2]
_STYLES_DIR = _MODES_DIR / "mode_roleplay" / "styles"

pytestmark = pytest.mark.unit


def _style_files() -> list[Path]:
    return sorted(path for path in _STYLES_DIR.iterdir() if path.suffix == ".yaml")


def _load_profile(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        profile: dict[str, object] = yaml.safe_load(fh)
    assert isinstance(profile, dict), f"{path.name} 须为映射"
    return profile


def test_style_profiles_present_in_range() -> None:
    files = _style_files()
    assert 3 <= len(files) <= 5, "任务书定档 3-5 个标准文风档"


def test_style_profile_ids_unique_and_filename_aligned() -> None:
    files = _style_files()
    ids = [str(_load_profile(path)["id"]) for path in files]
    assert len(ids) == len(set(ids)), "id 全局唯一（向导引用键）"
    for path, profile_id in zip(files, ids, strict=True):
        assert profile_id == path.stem, "id 与文件名一致"


@pytest.mark.parametrize("style_path", _style_files(), ids=lambda p: p.name)
def test_style_profile_schema(style_path: Path) -> None:
    profile = _load_profile(style_path)
    assert set(profile) == {"id", "name", "traits", "authors", "sample_note"}
    assert isinstance(profile["name"], str)
    assert bool(profile["name"])
    traits = profile["traits"]
    assert isinstance(traits, list), "特征清单须为列表"
    assert bool(traits), "特征清单非空"
    assert all(isinstance(item, str) and item for item in traits)
    authors = profile["authors"]
    assert isinstance(authors, list), "参考作家须为列表"
    assert bool(authors), "参考作家非空"
    assert all(isinstance(item, str) and item for item in authors)
    note = profile["sample_note"]
    assert isinstance(note, str)
    # 字数按中文惯例去空白计（换行/缩进来自 yaml 折叠排版，不占字数）
    assert 80 <= len("".join(note.split())) <= 150, "示例风格说明 80-150 字"


def test_four_factory_profiles_anchored() -> None:
    """出厂四档锚点在场（防误删致向导选项静默缩水）。"""
    ids = {str(_load_profile(path)["id"]) for path in _style_files()}
    assert {"concrete_plain", "literary_figurative", "hard_minimal", "webnovel_paced"} <= ids
