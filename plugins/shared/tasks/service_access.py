"""TaskService 获取的统一入口。

插件自包含实例化 TaskService（mixin _task_cleanup/_task_crud/_task_state 已
在本插件包内）；event_bus 是装饰性（赋值后从不读取），event_bus=None 安全。

实例来源两级：`set_task_service` 注入的**正门实例**（tasks/server.py on_load
以插件配置构造后注入，同进程 co-host 消费方与其共享同一单例）优先；无注入
时（消费方独立 sidecar，正门不在本进程）按租户懒自建——租户键取「SDK 身份
contextvar > default」（刀1b 直调面透传），各租户实例的存储数据根落在
data/{tenant}/tasks；与正门 data_dir 未配置时 TaskStorage 同链路。

公共接口：
- get_task_service() -> Any: 获取 TaskService 实例（注入优先，其次按租户懒加载单例）
- set_task_service(instance) -> None: 注入/清除（None）正门实例（tasks 正门接线用）
- reset_singletons() -> None: 清空单例（测试隔离用）

项目登记簿读面自 T6a 起经 project_manager 的 project.* 服务消费（登记簿
实现吸收进该插件，本模块不再持有登记单例）。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from agentos_plugin_sdk.capability import current_reverse_call_tenant_id

# 解析前提自举（本模块是任务域跨进程共享入口，消费进程的 sys.path 装配不可
# 假设，三级缺一即解析失败）：
# - 共享层（plugins/shared/）：project_registry 所在，与 storage.py 同模式；
# - 组根（system/）：`tasks` 命名空间包（tasks.service）解析前提——组根不随
#   bootstrap 注入（ADR 2026-09-08-plugin-bootstrap-sink 决策 1），需用方
#   自持（workspace/server.py 同款）；
# - 本插件目录：service.py 及其平铺闭包（_task_* / storage / task_types /
#   state_machine / agents_types）裸名互导的解析前提。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_GROUP_ROOT = os.path.dirname(_PLUGIN_DIR)
for _boot_dir in (_SHARED_ROOT, _GROUP_ROOT, _PLUGIN_DIR):
    if _boot_dir not in sys.path:
        sys.path.insert(0, _boot_dir)

logger = logging.getLogger(__name__)

__all__ = ["get_task_service", "set_task_service", "reset_singletons"]

# 进程内单例（懒加载缓存），按租户分化（刀1b 直调面透传）：合宿 light 组
# 多租户共进程，单槽实例会把后续租户的写入钉在首建租户的数据根——键取
# 「身份 contextvar > DEFAULT_TENANT」，与 TaskStorage 取租户序同源。
_task_service_instances: dict[str, Any] = {}
# 正门注入实例（tasks/server.py on_load 接线；优先于懒加载单例）。
_injected_task_service: Any = None


def set_task_service(instance: Any | None) -> None:
    """注入正门 TaskService 实例；传 None 清除注入（回到懒构建通道）。"""
    global _injected_task_service  # noqa: PLW0603
    _injected_task_service = instance


def _lazy_tenant_key() -> str:
    """懒构建通道的租户键：身份 contextvar > DEFAULT_TENANT（与存储层同源）。"""
    return current_reverse_call_tenant_id() or "default"


def get_task_service() -> Any:
    """获取 TaskService 实例（正门注入优先，其次按租户懒加载单例）。

    插件自包含：直接实例化 tasks.service.TaskService（mixin 已在本包）。
    event_bus=None（装饰性，从不读取）。懒构建实例以当前租户为键缓存并
    显式下发 tenant_id（存储数据根落 ``data/{tenant}/tasks``）。

    Returns:
        TaskService 实例，初始化失败时返回 None
    """
    if _injected_task_service is not None:
        return _injected_task_service
    tenant_key = _lazy_tenant_key()
    cached = _task_service_instances.get(tenant_key)
    if cached is not None:
        return cached
    try:
        # 限定导入（M3 防误解析契约，tools/task·task_submit server.py 注释同源
        # 声明）：合宿静息态下裸名 `service` 槽位归首占成员的同名模块（如
        # security_check/service.py，无 TaskService），平铺导入命中的是槽位
        # 驻留者而非本插件实现；`tasks.service` 经命名空间包解析到本插件
        # 目录，与裸名槽位解耦。解析前提由模块头三级自举保证，不依赖同宿
        # 插件的 extra 装配副作用。
        # 按租户缓存（cache 命中后不再触碰 import，重复调用零开销）。
        from tasks.service import TaskService  # noqa: PLC0415

        instance = TaskService(tenant_id=tenant_key)
        _task_service_instances[tenant_key] = instance
        return instance
    except Exception as exc:
        logger.warning(
            "get_task_service: TaskService 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None


def reset_singletons() -> None:
    """清空进程内单例（测试隔离用）。"""
    global _task_service_instances, _injected_task_service  # noqa: PLW0603
    _task_service_instances = {}
    _injected_task_service = None
