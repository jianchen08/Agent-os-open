"""
数据库迁移运行器

提供命令行接口来执行数据库迁移操作
"""

import argparse
import asyncio
import sys

from .manager import MigrationManager


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="数据库迁移工具")

    subparsers = parser.add_subparsers(dest="command", help="迁移命令")

    # status 命令
    subparsers.add_parser("status", help="显示迁移状态")

    # migrate 命令
    migrate_parser = subparsers.add_parser("migrate", help="应用迁移")
    migrate_parser.add_argument("--version", help="目标版本（不指定则迁移到最新）")

    # rollback 命令
    rollback_parser = subparsers.add_parser("rollback", help="回滚迁移")
    rollback_parser.add_argument("--version", help="目标版本")
    rollback_parser.add_argument(
        "--steps", type=int, default=1, help="回滚步数（默认: 1）"
    )

    # create 命令
    create_parser = subparsers.add_parser("create", help="创建新迁移")
    create_parser.add_argument("name", help="迁移名称")
    create_parser.add_argument("--description", help="迁移描述")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    manager = MigrationManager()

    try:
        if args.command == "status":
            status = await manager.get_status()
            print("\n迁移状态:")
            print(f"总计: {status['total']}")
            print(f"已应用: {status['applied_count']}")
            print(f"待应用: {status['pending_count']}")

            if status["applied"]:
                print("\n已应用的迁移:")
                for version in status["applied"]:
                    migration = manager.migrations[version]
                    print(f"  ✓ {version} - {migration.name}")

            if status["pending"]:
                print("\n待应用的迁移:")
                for version in status["pending"]:
                    migration = manager.migrations[version]
                    print(f"  [任务] {version} - {migration.name}")

        elif args.command == "migrate":
            print("开始迁移...")
            applied = await manager.migrate(target_version=args.version)
            if applied:
                print(f"\n✓ 成功应用 {len(applied)} 个迁移")
            else:
                print("\n没有需要应用的迁移")

        elif args.command == "rollback":
            print("开始回滚...")
            rolled_back = await manager.rollback(
                target_version=args.version, steps=args.steps
            )
            if rolled_back:
                print(f"\n✓ 成功回滚 {len(rolled_back)} 个迁移")
            else:
                print("\n没有需要回滚的迁移")

        elif args.command == "create":
            print("创建迁移文件...")
            migration_file = await manager.create_migration(
                name=args.name, description=args.description
            )
            print(f"\n✓ 迁移文件已创建: {migration_file}")
            print("请编辑文件添加迁移 SQL")

    except Exception as e:
        print(f"\n✗ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
