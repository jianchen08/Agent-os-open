"""
数据库迁移管理器

提供数据库迁移的管理、版本跟踪和应用功能
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# [DEPRECATED] SQLAlchemy ORM 已移除，以下为兼容存根
# 该模块未来应迁移到纯 aiosqlite / asyncpg 直接执行，不再依赖 SQLAlchemy。


def text(sql: str):
    """SQLAlchemy text() 存根 —— 仅保留包装语义，返回原始 SQL 字符串。

    真正的参数绑定功能已不可用（:param 占位符仍可出现在 SQL 中，
    但不会做字典替换）。如需参数化查询，请使用各数据库驱动的原生参数机制。
    """
    return sql


# AsyncSession 类型存根 —— 标记待迁移，当前用法通过 get_db_manager().get_session() 获取
# 实际会话对象。此类型别名仅用于类型注解，运行时不依赖 SQLAlchemy。
AsyncSession = object  # type: ignore[misc,assignment]


class Migration:
    """迁移定义"""

    def __init__(
        self,
        version: str,
        name: str,
        up_sql: str,
        down_sql: str | None = None,
        description: str | None = None,
    ):
        self.version = version
        self.name = name
        self.up_sql = up_sql
        self.down_sql = down_sql
        self.description = description or ""

    @property
    def full_name(self) -> str:
        """完整迁移名称"""
        return f"{self.version}_{self.name}"


class MigrationManager:
    """数据库迁移管理器"""

    def __init__(self, migration_dir: Path | None = None):
        """
        初始化迁移管理器

        Args:
            migration_dir: 迁移脚本目录，默认为 src/db/migrations/scripts
        """
        self.migration_dir = migration_dir or Path(__file__).parent / "scripts"
        self.migrations: dict[str, Migration] = {}
        self._ensure_migration_dir()

    def _ensure_migration_dir(self) -> None:
        """确保迁移目录存在"""
        self.migration_dir.mkdir(parents=True, exist_ok=True)

    async def _ensure_migration_table(self, session: AsyncSession) -> None:
        """确保迁移记录表存在"""
        # 检测数据库类型
        from src.db.connection import get_db_manager

        db_manager = get_db_manager()
        is_sqlite = "sqlite" in db_manager.database_url.lower()

        if is_sqlite:
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        else:
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        await session.execute(text(create_table_sql))
        await session.commit()

    async def _get_applied_migrations(self, session: AsyncSession) -> set:
        """获取已应用的迁移版本"""
        result = await session.execute(text("SELECT version FROM schema_migrations"))
        return {row[0] for row in result}

    def _load_migration_files(self) -> None:
        """加载迁移文件"""
        self.migrations.clear()

        if not self.migration_dir.exists():
            return

        for migration_file in sorted(self.migration_dir.glob("*.yaml")):
            try:
                with open(migration_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                migration = Migration(
                    version=data["version"],
                    name=data["name"],
                    up_sql=data["up"],
                    down_sql=data.get("down"),
                    description=data.get("description", ""),
                )
                self.migrations[migration.version] = migration
            except Exception as e:
                print(f"加载迁移文件失败 {migration_file}: {e}")

    async def get_status(self) -> dict[str, Any]:
        """
        获取迁移状态

        Returns:
            包含迁移状态的字典
        """
        self._load_migration_files()

        from src.db.connection import get_db_manager

        async with get_db_manager().get_session() as session:
            await self._ensure_migration_table(session)
            applied_versions = await self._get_applied_migrations(session)

            all_versions = sorted(self.migrations.keys())
            pending = [v for v in all_versions if v not in applied_versions]
            applied = [v for v in all_versions if v in applied_versions]

            return {
                "pending": pending,
                "applied": applied,
                "total": len(all_versions),
                "applied_count": len(applied),
                "pending_count": len(pending),
            }

    async def migrate(
        self, target_version: str | None = None, session: AsyncSession | None = None
    ) -> list[str]:
        """
        执行迁移

        Args:
            target_version: 目标版本，None 表示迁移到最新版本
            session: 数据库会话，None 表示创建新会话

        Returns:
            应用的迁移版本列表
        """
        self._load_migration_files()

        from src.db.connection import get_db_manager

        if session is None:
            db_manager = get_db_manager()
            async with db_manager.get_session() as session:
                return await self._execute_migrations(session, target_version)
        else:
            return await self._execute_migrations(session, target_version)

    async def _execute_migrations(
        self, session: AsyncSession, target_version: str | None = None
    ) -> list[str]:
        """执行迁移的内部方法"""
        await self._ensure_migration_table(session)
        applied_versions = await self._get_applied_migrations(session)

        # 确定要应用的迁移
        if target_version:
            if target_version not in self.migrations:
                raise ValueError(f"未找到迁移版本: {target_version}")
            versions_to_apply = [
                v
                for v in sorted(self.migrations.keys())
                if v <= target_version and v not in applied_versions
            ]
        else:
            versions_to_apply = [
                v for v in sorted(self.migrations.keys()) if v not in applied_versions
            ]

        applied = []
        for version in versions_to_apply:
            migration = self.migrations[version]
            print(f"应用迁移: {migration.full_name}")

            # 执行 up SQL
            for statement in migration.up_sql.split(";"):
                statement = statement.strip()
                if statement:
                    await session.execute(text(statement))

            # 记录迁移
            await session.execute(
                text(
                    "INSERT INTO schema_migrations (version, name) VALUES (:version, :name)"
                ),
                {"version": version, "name": migration.name},
            )

            await session.commit()
            applied.append(version)
            print(f"[OK] 迁移 {version} 应用成功")

        return applied

    async def rollback(
        self, target_version: str | None = None, steps: int = 1
    ) -> list[str]:
        """
        回滚迁移

        Args:
            target_version: 目标版本，None 表示回滚指定步数
            steps: 回滚步数，当 target_version 为 None 时使用

        Returns:
            回滚的迁移版本列表
        """
        self._load_migration_files()

        from src.db.connection import get_db_manager

        async with get_db_manager().get_session() as session:
            await self._ensure_migration_table(session)
            applied_versions = sorted(
                (await self._get_applied_migrations(session)), reverse=True
            )

            if not applied_versions:
                print("没有已应用的迁移可以回滚")
                return []

            # 确定要回滚的版本
            if target_version:
                # 如果目标版本不在已应用版本中，说明要回滚所有迁移
                if target_version not in applied_versions:
                    # 回滚所有版本大于目标版本的迁移
                    versions_to_rollback = [
                        v for v in applied_versions if v > target_version
                    ]
                else:
                    # 回滚到目标版本之前（包括目标版本）
                    versions_to_rollback = [
                        v
                        for v in applied_versions
                        if v > target_version or v == target_version
                    ]
            else:
                versions_to_rollback = applied_versions[:steps]

            rolled_back = []
            for version in versions_to_rollback:
                migration = self.migrations.get(version)
                if not migration or not migration.down_sql:
                    print(f"[WARN] 跳过无法回滚的迁移: {version}")
                    continue

                print(f"回滚迁移: {migration.full_name}")

                # 执行 down SQL
                for statement in migration.down_sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        await session.execute(text(statement))

                # 删除迁移记录
                await session.execute(
                    text("DELETE FROM schema_migrations WHERE version = :version"),
                    {"version": version},
                )

                await session.commit()
                rolled_back.append(version)
                print(f"[OK] 迁移 {version} 回滚成功")

            return rolled_back

    async def create_migration(self, name: str, description: str | None = None) -> Path:
        """
        创建新的迁移文件

        Args:
            name: 迁移名称
            description: 迁移描述

        Returns:
            迁移文件路径
        """
        # 生成版本号：YYYYMMDDHHMMSS
        version = datetime.now().strftime("%Y%m%d%H%M%S")

        migration_data = {
            "version": version,
            "name": name,
            "description": description or "",
            "up": "-- UP SQL 在这里编写\n",
            "down": "-- DOWN SQL 在这里编写（可选）\n",
        }

        migration_file = self.migration_dir / f"{version}_{name}.yaml"

        with open(migration_file, "w", encoding="utf-8") as f:
            yaml.dump(migration_data, f, allow_unicode=True, sort_keys=False)

        print(f"[OK] 迁移文件已创建: {migration_file}")
        return migration_file


# 全局迁移管理器实例
migration_manager = MigrationManager()
