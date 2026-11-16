/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ActivityCard 残余覆盖补测：
 * - 状态图标全状态分支：「已中断」终态与未知状态都落图标（前者 Ban 静态、
 *   后者回退时钟），不再宣称活动
 * - 标题的插件页面入口：header 标题点击按声明 pageId/itemId 回调宿主；
 *   声明的 label 参与 title/aria 文案
 *
 * 断言可见行为（图标元素存在 / 回调载荷 / 无障碍文案），不触碰样式实现。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ActivityCard, { getStatusIcon } from '../ActivityCard'
import type { ActivityData, ActivityStatus } from '@/types/activity'

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-1',
    title: 'task_list',
    status: 'completed',
    ...overrides,
  }
}

describe('getStatusIcon — 全状态分支', () => {
  it('interrupted（中断终态）渲染图标且不带呼吸动画', () => {
    const { container } = render(<div>{getStatusIcon('interrupted')}</div>)

    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg?.getAttribute('style') ?? '').not.toContain('breathe')
  })

  it('未知状态回退占位图标（不返回空节点）', () => {
    const { container } = render(<div>{getStatusIcon('mystery' as ActivityStatus)}</div>)

    expect(container.querySelector('svg')).not.toBeNull()
  })
})

describe('ActivityCard — 插件页面入口', () => {
  it('点击标题 → 按声明的 pageId/itemId 打开插件页面', () => {
    const onOpenPanel = vi.fn()
    render(
      <ActivityCard
        activity={makeActivity({
          panelPageId: 'pim_today',
          panelPageItemId: 'todo-7',
          panelPageLabel: '今日待办',
          onOpenPanel,
        })}
      />,
    )

    fireEvent.click(screen.getByText('task_list'))

    expect(onOpenPanel).toHaveBeenCalledWith('pim_today', 'todo-7')
  })

  it('入口按钮带声明 label 的无障碍名（同页多卡可区分）', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          panelPageId: 'pim_week',
          panelPageLabel: '本周',
          onOpenPanel: vi.fn(),
        })}
      />,
    )

    expect(screen.getByRole('button', { name: '本周 task_list' })).toBeInTheDocument()
  })

  it('无 panelPageId 时标题纯文本、无打开入口（不误发回调）', () => {
    render(<ActivityCard activity={makeActivity()} />)

    expect(screen.queryByRole('button', { name: /打开页面/ })).not.toBeInTheDocument()
  })
})
