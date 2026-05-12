"""
添加 JSON 字段表达式索引

为 tasks 表的 task_metadata 和 execution_records 表的 message_data JSON 字段添加表达式索引，
提升按 JSON 字段内部属性查询的性能。

索引说明：
- tasks 表：为 task_metadata 中的 status 字段创建索引
- execution_records 表：为 message_data 中的 status 和 type 字段创建索引

注意：PostgreSQL 使用 ->> 操作符提取 JSON 字段值为文本
"""

from alembic import op


def upgrade():
    """添加 JSON 字段表达式索引"""

    # 为 tasks 表的 task_metadata->>'status' 创建索引
    # 注意：task_metadata 在数据库中的列名是 metadata
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tasks_metadata_status
        ON tasks ((metadata->>'status'))
        """
    )

    # 为 execution_records 表的 message_data->>'status' 创建索引
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_execution_records_message_status
        ON execution_records ((message_data->>'status'))
        """
    )

    # 为 execution_records 表的 message_data->>'type' 创建索引
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_execution_records_message_type
        ON execution_records ((message_data->>'type'))
        """
    )


def downgrade():
    """删除 JSON 字段表达式索引"""

    op.execute(
        """
        DROP INDEX IF EXISTS ix_execution_records_message_type
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS ix_execution_records_message_status
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS ix_tasks_metadata_status
        """
    )
