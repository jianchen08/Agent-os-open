"""
清空数据库中的执行记录和任务

清空以下表：
- execution_records: 执行记录
- tasks: 任务
- sessions: 会话（可选）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import text

from src.db.connection import get_db_manager


async def clear_execution_data(clear_sessions: bool = False, keep_users: bool = True):
    """
    清空执行记录和任务数据

    Args:
        clear_sessions: 是否清空会话数据
        keep_users: 是否保留用户数据
    """
    db_manager = get_db_manager()

    async with db_manager.get_session() as session:
        print("开始清空数据...")

        result = await session.execute(text("DELETE FROM execution_records"))
        print(f"已清空 execution_records 表，删除 {result.rowcount} 条记录")

        result = await session.execute(text("DELETE FROM tasks"))
        print(f"已清空 tasks 表，删除 {result.rowcount} 条记录")

        if clear_sessions:
            result = await session.execute(text("DELETE FROM sessions"))
            print(f"已清空 sessions 表，删除 {result.rowcount} 条记录")

        if not keep_users:
            result = await session.execute(text("DELETE FROM users"))
            print(f"已清空 users 表，删除 {result.rowcount} 条记录")

        await session.commit()

    print("数据清空完成！")


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="清空数据库中的执行记录和任务")
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="同时清空会话数据",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="同时清空用户数据（危险操作）",
    )

    args = parser.parse_args()

    if args.all_users:
        confirm = input("⚠️  警告：将删除所有用户数据，此操作不可恢复！确认继续？(yes/no): ")
        if confirm.lower() != "yes":
            print("操作已取消")
            return

    await clear_execution_data(
        clear_sessions=args.sessions,
        keep_users=not args.all_users,
    )


if __name__ == "__main__":
    asyncio.run(main())
