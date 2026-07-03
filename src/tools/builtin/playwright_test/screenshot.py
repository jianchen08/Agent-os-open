"""
截图对比功能

提供页面截图和像素级对比功能。
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class ScreenshotManager:
    """
    截图对比管理器

    支持全页面截图、元素截图和像素级对比。
    """

    @staticmethod
    async def capture_full_page(
        page: Any,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """
        截取整页截图

        Args:
            page: Playwright 页面对象
            output_path: 保存路径

        Returns:
            截图结果
        """
        try:
            if output_path is None:
                import tempfile  # noqa: PLC0415
                import uuid  # noqa: PLC0415

                output_path = os.path.join(tempfile.gettempdir(), f"playwright_screenshot_{uuid.uuid4().hex[:8]}.png")

            # 确保目录存在
            os.makedirs(os.path.dirname(output_path), exist_ok=True)  # noqa: PTH103,PTH120

            # 截图
            await page.screenshot(path=output_path, full_page=True)

            # 编码为 base64 用于多模态传输
            with open(output_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")

            return {
                "success": True,
                "path": output_path,
                "base64_data": b64_data,
                "mime_type": "image/png",
                "message": "全页面截图已保存",
            }
        except Exception as e:
            logger.error(f"截图失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def capture_element(
        page: Any,
        selector: str,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """
        截取元素截图

        Args:
            page: Playwright 页面对象
            selector: 元素选择器
            output_path: 保存路径

        Returns:
            截图结果
        """
        try:
            # 定位元素
            element = page.locator(selector).first
            await element.wait_for(timeout=5000)

            if output_path is None:
                import tempfile  # noqa: PLC0415
                import uuid  # noqa: PLC0415

                output_path = os.path.join(tempfile.gettempdir(), f"playwright_element_{uuid.uuid4().hex[:8]}.png")

            # 确保目录存在
            os.makedirs(os.path.dirname(output_path), exist_ok=True)  # noqa: PTH103,PTH120

            # 元素截图
            await element.screenshot(path=output_path)

            # 编码为 base64 用于多模态传输
            with open(output_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")

            return {
                "success": True,
                "path": output_path,
                "base64_data": b64_data,
                "mime_type": "image/png",
                "message": "元素截图已保存",
            }
        except Exception as e:
            logger.error(f"元素截图失败: {e}")
            return {
                "success": False,
                "error": f"元素截图失败: {str(e)}",
            }

    @staticmethod
    def compare_images(
        baseline_path: str,
        current_path: str,
        threshold: float = 0.1,
    ) -> dict[str, Any]:
        """
        对比两张图片的像素差异

        Args:
            baseline_path: 基准图片路径
            current_path: 当前图片路径
            threshold: 差异阈值 (0.0-1.0)

        Returns:
            对比结果
        """
        try:
            import numpy as np  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415

            # 检查文件是否存在
            if not os.path.exists(baseline_path):  # noqa: PTH110
                return {
                    "success": False,
                    "error": f"基准图片不存在: {baseline_path}",
                }
            if not os.path.exists(current_path):  # noqa: PTH110
                return {
                    "success": False,
                    "error": f"当前图片不存在: {current_path}",
                }

            # 打开图片
            baseline_img = Image.open(baseline_path)
            current_img = Image.open(current_path)

            # 转换为 RGB
            if baseline_img.mode != "RGB":
                baseline_img = baseline_img.convert("RGB")
            if current_img.mode != "RGB":
                current_img = current_img.convert("RGB")

            # 调整为相同大小
            if baseline_img.size != current_img.size:
                current_img = current_img.resize(baseline_img.size, Image.LANCZOS)

            # 转换为 numpy 数组
            baseline_array = np.array(baseline_img)
            current_array = np.array(current_img)

            # 计算差异
            diff = np.abs(baseline_array.astype(float) - current_array.astype(float))
            total_pixels = baseline_array.shape[0] * baseline_array.shape[1]

            # 统计不同像素数（阈值设为 10 避免颜色抖动误判）
            diff_threshold = 10
            diff_pixels = np.sum(np.any(diff > diff_threshold, axis=2))
            diff_percentage = diff_pixels / total_pixels

            # 判断是否通过
            passed = diff_percentage <= threshold

            return {
                "success": True,
                "passed": passed,
                "diff_percentage": round(diff_percentage * 100, 2),
                "baseline_path": baseline_path,
                "current_path": current_path,
                "threshold": threshold,
                "message": "截图对比完成",
            }
        except ImportError as e:
            return {
                "success": False,
                "error": f"Pillow 或 numpy 未安装: {e}",
            }
        except Exception as e:
            logger.error(f"图片对比失败: {e}")
            return {
                "success": False,
                "error": f"图片对比失败: {str(e)}",
            }

    @staticmethod
    def save_baseline(
        image_path: str,
        baseline_path: str,
    ) -> dict[str, Any]:
        """
        保存基准图片

        Args:
            image_path: 当前图片路径
            baseline_path: 基准图片保存路径

        Returns:
            保存结果
        """
        try:
            import shutil  # noqa: PLC0415

            # 确保目录存在
            os.makedirs(os.path.dirname(baseline_path), exist_ok=True)  # noqa: PTH103,PTH120

            # 复制图片
            shutil.copy2(image_path, baseline_path)

            return {
                "success": True,
                "baseline_path": baseline_path,
                "message": "基准图片已保存",
            }
        except Exception as e:
            logger.error(f"保存基准图片失败: {e}")
            return {
                "success": False,
                "error": f"保存基准图片失败: {str(e)}",
            }
