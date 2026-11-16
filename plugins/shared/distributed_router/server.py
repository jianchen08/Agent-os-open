#!/usr/bin/env python3
"""distributed_router —— 2c 执行通道内部面（零内核改动，R24 撤工具面）。

现状（ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer）：三端统一
编址 = **设备前缀** ``/<device_id>/<相对路径>``（D16 规范记法），**路径统一
的承载 = OS 文件系统挂载**（D14：每端把他端领地挂进本端命名空间，运行时零
路径翻译服务）——文件类操作不经本插件。本插件保留：tree_core 作 D16 统一
编址规范记法与导出声明的对照单源（无运行时消费面）；channel_ops + mount 供
**非文件类能力工具**的执行通道挂载（D19 边界：手机摄像头/Shell、桌面 GPU 等
能力不对称场景，签发为显式配置/管理动作）。

**LLM 工具面零新增**：原 resolve_path/list_territories/mount_device_channel/
unmount_device_channel 四工具已撤除（R24）；地盘观测归前端呈现层（「我的
地盘」页）。本进程保留 settings 装配（on_load）与 relay 连通性诚实告警；
模块内部面：tree_core（编址记法纯函数）、channel_ops（relay 读面 + 通道
建立链）、mount（manifest 原语）。

配置源见 channel_ops.load_settings（口令恒现读，settings 不承载 token——P6）。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（共享裸模块）入 sys.path

from channel_ops import RouterSettings, load_settings  # noqa: E402

plugin = AgentOSPlugin("distributed_router")
logger = logging.getLogger(__name__)

_settings = RouterSettings()


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    global _settings
    settings = plugin.get_config().get("settings", {}).get("router", {})
    _settings = load_settings(settings)
    if not _settings.relay_url or not _settings.token:
        logger.warning(
            "[distributed_router] relay_url/token 未配齐（env %s/%s）；非文件类能力"
            "工具的执行通道签发面不可用（D19），路径统一承载=OS 挂载不受此影响，"
            "本端属地干活不受影响（离线第一）",
            "AGENTOS_SYNC_RELAY_URL", "AGENTOS_SYNC_TOKEN",
        )
    logger.info(
        "distributed_router 已装载: device=%s relay=%s",
        _settings.self_device_id, _settings.relay_url or "(未配置)",
    )


if __name__ == "__main__":
    plugin.run()
