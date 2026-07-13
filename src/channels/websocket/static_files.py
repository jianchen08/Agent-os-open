"""静态文件服务挂载模块。

负责将媒体输出目录（images/tts/video/music 等）挂载为 FastAPI 静态文件服务。

从 start_server.py 拆分而来，保持向后兼容。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def mount_media_static_files(app: FastAPI) -> None:
    """将媒体输出目录挂载为 FastAPI 静态文件服务。

    挂载 output/ 下的 images、tts、video、music 等子目录到 /media/* 路径。
    同时挂载用户上传文件目录到 /uploads/* 路径。
    必须在所有路由注册之后调用，以避免路由冲突。

    Args:
        app: FastAPI 应用实例
    """
    try:
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

        output_dir = Path(os.environ.get("MEDIA_OUTPUT_DIR", "./output"))
        if output_dir.exists():
            media_dirs = {
                "images": output_dir / "images",
                "tts": output_dir / "tts",
                "video": output_dir / "video",
                "music": output_dir / "music",
                "test_images": output_dir / "test_images",
                "test_tts": output_dir / "test_tts",
                "test_video": output_dir / "test_video",
                "test_music": output_dir / "test_music",
            }
            for name, path in media_dirs.items():
                if path.exists():
                    path.mkdir(parents=True, exist_ok=True)
                    app.mount(
                        f"/media/{name}",
                        StaticFiles(directory=str(path)),
                        name=f"media_{name}",
                    )
            logger.info(
                "[STARTUP] Media static files mounted at /media/* (dirs: %s)",
                [n for n, p in media_dirs.items() if p.exists()],
            )

        # 挂载用户上传文件目录（多模态文件上传）
        uploads_dir = Path(os.environ.get("UPLOADS_DIR", "./data/uploads"))
        uploads_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/uploads",
            StaticFiles(directory=str(uploads_dir)),
            name="uploads",
        )
        logger.info("[STARTUP] Uploads static files mounted at /uploads/*")

    except Exception as exc:
        logger.warning("[STARTUP] Media static files mount failed: %s", exc)
