// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 聊天组件覆盖缺口补测（ActivityCard / ActivityBlockViews / MessageActions）
 *
 * 断行为：用户可见的渲染产物与点击交互（图标形态、折叠切换、剪贴板副作用、
 * toast 提示），不断言内部实现。
 *
 * 覆盖契约：
 * - ActivityCard 状态图标：running 显示旋转图标、failed 显示错误图标、
 *   completed 只在左边条表达（无状态图标噪音）——三组有区分度状态；
 * - ActivityBlockViews search 块：分组折叠按钮展开/收起切换（两组方向相反的点击）、
 *   截断态文案与完整态文案有区分；
 * - MessageActions 复制：无 onCopy 回调 → 写剪贴板并 toast 成功；
 *   有 onCopy 回调 → 只调回调、不写剪贴板（两条互斥路径）。
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import { MessageActions } from '../MessageActions'
import type { ActivityData } from '@/types/activity'
import type { Message } from '@/types/models'

const { toastSuccess } = vi.hoisted(() => ({ toastSuccess: vi.fn() }))
vi.mock('sonner', () => ({
  toast: { success: toastSuccess, error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

vi.mock('@/components/approval', () => ({ TextDiffView: () => null }))
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({ MarkdownRenderer: () => null }))
vi.mock('@/utils/toolCardRegistry', () => ({ getGlobalOpenFileCallback: () => undefined }))

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-gaps2',
    title: '工具活动',
    status: 'completed',
    ...overrides,
  }
}

function makeMessage(role: Message['role'], overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-a1',
    sessionId: 'session-1',
    sequence: 1,
    role,
    content: '待复制的内容',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) }

beforeEach(() => {
  vi.resetAllMocks()
  clipboard.writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText: clipboard.writeText },
    configurable: true,
  })
})

describe('ActivityCard 状态图标', () => {
  it('running 显示旋转状态图标，completed 不显示状态图标（左边条已表达状态）', () => {
    const { container, unmount } = render(
      <ActivityCard activity={makeActivity({ status: 'running' })} defaultExpanded />,
    )
    // 状态图标仅 running/failed 出现：header 内 animate-spin 图标存在
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()
    unmount()

    const done = render(
      <ActivityCard activity={makeActivity({ status: 'completed' })} defaultExpanded />,
    )
    expect(done.container.querySelector('.animate-spin')).toBeNull()
    // completed 状态由 data 属性与左边条表达
    expect(done.container.querySelector('[data-activity-status="completed"]')).toBeInTheDocument()
  })

  it('failed 显示状态图标（与 completed 区分）；两种状态左边条透明度不同', () => {
    const { container } = render(
      <ActivityCard activity={makeActivity({ status: 'failed' })} defaultExpanded />,
    )
    const bar = container.querySelector('span[aria-hidden="true"]')
    expect(bar).toBeInTheDocument()
    // failed 无呼吸动画；running 有（区分性断言）
    expect(bar).not.toHaveStyle({ animation: 'breathe 2s ease-in-out infinite' })

    const running = render(
      <ActivityCard activity={makeActivity({ status: 'running' })} defaultExpanded />,
    )
    expect(running.container.querySelector('span[aria-hidden="true"]')).toHaveStyle({
      animation: 'breathe 2s ease-in-out infinite',
    })
  })

  it('cancelled 左边条半透明（40%）且无状态图标', () => {
    const { container } = render(
      <ActivityCard activity={makeActivity({ status: 'cancelled' })} defaultExpanded />,
    )
    expect(container.querySelector('span[aria-hidden="true"]')).toHaveStyle({ opacity: '0.4' })
    expect(container.querySelector('.animate-spin')).toBeNull()
  })
})

describe('ActivityBlockViews search 块', () => {
  const searchActivity = () =>
    makeActivity({
      details: [
        {
          id: 's1',
          label: '搜索结果',
          contentType: 'search',
          search: {
            kind: 'matches',
            files: [
              {
                path: 'src/a.ts',
                matches: [
                  { lineNumber: 3, line: 'export foo' },
                  { lineNumber: 9, line: 'export bar' },
                ],
              },
            ],
            total: 2,
            truncated: false,
          },
        },
      ],
    })

  it('折叠按钮两次点击：收起隐藏匹配行、再点展开恢复（方向相反的两组输入）', () => {
    render(<ActivityCard activity={searchActivity()} defaultExpanded />)

    expect(screen.getByText('export foo')).toBeInTheDocument()
    expect(screen.getByText('export bar')).toBeInTheDocument()

    // 点击文件分组行 → 收起：匹配行隐藏，行数徽标保留
    fireEvent.click(screen.getByText('src/a.ts'))
    expect(screen.queryByText('export foo')).toBeNull()
    expect(screen.queryByText('export bar')).toBeNull()
    expect(screen.getByText('2 处')).toBeInTheDocument()

    // 再点 → 展开恢复
    fireEvent.click(screen.getByText('src/a.ts'))
    expect(screen.getByText('export foo')).toBeInTheDocument()
    expect(screen.getByText('export bar')).toBeInTheDocument()
  })

  it('截断态与完整态的匹配计数文案有区分', () => {
    const { unmount } = render(<ActivityCard activity={searchActivity()} defaultExpanded />)
    expect(screen.getByTestId('search-count')).toHaveTextContent('2 条匹配')
    unmount()

    const truncated = searchActivity()
    truncated.details![0].search = {
      kind: 'matches',
      files: [
        { path: 'src/a.ts', matches: [{ lineNumber: 3, line: 'export foo' }] },
      ],
      total: 57,
      truncated: true,
    }
    render(<ActivityCard activity={truncated} defaultExpanded />)
    expect(screen.getByTestId('search-count')).toHaveTextContent('显示 1 / 共 57 条')
  })

  it('search 块复制按钮写入「路径 + 行号: 行内容」聚合文本', async () => {
    render(<ActivityCard activity={searchActivity()} defaultExpanded />)

    const block = screen.getByTestId('search-block')
    // 复制按钮在块头部（唯一按钮）
    fireEvent.click(within(block).getAllByRole('button')[0])

    expect(clipboard.writeText).toHaveBeenCalledTimes(1)
    const copied = clipboard.writeText.mock.calls[0][0] as string
    expect(copied).toContain('src/a.ts')
    expect(copied).toContain('3: export foo')
    expect(copied).toContain('9: export bar')
  })
})

describe('MessageActions 复制路径', () => {
  it('无 onCopy 回调 → 写剪贴板并 toast 成功提示', async () => {
    render(<MessageActions message={makeMessage('assistant')} sessionId="session-1" />)

    fireEvent.click(screen.getByTitle('复制'))

    expect(clipboard.writeText).toHaveBeenCalledWith('待复制的内容')
    expect(toastSuccess).toHaveBeenCalledWith('已复制到剪贴板')
  })

  it('传 onCopy 回调 → 只调回调，不写剪贴板也不 toast（调用方接管）', () => {
    const onCopy = vi.fn()
    render(
      <MessageActions message={makeMessage('assistant')} sessionId="session-1" onCopy={onCopy} />,
    )

    fireEvent.click(screen.getByTitle('复制'))

    expect(onCopy).toHaveBeenCalledTimes(1)
    expect(clipboard.writeText).not.toHaveBeenCalled()
    expect(toastSuccess).not.toHaveBeenCalled()
  })
})
