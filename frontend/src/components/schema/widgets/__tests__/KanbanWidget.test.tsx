/**
 * KanbanWidget 骨架测试 —— 静态列/卡片渲染（不做拖拽）
 *
 * 设计原则：KanbanWidget 只负责把 columns + data 渲染成横向列 + 卡片堆叠，
 * 不引入 dnd-kit/sortablejs。真实数据可由插件通过 props.data 提供。
 *
 * 数据结构：
 * - columns: [{ id, title }]
 * - data   : [{ id, columnId, title, ... }]
 *   → 按 columnId 分组到对应列
 *
 * 可观察行为（AC）：
 * - AC-1: 给定 columns + data → 渲染出全部列 + 全部卡片
 * - AC-2: 无 columns / 无 data → 空状态占位
 * - AC-3: 卡片按 columnId 正确分组（同列归集，跨列不串）
 * - AC-4: 有 pluginId → 标记「由插件提供」
 * - AC-5: 列内无卡片时仍渲染列（空列可识别）
 */
import { render, screen, within } from '@testing-library/react'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { KanbanWidget } from '../KanbanWidget'

const columns = [
  { id: 'todo', title: '待办' },
  { id: 'doing', title: '进行中' },
  { id: 'done', title: '已完成' },
]

const cards = [
  { id: 'c1', columnId: 'todo', title: '设计 API' },
  { id: 'c2', columnId: 'todo', title: '建数据库表' },
  { id: 'c3', columnId: 'doing', title: '写前端骨架' },
  { id: 'c4', columnId: 'done', title: '需求评审' },
]

describe('KanbanWidget 骨架', () => {
  it('AC-1: 给定 columns + data → 渲染全部列与卡片', () => {
    render(<KanbanWidget columns={columns} data={cards} />)

    // 三列都渲染
    expect(screen.getByTestId('kanban-column-todo')).toBeInTheDocument()
    expect(screen.getByTestId('kanban-column-doing')).toBeInTheDocument()
    expect(screen.getByTestId('kanban-column-done')).toBeInTheDocument()
    // 列标题
    expect(screen.getByText('待办')).toBeInTheDocument()
    expect(screen.getByText('进行中')).toBeInTheDocument()

    // 四张卡片都渲染
    expect(screen.getByText('设计 API')).toBeInTheDocument()
    expect(screen.getByText('需求评审')).toBeInTheDocument()
  })

  it('AC-2: 无 columns / 无 data → 空状态占位', () => {
    const { rerender } = render(<KanbanWidget />)
    expect(screen.getByTestId('kanban-empty')).toBeInTheDocument()

    // 仅给空数组也视为空状态
    rerender(<KanbanWidget columns={[]} data={[]} />)
    expect(screen.getByTestId('kanban-empty')).toBeInTheDocument()
  })

  it('AC-3: 卡片按 columnId 正确分组', () => {
    render(<KanbanWidget columns={columns} data={cards} />)

    // todo 列含 c1、c2
    const todoCol = screen.getByTestId('kanban-column-todo')
    expect(within(todoCol).getByText('设计 API')).toBeInTheDocument()
    expect(within(todoCol).getByText('建数据库表')).toBeInTheDocument()
    expect(within(todoCol).queryByText('需求评审')).not.toBeInTheDocument()

    // doing 列只含 c3
    const doingCol = screen.getByTestId('kanban-column-doing')
    expect(within(doingCol).getByText('写前端骨架')).toBeInTheDocument()
    expect(within(doingCol).queryByText('设计 API')).not.toBeInTheDocument()

    // done 列只含 c4
    const doneCol = screen.getByTestId('kanban-column-done')
    expect(within(doneCol).getByText('需求评审')).toBeInTheDocument()
    expect(within(doneCol).queryByText('写前端骨架')).not.toBeInTheDocument()
  })

  it('AC-4: 有 pluginId → 标记「由插件提供」', () => {
    render(<KanbanWidget columns={columns} data={cards} pluginId="trello-ext" />)

    const board = screen.getByTestId('kanban-board')
    expect(board.getAttribute('data-plugin-id')).toBe('trello-ext')
    expect(board.textContent).toMatch(/trello-ext|插件|plugin/)
  })

  it('AC-5: 列内无卡片时仍渲染列（空列可识别）', () => {
    render(
      <KanbanWidget
        columns={columns}
        data={[{ id: 'c1', columnId: 'todo', title: '唯一卡' }]}
      />,
    )

    // done 列存在但为空
    const doneCol = screen.getByTestId('kanban-column-done')
    expect(doneCol).toBeInTheDocument()
    expect(within(doneCol).queryByText('唯一卡')).not.toBeInTheDocument()
  })
})
