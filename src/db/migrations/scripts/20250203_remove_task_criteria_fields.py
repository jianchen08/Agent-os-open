"""
移除 Task 表的验收标准相关字段

移除字段：
- acceptance_criteria: JSON 字段，存储 AC 详细信息
- total_criteria: 总 AC 数量
- passed_criteria: 已通过的 AC 数量
- failed_criteria: 失败的 AC 数量
- progress_percent: 进度百分比

这些字段已被移除，相关信息现在存储在 task_metadata JSON 字段中
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20250203_remove_task_criteria_fields"
down_revision = "20250119_add_task_ac_fields"
branch_labels = None
depends_on = None


def upgrade():
    """移除字段"""
    # 移除验收标准相关字段
    op.drop_column("tasks", "acceptance_criteria")
    op.drop_column("tasks", "total_criteria")
    op.drop_column("tasks", "passed_criteria")
    op.drop_column("tasks", "failed_criteria")
    op.drop_column("tasks", "progress_percent")


def downgrade():
    """添加字段（回滚用）"""
    from sqlalchemy.dialects import postgresql

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
