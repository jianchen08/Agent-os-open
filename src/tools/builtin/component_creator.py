"""
组件创建工具

将生成的组件（工具/Agent/工作流）保存到文件并同步到数据库。
"""

from pathlib import Path
from typing import Any, Literal

import yaml


async def component_creator(
    component_type: Literal["tool", "agent", "workflow"],
    name: str,
    content: str | dict[str, Any],
    category: str | None = None,
    sync_db: bool = True,
) -> dict[str, Any]:
    """
    创建组件并保存到相应位置

    Args:
        component_type: 组件类型 - tool/agent/workflow
        name: 组件名称（用于文件名）
        content: 组件内容（代码字符串或配置字典）
        category: 分类名称（可选，用于组织目录）
        sync_db: 是否同步到数据库

    Returns:
        创建结果，包含文件路径和同步状态
    """
    result = {
        "success": False,
        "file_path": None,
        "db_synced": False,
        "message": "",
    }

    try:
        # 确定保存路径
        if component_type == "tool":
            # 工具保存为 Python 文件
            if category:
                file_path = Path(f"src/tools/{category}/{name}.py")
            else:
                file_path = Path(f"src/tools/builtin/{name}.py")

            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # 写入代码
            if isinstance(content, str):
                file_path.write_text(content, encoding="utf-8")
            else:
                result["message"] = "工具内容必须是代码字符串"
                return result

        elif component_type == "agent":
            # Agent 保存为 YAML 文件
            if category:
                file_path = Path(f"config/agents/{category}/{name}.yaml")
            else:
                file_path = Path(f"config/agents/{name}.yaml")

            file_path.parent.mkdir(parents=True, exist_ok=True)

            # 写入配置
            if isinstance(content, dict):
                yaml_content = yaml.dump(
                    content,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
                file_path.write_text(yaml_content, encoding="utf-8")
            elif isinstance(content, str):
                file_path.write_text(content, encoding="utf-8")
            else:
                result["message"] = "Agent 内容必须是配置字典或 YAML 字符串"
                return result

        elif component_type == "workflow":
            # 工作流保存为 YAML 文件
            if category:
                file_path = Path(f"config/workflows/{category}/{name}.yaml")
            else:
                file_path = Path(f"config/workflows/{name}.yaml")

            file_path.parent.mkdir(parents=True, exist_ok=True)

            # 写入配置
            if isinstance(content, dict):
                yaml_content = yaml.dump(
                    content,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
                file_path.write_text(yaml_content, encoding="utf-8")
            elif isinstance(content, str):
                file_path.write_text(content, encoding="utf-8")
            else:
                result["message"] = "工作流内容必须是配置字典或 YAML 字符串"
                return result
        else:
            result["message"] = f"不支持的组件类型: {component_type}"
            return result

        result["file_path"] = str(file_path)
        result["success"] = True
        result["message"] = f"文件已保存: {file_path}"

        # 同步到数据库
        if sync_db and component_type in ("agent", "workflow"):
            try:
                db_result = await _sync_to_database(component_type, file_path)
                result["db_synced"] = db_result
                if db_result:
                    result["message"] += "，已同步到数据库"
                else:
                    result["message"] += "，数据库同步失败"
            except Exception as e:
                result["message"] += f"，数据库同步异常: {e}"

        return result

    except Exception as e:
        result["message"] = f"创建组件失败: {e}"
        return result


async def _sync_to_database(component_type: str, file_path: Path) -> bool:
    """
    同步配置到数据库

    Args:
        component_type: 组件类型
        file_path: 配置文件路径

    Returns:
        是否同步成功
    """
    try:
        # 延迟导入避免循环依赖
        from src.config.loader import ConfigLoader
        from src.db.connection import get_db_manager

        async with get_db_manager().get_session() as session:
            loader = ConfigLoader()

            # 加载单个文件
            config = loader._load_yaml(file_path)
            if not config:
                return False

            if component_type == "agent":
                from sqlalchemy import select

                from src.db.models import AgentConfig

                config_id = config.get("config_id")
                if not config_id:
                    return False

                # 检查是否存在
                existing = await session.execute(
                    select(AgentConfig).where(AgentConfig.config_id == config_id)
                )
                agent = existing.scalar_one_or_none()

                if agent:
                    loader._update_agent(agent, config)
                else:
                    agent = loader._create_agent(config)
                    session.add(agent)

                await session.commit()
                return True

            elif component_type == "workflow":
                from sqlalchemy import select

                from src.db.models import Workflow

                workflow_id = config.get("id")
                if not workflow_id:
                    return False

                existing = await session.execute(
                    select(Workflow).where(Workflow.name == workflow_id)
                )
                workflow = existing.scalar_one_or_none()

                if workflow:
                    loader._update_workflow(workflow, config)
                else:
                    workflow = loader._create_workflow(config)
                    session.add(workflow)

                await session.commit()
                return True

        return False

    except Exception:
        return False
