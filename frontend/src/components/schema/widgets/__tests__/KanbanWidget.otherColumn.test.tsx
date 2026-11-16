/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 看板卡片列归属兜底补测：columnId 未命中任何声明列的卡片统一收进「其他」列
 * （不丢卡、不静默吞卡片）。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { KanbanWidget } from '../KanbanWidget'

const columns = [
  { id: 'todo', title: '待办' },
  { id: 'done', title: '已完成' },
]

describe('KanbanWidget — 未知列兜底', () => {
  it('columnId 未命中声明列 → 收进「其他」列', () => {
    render(
      <KanbanWidget
        columns={columns}
        data={[
          { id: 'c1', columnId: 'todo', title: '正常卡片' },
          { id: 'c2', columnId: 'unknown-col', title: '孤儿卡片' },
        ]}
      />,
    )

    expect(screen.getByText('其他')).toBeInTheDocument()
    expect(screen.getByText('孤儿卡片')).toBeInTheDocument()
  })

  it('全部卡片命中声明列 → 不出现「其他」列', () => {
    render(
      <KanbanWidget columns={columns} data={[{ id: 'c1', columnId: 'done', title: '已归档' }]} />,
    )

    expect(screen.queryByText('其他')).not.toBeInTheDocument()
  })
})
