"""
添加 Task 表的任务流程改进相关字段

添加字段：
- best_passed_count: 历史最佳通过指标数（进步重置机制）
- last_passed_count: 上次评估通过指标数（进步重置机制）
- continuation_count: 续执行次数（只统计，不限制）
- last_continuation_at: 上次续执行时间
"""

import sqlalchemy as sa
from alembic import op


def upgrade():
    """添加字段"""
    # 进步重置机制相关字段
    op.add_column(
        "tasks",
        sa.Column(
            "best_passed_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="历史最佳通过指标数",
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "last_passed_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="上次评估通过指标数",
        ),
    )

    # 续执行机制相关字段
    op.add_column(
        "tasks",
        sa.Column(
            "continuation_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="续执行次数（只统计，不限制）",
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "last_continuation_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="上次续执行时间",
        ),
    )


def downgrade():
    """删除字段"""
    op.drop_column("tasks", "last_continuation_at")
    op.drop_column("tasks", "continuation_count")
    op.drop_column("tasks", "last_passed_count")
    op.drop_column("tasks", "best_passed_count")
