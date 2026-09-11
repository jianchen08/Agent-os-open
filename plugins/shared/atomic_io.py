"""插件共享原子文件写助手 — 关键配置不截断（B4 单一实现）。

llm.yaml / .env / agent yaml 等关键配置承载 provider key 与执行管道定义，
write_text 直写存在崩溃窗口 = 配置截断丢失。本模块收口"同目录 tmp +
os.replace 原子替换"范式：

- tmp 与目标同目录（同卷，os.replace 为原子 rename，无跨卷拷贝窗口）；
- 失败清 tmp（不留 .tmp 残骸）；
- os.replace 短重试（Windows 目标被读句柄/杀软瞬时占住报 PermissionError，
  短退避吸收瞬态窗口，重试耗尽仍失败才上抛）。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上
sys.path 后裸名导入（如 ``from atomic_io import atomic_write_text as
_atomic_write_text``），保持各插件内部调用点零改动。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# os.replace 短重试：次数与退避（只吸收目标文件瞬态占用，不做长阻塞）
_REPLACE_ATTEMPTS = 3
_REPLACE_BACKOFF_SECONDS = 0.05


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """原子覆写文本文件：同目录 tmp 写入 → os.replace 原子换入。

    失败保证：目标文件保持旧内容；tmp 不残留。tmp 写失败立即清 tmp 上抛；
    replace 瞬态失败短重试，重试耗尽清 tmp 后上抛最后一次异常（错误是值、
    传播给调用方定语义，不吞）。

    Args:
        path: 目标文件路径（父目录需已存在，调用方负责 mkdir）
        text: 待写入文本
        encoding: 写入编码，默认 utf-8

    Raises:
        OSError: tmp 写失败，或 replace 重试耗尽仍失败（目标保持旧内容）
    """
    target = Path(path)
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(text)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, target)
                return
            except OSError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS)
    finally:
        # tmp 不残留：replace 成功后 tmp 已不存在，FileNotFoundError 即无物可清
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
