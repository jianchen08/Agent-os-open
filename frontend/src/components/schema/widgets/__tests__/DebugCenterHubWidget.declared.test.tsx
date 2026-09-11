/**
 * DebugCenterHubWidget 声明组台测试
 *
 * 核验「子页清单声明驱动」：tab 栏来自 debug_center 插件 space=debug_center
 * 页声明（id/title/icon/order/widget），内容经 renderPageContent widget 分支
 * 解析注册名；声明为空时显式占位（不静默空白）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DebugCenterHubWidget } from '../DebugCenterHubWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

// 子页组件按注册名打桩（hub 只负责声明→注册名解析，子页本体有自己的车道）
vi.mock('@/pages/debug/DbAdminPage', () => ({ DbAdminPage: () => <div data-testid="stub-db" /> }))
vi.mock('@/pages/debug/DebugTasksPage', () => ({ DebugTasksPage: () => <div data-testid="stub-tasks" /> }))

import { DbAdminPage } from '@/pages/debug/DbAdminPage'
import { DebugTasksPage } from '@/pages/debug/DebugTasksPage'

function seedSubPages(ids: string[]) {
  const pages = ids.map((id, i) => ({
    id,
    title: `子页${i}`,
    icon: '🔧',
    space: 'debug_center',
    slot: 'tab',
    order: (i + 1) * 10,
    widget: id === 'db_admin' ? 'debug_db_admin' : 'debug_tasks',
  }))
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      { plugin_id: 'debug_center', plugin_name: 'Debug Center', contributes: { pages } },
    ],
    plugin_configs: [],
  })
}

beforeEach(() => {
  widgetRegistry.register('debug_db_admin', DbAdminPage as never, {
    name: 'debug_db_admin',
    supportedSpaces: ['workspace'],
  })
  widgetRegistry.register('debug_tasks', DebugTasksPage as never, {
    name: 'debug_tasks',
    supportedSpaces: ['workspace'],
  })
})

describe('DebugCenterHubWidget — 子页声明组台', () => {
  it('声明子页渲染 tab 栏 + 首个子页内容', () => {
    seedSubPages(['db_admin', 'tasks_page'])
    render(<DebugCenterHubWidget />)
    expect(screen.getByTestId('debug-hub-tab-db_admin')).toBeInTheDocument()
    expect(screen.getByTestId('debug-hub-tab-tasks_page')).toBeInTheDocument()
    expect(screen.getByTestId('stub-db')).toBeInTheDocument()
  })

  it('点击声明 tab 切换子页内容', () => {
    seedSubPages(['db_admin', 'tasks_page'])
    render(<DebugCenterHubWidget />)
    fireEvent.click(screen.getByTestId('debug-hub-tab-tasks_page'))
    expect(screen.getByTestId('stub-tasks')).toBeInTheDocument()
    expect(screen.queryByTestId('stub-db')).not.toBeInTheDocument()
  })

  it('声明为空 → 显式占位（不静默空白）', () => {
    seedSubPages([])
    render(<DebugCenterHubWidget />)
    expect(screen.getByText(/暂无声明子页/)).toBeInTheDocument()
  })

  it('order 声明驱动排序（后声明序小者先渲染为首屏）', () => {
    // tasks_page order=10 < db_admin order=20 → 首屏是 tasks
    const pages = [
      { id: 'db_admin', title: '数据库', icon: '🗄️', space: 'debug_center', slot: 'tab', order: 20, widget: 'debug_db_admin' },
      { id: 'tasks_page', title: '任务', icon: '⚙️', space: 'debug_center', slot: 'tab', order: 10, widget: 'debug_tasks' },
    ]
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'debug_center', plugin_name: 'Debug Center', contributes: { pages } },
      ],
      plugin_configs: [],
    })
    render(<DebugCenterHubWidget />)
    expect(screen.getByTestId('stub-tasks')).toBeInTheDocument()
  })
})
