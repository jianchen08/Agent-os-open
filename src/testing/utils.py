"""
测试工具类

提供测试中常用的工具类和函数
"""

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import create_engine, text

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


class MockEmbeddingService:
    """模拟嵌入服务 - 用于测试"""

    def __init__(self, client=None):
        self.client = client
        self._embeddings_cache: dict[str, list[float]] = {}

    async def get_embedding(self, text: str) -> list[float]:
        """
        获取文本嵌入向量

        Args:
            text: 输入文本

        Returns:
            嵌入向量
        """
        # 使用缓存避免重复计算
        if text in self._embeddings_cache:
            return self._embeddings_cache[text]

        # 生成模拟嵌入向量（基于文本哈希）
        import hashlib

        hash_obj = hashlib.md5(text.encode())
        hash_bytes = hash_obj.digest()

        # 将哈希转换为384维向量（模拟真实嵌入维度）
        embedding = []
        for i in range(0, len(hash_bytes), 2):
            if i + 1 < len(hash_bytes):
                val = (hash_bytes[i] << 8) | hash_bytes[i + 1]
                embedding.append((val / 65535.0) * 2 - 1)  # 归一化到[-1, 1]

        # 扩展到384维
        while len(embedding) < 384:
            embedding.extend(embedding[: min(len(embedding), 384 - len(embedding))])

        embedding = embedding[:384]
        self._embeddings_cache[text] = embedding
        return embedding

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        批量获取文本嵌入向量

        Args:
            texts: 输入文本列表

        Returns:
            嵌入向量列表
        """
        return [await self.get_embedding(text) for text in texts]


class MockDatabaseSession:
    """模拟数据库会话 - 用于测试"""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._committed = False
        self._rolled_back = False

    async def add(self, obj):
        """添加对象"""

    async def commit(self):
        """提交事务"""
        self._committed = True

    async def rollback(self):
        """回滚事务"""
        self._rolled_back = True

    async def close(self):
        """关闭会话"""

    async def flush(self):
        """刷新会话"""

    async def refresh(self, obj):
        """刷新对象"""

    @property
    def is_committed(self) -> bool:
        """是否已提交"""
        return self._committed

    @property
    def is_rolled_back(self) -> bool:
        """是否已回滚"""
        return self._rolled_back


def create_mock_user_data(user_id: str = "test_user") -> dict[str, Any]:
    """
    创建模拟用户数据

    Args:
        user_id: 用户ID

    Returns:
        用户数据字典
    """
    return {
        "id": user_id,
        "username": f"user_{user_id}",
        "email": f"{user_id}@example.com",
        "role": "user",
        "is_active": True,
        "created_at": "2024-01-01T00:00:00Z",
    }


def create_mock_notification_data(
    notification_id: str = "test_notification",
    user_id: str = "test_user",
    title: str = "测试通知",
    message: str = "这是一个测试通知",
) -> dict[str, Any]:
    """
    创建模拟通知数据

    Args:
        notification_id: 通知ID
        user_id: 用户ID
        title: 通知标题
        message: 通知内容

    Returns:
        通知数据字典
    """
    return {
        "id": notification_id,
        "user_id": user_id,
        "title": title,
        "message": message,
        "type": "info",
        "priority": "normal",
        "read": False,
        "pushed": False,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def create_mock_task_data(
    task_id: str = "test_task",
    status: str = "pending",
    task_type: str = "test",
) -> dict[str, Any]:
    """
    创建模拟任务数据

    Args:
        task_id: 任务ID
        status: 任务状态
        task_type: 任务类型

    Returns:
        任务数据字典
    """
    return {
        "id": task_id,
        "status": status,
        "type": task_type,
        "created_at": "2024-01-01T00:00:00Z",
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
    }


class TestEnvironmentManager:
    """测试环境管理器"""

    def __init__(self):
        self.backend_process = None
        self.frontend_process = None
        self.test_db_url = None
        self.original_env = {}

    async def setup_test_database(self):
        """设置测试数据库"""
        try:
            settings = get_settings()

            # 创建测试数据库URL
            if hasattr(settings, "database_url"):
                base_url = settings.database_url.rsplit("/", 1)[0]
                self.test_db_url = f"{base_url}/test_agent_db"
            else:
                self.test_db_url = "postgresql://localhost/test_agent_db"

            # 设置环境变量
            self.original_env["DATABASE_URL"] = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = self.test_db_url

            # 创建测试数据库（如果不存在）
            # 使用 SQLAlchemy 的 create_all() 方法创建表
            from src.db.models import Base

            engine = create_engine(self.test_db_url)
            Base.metadata.create_all(engine)

            logger.info(f"✅ 测试数据库设置完成: {self.test_db_url}")

        except Exception as e:
            logger.error(f"❌ 测试数据库设置失败: {e}")
            raise

    async def start_backend_service(self):
        """启动后端服务"""
        try:
            # 设置测试环境变量
            env = os.environ.copy()
            env.update(
                {
                    "ENVIRONMENT": "test",
                    "LOG_LEVEL": "INFO",
                    "DATABASE_URL": self.test_db_url,
                    "PYTHONIOENCODING": "utf-8",  # 修复Unicode编码问题
                    "PYTHONPATH": str(Path.cwd()),
                }
            )

            # 启动后端服务
            cmd = [
                "python",
                "-m",
                "uvicorn",
                "src.api.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8888",
            ]

            self.backend_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path.cwd(),
                encoding="utf-8",
                errors="replace",  # 处理编码错误
            )

            logger.info("🚀 后端服务启动中...")
            return self.backend_process

        except Exception as e:
            logger.error(f"❌ 后端服务启动失败: {e}")
            raise

    async def start_frontend_service(self):
        """启动前端服务"""
        try:
            frontend_dir = Path("frontend")
            if not frontend_dir.exists():
                logger.warning("⚠️ 前端目录不存在，跳过前端服务启动")
                return None

            # 检查是否有package.json
            package_json = frontend_dir / "package.json"
            if not package_json.exists():
                logger.warning("⚠️ package.json不存在，跳过前端服务启动")
                return None

            # 启动前端开发服务器
            cmd = ["npm", "run", "dev"]

            self.frontend_process = subprocess.Popen(
                cmd,
                cwd=frontend_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )

            logger.info("🎨 前端服务启动中...")
            return self.frontend_process

        except Exception as e:
            logger.warning(f"⚠️ 前端服务启动失败: {e}")
            return None

    async def wait_for_services_ready(self, timeout: int = 60):
        """等待服务就绪"""
        start_time = time.time()

        # 等待后端服务
        from src.core.constants import Timeout

        backend_ready = False
        while time.time() - start_time < timeout and not backend_ready:
            try:
                response = requests.get(
                    "http://localhost:8888/health", timeout=Timeout.HEALTH_CHECK
                )
                if response.status_code == 200:
                    backend_ready = True
                    logger.info("✅ 后端服务就绪")
                    break
            except requests.exceptions.RequestException:
                pass

            await asyncio.sleep(2)

        if not backend_ready:
            raise TimeoutError("后端服务启动超时")

        # 等待前端服务（如果存在）
        from src.core.constants import Timeout

        if self.frontend_process:
            frontend_ready = False
            while time.time() - start_time < timeout and not frontend_ready:
                try:
                    response = requests.get(
                        "http://localhost:3000", timeout=Timeout.SERVICE_STARTUP
                    )
                    if response.status_code in [200, 404]:  # 404也表示服务在运行
                        frontend_ready = True
                        logger.info("✅ 前端服务就绪")
                        break
                except requests.exceptions.RequestException:
                    pass

                await asyncio.sleep(2)

            if not frontend_ready:
                logger.warning("⚠️ 前端服务启动超时，继续测试")

    async def cleanup(self):
        """清理测试环境"""
        try:
            # 停止服务
            if self.backend_process:
                self.backend_process.terminate()
                try:
                    from src.core.constants import Timeout

                    self.backend_process.wait(timeout=Timeout.PROCESS_WAIT)
                except subprocess.TimeoutExpired:
                    self.backend_process.kill()
                logger.info("🛑 后端服务已停止")

            if self.frontend_process:
                self.frontend_process.terminate()
                try:
                    from src.core.constants import Timeout

                    self.frontend_process.wait(timeout=Timeout.PROCESS_WAIT)
                except subprocess.TimeoutExpired:
                    self.frontend_process.kill()
                logger.info("🛑 前端服务已停止")

            # 恢复环境变量
            for key, value in self.original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

            logger.info("🧹 测试环境清理完成")

        except Exception as e:
            logger.error(f"❌ 环境清理失败: {e}")


class TestDataManager:
    """测试数据管理器"""

    def __init__(self):
        self.created_users = []
        self.created_tasks = []
        self.test_counter = 0

    def create_test_user(self) -> dict[str, str]:
        """创建测试用户数据"""
        self.test_counter += 1
        user_data = {
            "username": f"test_user_{self.test_counter}_{int(time.time())}",
            "email": f"test_{self.test_counter}@example.com",
            "password": "test_password_123",
            "full_name": f"Test User {self.test_counter}",
        }
        self.created_users.append(user_data)
        return user_data

    def create_test_task(self) -> dict[str, Any]:
        """创建测试任务数据"""
        self.test_counter += 1
        task_data = {
            "title": f"测试任务 {self.test_counter}",
            "description": f"这是一个端到端测试任务，用于验证完整的数据流。任务ID: {self.test_counter}",
            "priority": "medium",
            "agent_id": "default-agent-id",  # 添加必需的 agent_id 字段
            "agent_config": "lingxi",
            "workflow_name": "task_execution",
            "input_data": {
                "test_mode": True,
                "task_number": self.test_counter,
                "expected_result": "success",
            },
            "tags": ["e2e_test", "integration_test"],
        }
        self.created_tasks.append(task_data)
        return task_data

    async def verify_task_in_database(
        self, task_id: str, user_id: str
    ) -> dict[str, Any]:
        """验证任务在数据库中的存在性和完整性"""
        try:
            settings = get_settings()
            engine = create_engine(
                os.environ.get("DATABASE_URL", settings.database_url)
            )

            with engine.connect() as conn:
                # 查询任务
                task_query = text(
                    """
                    SELECT id, title, description, status, created_at, updated_at, user_id
                    FROM tasks
                    WHERE id = :task_id
                """
                )
                task_result = conn.execute(task_query, {"task_id": task_id}).fetchone()

                # 查询执行记录
                execution_query = text(
                    """
                    SELECT id, task_id, status, started_at, completed_at, result
                    FROM task_executions
                    WHERE task_id = :task_id
                """
                )
                execution_result = conn.execute(
                    execution_query, {"task_id": task_id}
                ).fetchone()

                return {
                    "task_exists": task_result is not None,
                    "execution_logged": execution_result is not None,
                    "user_associated": (
                        task_result and str(task_result.user_id) == str(user_id)
                        if task_result
                        else False
                    ),
                    "task_data": dict(task_result._mapping) if task_result else None,
                    "execution_data": (
                        dict(execution_result._mapping) if execution_result else None
                    ),
                }

        except Exception as e:
            logger.error(f"数据库验证失败: {e}")
            return {
                "task_exists": False,
                "execution_logged": False,
                "user_associated": False,
                "error": str(e),
            }

    def cleanup_test_data(self):
        """清理测试数据"""
        try:
            # 这里可以添加清理数据库中测试数据的逻辑
            self.created_users.clear()
            self.created_tasks.clear()
            self.test_counter = 0
            logger.info("🧹 测试数据清理完成")
        except Exception as e:
            logger.error(f"❌ 测试数据清理失败: {e}")


class RealTimeTestMonitor:
    """实时测试监控器"""

    def __init__(self):
        self.test_events = []
        self.start_time = time.time()

    def log_event(self, event_type: str, message: str, data: dict | None = None):
        """记录测试事件"""
        event = {
            "timestamp": time.time(),
            "elapsed": time.time() - self.start_time,
            "type": event_type,
            "message": message,
            "data": data or {},
        }
        self.test_events.append(event)

        # 实时输出
        elapsed_str = f"{event['elapsed']:.2f}s"
        print(f"[{elapsed_str}] {event_type}: {message}")

    def get_test_timeline(self) -> list[dict]:
        """获取测试时间线"""
        return self.test_events.copy()

    def generate_timeline_report(self) -> str:
        """生成时间线报告"""
        lines = ["🕒 测试执行时间线", "=" * 50]

        for event in self.test_events:
            elapsed_str = f"{event['elapsed']:.2f}s"
            lines.append(f"[{elapsed_str}] {event['type']}: {event['message']}")

        return "\n".join(lines)
