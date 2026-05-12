"""
Tag 共现矩阵构建器

用于构建和维护 Tag 共现关系矩阵，支持：
1. 完整重建共现矩阵
2. 增量更新共现关系
3. 获取共现矩阵数据

共现定义：同一记忆（memory_id + memory_type）关联的多个 Tag 视为共现
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.memory import TagCooccurrence

logger = logging.getLogger(__name__)


class TagCooccurrenceBuilder:
    """
    Tag 共现矩阵构建器

    负责从 memory_tags 表统计 Tag 共现关系，并维护 tag_cooccurrences 表。
    共现关系定义为：同一记忆（memory_id + memory_type）关联的多个 Tag 同时出现。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化构建器

        Args:
            session: 数据库异步会话
        """
        self.session = session

    async def rebuild_cooccurrence(self) -> dict[str, Any]:
        """
        重建完整的共现矩阵

        执行流程：
        1. 清空 tag_cooccurrences 表
        2. 从 memory_tags 表统计所有共现关系
        3. 插入新的共现记录

        Returns:
            操作结果统计，包含：
            - cleared: 清空的记录数
            - inserted: 插入的新记录数
            - duration_ms: 执行耗时（毫秒）
        """
        start_time = datetime.now()
        logger.info("[TagCooccurrenceBuilder] 开始重建共现矩阵")

        try:
            # 步骤 1: 清空共现表
            clear_result = await self.session.execute(delete(TagCooccurrence))
            cleared_count = clear_result.rowcount or 0
            logger.info(f"[TagCooccurrenceBuilder] 已清空 {cleared_count} 条旧记录")

            # 步骤 2: 统计共现关系
            # 使用 SQL 自连接找出同一记忆中的 Tag 对
            # 条件 mt1.tag_id < mt2.tag_id 确保每对 Tag 只统计一次
            cooccurrence_query = text(
                """
                SELECT
                    mt1.tag_id as tag1,
                    mt2.tag_id as tag2,
                    COUNT(*) as count
                FROM memory_tags mt1
                JOIN memory_tags mt2
                    ON mt1.memory_id = mt2.memory_id
                    AND mt1.memory_type = mt2.memory_type
                    AND mt1.tag_id < mt2.tag_id
                GROUP BY mt1.tag_id, mt2.tag_id
            """
            )

            result = await self.session.execute(cooccurrence_query)
            rows = result.fetchall()

            # 步骤 3: 批量插入共现记录
            inserted_count = 0
            if rows:
                # 准备批量插入数据
                insert_values = [
                    {
                        "tag1_id": row.tag1,
                        "tag2_id": row.tag2,
                        "cooccurrence_count": row.count,
                    }
                    for row in rows
                ]

                # 使用 bulk insert 提高性能
                await self.session.execute(
                    insert(TagCooccurrence), insert_values
                )
                inserted_count = len(insert_values)

            await self.session.commit()

            duration = (datetime.now() - start_time).total_seconds() * 1000
            logger.info(
                f"[TagCooccurrenceBuilder] 共现矩阵重建完成 | "
                f"记录数: {inserted_count} | 耗时: {duration:.2f}ms"
            )

            return {
                "cleared": cleared_count,
                "inserted": inserted_count,
                "duration_ms": round(duration, 2),
            }

        except Exception as e:
            await self.session.rollback()
            logger.error(f"[TagCooccurrenceBuilder] 重建共现矩阵失败: {e}")
            raise

    async def incremental_update(self, since: datetime) -> dict[str, Any]:
        """
        增量更新共现关系

        只统计指定时间后新增的记忆关联，更新或插入共现记录。

        Args:
            since: 时间阈值，只处理此时间后创建的记忆关联

        Returns:
            操作结果统计，包含：
            - updated: 更新的记录数
            - inserted: 插入的新记录数
            - duration_ms: 执行耗时（毫秒）
        """
        start_time = datetime.now()
        logger.info(f"[TagCooccurrenceBuilder] 开始增量更新，时间阈值: {since}")

        try:
            # 查询新增的记忆关联产生的共现关系
            incremental_query = text(
                """
                SELECT
                    mt1.tag_id as tag1,
                    mt2.tag_id as tag2,
                    COUNT(*) as count
                FROM memory_tags mt1
                JOIN memory_tags mt2
                    ON mt1.memory_id = mt2.memory_id
                    AND mt1.memory_type = mt2.memory_type
                    AND mt1.tag_id < mt2.tag_id
                WHERE mt1.created_at >= :since
                   OR mt2.created_at >= :since
                GROUP BY mt1.tag_id, mt2.tag_id
            """
            )

            result = await self.session.execute(
                incremental_query, {"since": since}
            )
            rows = result.fetchall()

            if not rows:
                duration = (datetime.now() - start_time).total_seconds() * 1000
                logger.info(
                    f"[TagCooccurrenceBuilder] 增量更新完成，无新数据 | 耗时: {duration:.2f}ms"
                )
                return {
                    "updated": 0,
                    "inserted": 0,
                    "duration_ms": round(duration, 2),
                }

            # 获取现有共现记录，用于区分更新和插入
            existing_pairs = await self._get_existing_pairs(
                [(row.tag1, row.tag2) for row in rows]
            )

            # 分类处理：已存在的更新，不存在的插入
            to_update = []
            to_insert = []

            for row in rows:
                pair = (row.tag1, row.tag2)
                if pair in existing_pairs:
                    to_update.append(
                        {
                            "tag1_id": row.tag1,
                            "tag2_id": row.tag2,
                            "count": row.count,
                        }
                    )
                else:
                    to_insert.append(
                        {
                            "tag1_id": row.tag1,
                            "tag2_id": row.tag2,
                            "cooccurrence_count": row.count,
                        }
                    )

            # 执行批量插入
            inserted_count = 0
            if to_insert:
                await self.session.execute(
                    insert(TagCooccurrence), to_insert
                )
                inserted_count = len(to_insert)

            # 执行批量更新
            updated_count = 0
            if to_update:
                for item in to_update:
                    # 使用 SQLite 的 upsert 语法（ON CONFLICT DO UPDATE）
                    stmt = (
                        sqlite_insert(TagCooccurrence)
                        .values(
                            tag1_id=item["tag1_id"],
                            tag2_id=item["tag2_id"],
                            cooccurrence_count=item["count"],
                        )
                        .on_conflict_do_update(
                            index_elements=["tag1_id", "tag2_id"],
                            set_={
                                "cooccurrence_count": TagCooccurrence.cooccurrence_count
                                + item["count"],
                                "last_updated": func.now(),
                            },
                        )
                    )
                    await self.session.execute(stmt)
                updated_count = len(to_update)

            await self.session.commit()

            duration = (datetime.now() - start_time).total_seconds() * 1000
            logger.info(
                f"[TagCooccurrenceBuilder] 增量更新完成 | "
                f"更新: {updated_count} | 插入: {inserted_count} | 耗时: {duration:.2f}ms"
            )

            return {
                "updated": updated_count,
                "inserted": inserted_count,
                "duration_ms": round(duration, 2),
            }

        except Exception as e:
            await self.session.rollback()
            logger.error(f"[TagCooccurrenceBuilder] 增量更新失败: {e}")
            raise

    async def _get_existing_pairs(
        self, pairs: list[tuple[int, int]]
    ) -> set[tuple[int, int]]:
        """
        获取已存在的共现记录对

        Args:
            pairs: Tag 对列表 [(tag1_id, tag2_id), ...]

        Returns:
            已存在的 Tag 对集合
        """
        if not pairs:
            return set()

        # 构建查询条件
        conditions = [
            (TagCooccurrence.tag1_id == t1) & (TagCooccurrence.tag2_id == t2)
            for t1, t2 in pairs
        ]

        # 使用 OR 连接所有条件
        from sqlalchemy import or_

        stmt = select(TagCooccurrence.tag1_id, TagCooccurrence.tag2_id).where(
            or_(*conditions)
        )
        result = await self.session.execute(stmt)

        return {(row.tag1_id, row.tag2_id) for row in result.fetchall()}

    async def get_cooccurrence_matrix(self) -> dict[int, dict[int, int]]:
        """
        获取共现矩阵

        返回格式: {tag1_id: {tag2_id: count}}
        用于 Tag 网络检索等场景。

        Returns:
            共现矩阵字典，外层 key 为 tag1_id，内层 key 为 tag2_id，value 为共现次数
        """
        logger.debug("[TagCooccurrenceBuilder] 获取共现矩阵")

        stmt = select(
            TagCooccurrence.tag1_id,
            TagCooccurrence.tag2_id,
            TagCooccurrence.cooccurrence_count,
        )
        result = await self.session.execute(stmt)

        # 构建矩阵（对称矩阵，只存储 tag1_id < tag2_id 的部分）
        matrix: dict[int, dict[int, int]] = {}

        for row in result.fetchall():
            tag1, tag2, count = row.tag1_id, row.tag2_id, row.cooccurrence_count

            if tag1 not in matrix:
                matrix[tag1] = {}
            matrix[tag1][tag2] = count

        logger.debug(f"[TagCooccurrenceBuilder] 共现矩阵加载完成，包含 {len(matrix)} 个 Tag")
        return matrix

    async def get_cooccurrence_stats(self) -> dict[str, Any]:
        """
        获取共现统计信息

        Returns:
            统计信息字典，包含：
            - total_pairs: 共现对总数
            - total_occurrences: 共现次数总和
            - avg_cooccurrence: 平均共现次数
            - max_cooccurrence: 最大共现次数
            - top_pairs: 共现次数最高的前 10 对
        """
        # 基础统计
        count_stmt = select(func.count()).select_from(TagCooccurrence)
        total_pairs = await self.session.scalar(count_stmt) or 0

        sum_stmt = select(func.sum(TagCooccurrence.cooccurrence_count))
        total_occurrences = await self.session.scalar(sum_stmt) or 0

        avg_stmt = select(func.avg(TagCooccurrence.cooccurrence_count))
        avg_cooccurrence = await self.session.scalar(avg_stmt) or 0.0

        max_stmt = select(func.max(TagCooccurrence.cooccurrence_count))
        max_cooccurrence = await self.session.scalar(max_stmt) or 0

        # Top 10 共现对
        top_stmt = (
            select(
                TagCooccurrence.tag1_id,
                TagCooccurrence.tag2_id,
                TagCooccurrence.cooccurrence_count,
            )
            .order_by(TagCooccurrence.cooccurrence_count.desc())
            .limit(10)
        )
        top_result = await self.session.execute(top_stmt)
        top_pairs = [
            {
                "tag1_id": row.tag1_id,
                "tag2_id": row.tag2_id,
                "count": row.cooccurrence_count,
            }
            for row in top_result.fetchall()
        ]

        return {
            "total_pairs": total_pairs,
            "total_occurrences": int(total_occurrences),
            "avg_cooccurrence": round(float(avg_cooccurrence), 2),
            "max_cooccurrence": max_cooccurrence,
            "top_pairs": top_pairs,
        }

    async def clear_cooccurrence(self) -> int:
        """
        清空所有共现记录

        Returns:
            删除的记录数
        """
        logger.warning("[TagCooccurrenceBuilder] 清空所有共现记录")

        result = await self.session.execute(delete(TagCooccurrence))
        await self.session.commit()

        deleted_count = result.rowcount or 0
        logger.info(f"[TagCooccurrenceBuilder] 已清空 {deleted_count} 条共现记录")
        return deleted_count
