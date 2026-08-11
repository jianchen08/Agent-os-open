"""主题动态清单 API 路由。

提供「无状态清单」接口：请求时扫描前端 public/themes/ 目录，返回可用主题的
元数据列表（id / name / url），不读取也不返回主题内容。前端拿到清单后自行
fetch 各主题 JSON，走现有的 importTheme 通道存入 localStorage。

设计原则（无状态清单，后端不拥有主题）：
- 后端只是「文件系统代理」，帮助浏览器绕开「不能扫描服务器目录」的限制
- 不存储主题、不持久化、不管理主题生命周期——主题内容仍归前端
- 不加 require_auth：清单是公开静态数据，前端首屏 initializeTheme 就要拉，
  带 token 会拖累冷启动（参照 app.py 里的 health check 也是无 auth）
- 主题文件用 JSON（区别于编译期打包的 .ts preset），存于前端 public/
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/themes",
    tags=["主题管理"],
    # 注意：不加 require_auth —— 清单是公开静态数据，首屏冷启动即拉取
)

# 项目根目录：src/channels/api/routes_themes.py → src/ → project_root/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# 主题 JSON 目录（前端 public/themes/，由 Vite 直接 serve 到站点根）
_THEMES_DIR = _PROJECT_ROOT / "frontend" / "public" / "themes"


class ThemeManifestItem(BaseModel):
    """清单中单个主题的元数据（不含主题内容）。"""

    id: str = Field(..., description="主题 ID（= 文件名去 .json）")
    name: str = Field(..., description="主题显示名（取自 JSON 内 name 字段）")
    url: str = Field(..., description="主题内容 URL（前端 public 根路径）")


def _extract_meta(theme_path: Path) -> ThemeManifestItem | None:
    """从单个主题 JSON 提取清单所需的元数据。

    只读 id 和 name 两个字段（不返回完整内容）。解析失败返回 None，由调用方跳过。

    Args:
        theme_path: 主题 JSON 文件路径。

    Returns:
        清单元数据；JSON 无效或缺少必填字段时返回 None。
    """
    try:
        data: dict[str, Any] = json.loads(theme_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[routes_themes] 跳过无效主题文件 %s: %s", theme_path.name, exc)
        return None

    theme_id = data.get("id") or theme_path.stem
    name = data.get("name") or theme_id
    # public/themes/xxx.json → 站点根 /themes/xxx.json（Vite 约定）
    url = f"/themes/{theme_path.name}"

    return ThemeManifestItem(id=str(theme_id), name=str(name), url=url)


@router.get("/manifest", summary="获取动态主题清单")
def get_theme_manifest() -> list[ThemeManifestItem]:
    """返回 public/themes/ 下所有主题的元数据清单。

    扫描 frontend/public/themes/*.json，每个文件提取 id/name/url。
    目录不存在或为空时返回空列表（前端据此降级到内置 preset）。

    Returns:
        主题元数据列表，按文件名排序。
    """
    if not _THEMES_DIR.exists() or not _THEMES_DIR.is_dir():
        logger.debug("[routes_themes] 主题目录不存在: %s", _THEMES_DIR)
        return []

    items: list[ThemeManifestItem] = []
    # 非递归 glob：主题不嵌套，单层结构
    for theme_file in sorted(_THEMES_DIR.glob("*.json")):
        meta = _extract_meta(theme_file)
        if meta is not None:
            items.append(meta)

    return items
