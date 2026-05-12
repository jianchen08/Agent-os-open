"""
添加 Task 表的验收标准相关字段

添加字段：
- acceptance_criteria: JSON 字段，存储 AC 详细信息
- total_criteria: 总 AC 数量
- passed_criteria: 已通过的 AC 数量
- failed_criteria: 失败的 AC 数量
- progress_percent: 进度百分比
- updated_at: 更新时间
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade():
    """添加字段"""
    # 添加 acceptance_criteria 字段
    op.add_column(
        "tasks",
        sa.Column(
            "acceptance_criteria",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
            comment="验收标准详细信息列表（包含 id, status, retry_count 等）",
        ),
    )

    # 添加进度统计字段
    op.add_column(
        "tasks",
        sa.Column(
            "total_criteria",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="总验收标准数",
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "passed_criteria",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="已通过的验收标准数",
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "failed_criteria",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="失败的验收标准数",
        ),
    )

    op.add_column(
        "tasks",
        sa.Column(
            "progress_percent",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            comment="进度百分比",
        ),
    )

    # 添加 updated_at 字段
    op.add_column(
        "tasks",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="更新时间",
        ),
    )


def downgrade():
    """删除字段"""
    op.drop_column("tasks", "updated_at")
    op.drop_column("tasks", "progress_percent")
    op.drop_column("tasks", "failed_criteria")
    op.drop_column("tasks", "passed_criteria")
    op.drop_column("tasks", "total_criteria")
    op.drop_column("tasks", "acceptance_criteria")
