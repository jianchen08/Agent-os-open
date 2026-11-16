"""多模态链路正则与图片扩展名词汇单源（multimodal_pre/postprocessor 共用）。

扩展名词汇与正则此前在两插件各持复制（``_IMAGE_URL_PATTERN`` 字节级同文
×2、词汇 ``jpg|jpeg|png|gif|webp|svg`` ×4）：改词汇漏改任一侧 = 检出/
回收判定静默分叉（前端可见但后端不检出，或反之）。本模块是唯一词汇与
模板源，新增扩展名只改 :data:`IMAGE_EXTS` 一处，两插件同变。

消费方（经 plugins/shared 根裸名导入，先例同 state_fields/mode_keys）：
- multimodal_preprocessor：md 图片引用 / http(s) URL / 本地路径三检出分支；
- multimodal_postprocessor：LLM 输出图片 URL 回收（与 pre 的 URL 检出
  共用同一 :data:`IMAGE_URL_PATTERN`——「能检出即可回收」同一判定）。
"""

from __future__ import annotations

import re

# 图片扩展名词汇（唯一真源）：交替串形态，供三正则模板内插。
IMAGE_EXTS = "jpg|jpeg|png|gif|webp|svg"


def build_md_image_pattern(exts: str) -> re.Pattern[str]:
    """markdown 图片引用整 token（``![f](/uploads/x.png)``）——只认 /uploads/
    前缀（平台管理的引用），不劫持用户手打的任意 markdown。"""
    return re.compile(
        rf"!\[[^\]]*\]\((/uploads/[^\s)]+\.(?:{exts}))\)",
        re.IGNORECASE,
    )


def build_image_url_pattern(exts: str) -> re.Pattern[str]:
    """http(s) 图片 URL（pre 检出 / post 回收共用同一模板）。"""
    return re.compile(
        rf"(https?://\S+\.(?:{exts})(?:\?\S*)?)",
        re.IGNORECASE,
    )


def build_local_file_pattern(exts: str) -> re.Pattern[str]:
    """本地图片/PDF 路径兜底（词汇另拼 ``|pdf``，pdf 归本地文档分支）。"""
    return re.compile(
        rf"((?:[A-Za-z]:)?[/\\][\S]+\.(?:{exts}|pdf))",
        re.IGNORECASE,
    )


MD_IMAGE_PATTERN = build_md_image_pattern(IMAGE_EXTS)
IMAGE_URL_PATTERN = build_image_url_pattern(IMAGE_EXTS)
LOCAL_FILE_PATTERN = build_local_file_pattern(IMAGE_EXTS)
