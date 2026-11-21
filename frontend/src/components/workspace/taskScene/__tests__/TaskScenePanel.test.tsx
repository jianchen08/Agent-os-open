/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * TaskScenePanel 渲染测试（重构后：运行实例看板 + 产物 + 折叠历史）。
 *
 * 判据（本轮重构 AC）：
 * - AC-1 默认首屏是「运行中实例」区；逐条工具调用不在默认可见区（历史区默认折叠）
 * - AC-2 实例按真实通道分（data-instance-category ∈ terminal/browser/desktop/software/phone）
 * - AC-3 卡片含「当前动作」与已运行时长（elapsed ≥ 0）
 * - AC-5 无运行实例给空态，不白屏
 * - AC-6 历史区展开后仍可按类别筛、可看命令 stdout（复盘能力不丢）
 * - 存活判据边界：管道未运行时，在途状态不算"运行中"（不冒充）
 */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { TaskScenePanel } from '@/components/workspace/taskScene/TaskScenePanel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { expandAllGroups, expandHistory } from './sceneTestHelpers'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

/** 构造一条 assistant 消息（parts 里的 tool_call 就是现场条目） */
function messageWith(parts: MessagePart[], id = 'm1'): Message {
  return {
    id,
    sessionId: 'sess-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: '2026-11-16T09:00:00.000Z',
    parts,
  }
}

function toolCall(partial: Partial<Extract<MessagePart, { type: 'tool_call' }>>): MessagePart {
  return {
    type: 'tool_call',
    callId: 'call-1',
    name: 'bash',
    args: {},
    state: 'done',
    ...partial,
  } as MessagePart
}

/** 管道元数据（status 影响「存活」判定） */
function pipeline(id: string, status: 'running' | 'completed'): Record<string, unknown> {
  return {
    [id]: {
      pipelineId: id,
      sessionId: 'sess-1',
      level: 1,
      tabId: null,
      agentName: 'main',
      status,
      parentId: null,
      unreadCount: 0,
    },
  }
}

describe('TaskScenePanel — 运行中实例看板（默认视图）', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      initialLoadingByPipeline: {},
    })
  })

  it('管道在跑 + 在途调用：按通道出卡片，含当前动作与已运行时长', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-live': [
          messageWith([
            toolCall({
              callId: 'c-nav',
              name: 'browser_navigate',
              args: { url: 'https://example.com/login' },
              state: 'calling',
            }),
          ]),
        ],
      },
      pipelines: pipeline('task-live', 'running'),
    })
    render(<TaskScenePanel taskId="task-live" />)

    const cards = screen.getAllByTestId('scene-instance-card')
    expect(cards).toHaveLength(1)
    expect(cards[0]?.getAttribute('data-instance-category')).toBe('browser')
    expect(cards[0]?.getAttribute('data-instance-state')).toBe('running')
    expect(cards[0]?.getAttribute('data-instance-key')).toBe('browser')
    // host 只作当前焦点展示（同一会话的截图调用不带 url，不应拆成第二个实例）
    expect(cards[0]?.textContent).toContain('example.com')
    // 当前动作 = 该实例最新一条调用的行文案
    expect(screen.getByTestId('scene-instance-current')).toHaveTextContent(
      '导航 https://example.com/login',
    )
    // 已运行时长是数值且非负（起点缺失才给 '--'）
    const elapsed = Number(screen.getByTestId('scene-instance-elapsed').getAttribute('data-elapsed-ms'))
    expect(Number.isFinite(elapsed)).toBe(true)
    expect(elapsed).toBeGreaterThanOrEqual(0)
    expect(screen.getByTestId('task-scene-live-badge')).toHaveTextContent('1 个实例运行中')
  })

  it('默认视图不铺开工具调用流水：历史折叠，scene-row 不在文档里', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-default': [
          messageWith([
            toolCall({ callId: 'c1', name: 'bash', args: { command: 'ls' } }),
            toolCall({ callId: 'c2', name: 'browser_navigate', args: { url: 'https://a.test' } }),
          ]),
        ],
      },
      pipelines: pipeline('task-default', 'running'),
    })
    render(<TaskScenePanel taskId="task-default" />)

    expect(screen.getByTestId('task-scene-instances')).toBeInTheDocument()
    expect(screen.getByTestId('task-scene-artifacts')).toBeInTheDocument()
    expect(screen.getByTestId('task-scene-history')).toHaveAttribute('data-expanded', 'false')
    // 逐条调用行不在默认可见区（这是重构的核心诉求）
    expect(screen.queryAllByTestId('scene-row')).toHaveLength(0)
    expect(screen.getByTestId('task-scene-history-toggle')).toHaveTextContent('2 条调用')
  })

  it('无在途调用：给空态文案，不白屏也不留假卡片', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-idle': [
          messageWith([toolCall({ callId: 'c1', name: 'bash', args: { command: 'ls' }, state: 'done' })]),
        ],
      },
      pipelines: pipeline('task-idle', 'completed'),
    })
    render(<TaskScenePanel taskId="task-idle" />)

    expect(screen.getByTestId('scene-instance-empty')).toHaveTextContent('当前没有正在运行的实例')
    expect(screen.queryAllByTestId('scene-instance-card')).toHaveLength(0)
    expect(screen.getByTestId('task-scene-live-badge')).toHaveTextContent('无运行实例')
  })

  it('管道在跑但无通道调用：空态文案区分「在跑但没进行中的通道调用」', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: { 'task-run-only': [] },
      pipelines: pipeline('task-run-only', 'running'),
    })
    render(<TaskScenePanel taskId="task-run-only" />)
    expect(screen.getByTestId('scene-instance-empty')).toHaveTextContent(
      '任务在跑，但当前没有进行中的通道调用',
    )
  })

  it('存活边界：同一条在途调用，管道已结束不算运行中（两组有区分度输入）', () => {
    const messages = [
      messageWith([
        toolCall({ callId: 'c-phone', name: 'phone_control', args: { action: 'screenshot' }, state: 'calling' }),
      ]),
    ]
    usePipelineMessageStore.setState({
      messagesByPipeline: { 'task-edge': messages },
      pipelines: pipeline('task-edge', 'running'),
    })
    const { unmount } = render(<TaskScenePanel taskId="task-edge" />)
    expect(screen.getAllByTestId('scene-instance-card')).toHaveLength(1)
    unmount()

    // 同一份活动，管道转为 completed → 不冒充运行中（遗留状态不当活实例）
    usePipelineMessageStore.setState({ pipelines: pipeline('task-edge', 'completed') })
    render(<TaskScenePanel taskId="task-edge" />)
    expect(screen.queryAllByTestId('scene-instance-card')).toHaveLength(0)
    expect(screen.getByTestId('scene-instance-empty')).toBeInTheDocument()
  })

  it('多通道并行：终端/桌面/软件各自成卡，类别互不相同', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-multi': [
          messageWith([
            toolCall({ callId: 'c1', name: 'bash', args: { command: 'npm run dev' }, state: 'calling' }),
            toolCall({ callId: 'c2', name: 'desktop_capture', args: { window: '记事本' }, state: 'calling' }),
            toolCall({ callId: 'c3', name: 'desktop_launch', args: { app: 'notepad' }, state: 'calling' }),
          ]),
        ],
      },
      pipelines: pipeline('task-multi', 'running'),
    })
    render(<TaskScenePanel taskId="task-multi" />)

    const categories = screen
      .getAllByTestId('scene-instance-card')
      .map((card) => card.getAttribute('data-instance-category'))
    expect(new Set(categories)).toEqual(new Set(['terminal', 'desktop', 'software']))
  })
})

describe('TaskScenePanel — 活动历史（默认折叠的复盘区）', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      initialLoadingByPipeline: {},
    })
  })

  it('展开历史：按实例分组，三类条目各在组内且 data-scene-kind 区分命令/浏览器', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-1': [
          messageWith([
            toolCall({
              callId: 'c-bash',
              name: 'bash',
              args: { command: 'echo hi' },
              result: { stdout: 'hi\n', exit_code: 0 },
              durationMs: 120,
            }),
            toolCall({
              callId: 'c-nav',
              name: 'browser_navigate',
              args: { url: 'https://example.com/login' },
              result: { status: 'ok', url: 'https://example.com/login' },
            }),
            toolCall({
              callId: 'c-shot',
              name: 'browser_take_screenshot',
              args: {},
              result: { status: 'ok', file_path: 'shots/a.png' },
            }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="task-1" title="示例任务" />)
    await expandAllGroups(user)

    const rows = screen.getAllByTestId('scene-row')
    expect(rows).toHaveLength(3)
    const kinds = rows.map((r) => r.getAttribute('data-scene-kind'))
    expect(kinds).toEqual(['terminal', 'browser', 'browser'])
    expect(new Set(kinds).size).toBe(2)
    // 行文案据实取参（组头也复述最新一条，故在行内断言避免多命中）
    expect(within(rows[0] as HTMLElement).getByText('echo hi')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('导航 https://example.com/login')).toBeInTheDocument()
    // 分组按实例：终端 1 组、浏览器 1 组
    expect(screen.getAllByTestId('scene-history-group')).toHaveLength(2)
  })

  it('命令行展开显示终端块（完整命令行 + exit 码 + stdout）', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-2': [
          messageWith([
            toolCall({
              callId: 'c-bash-2',
              name: 'bash',
              args: { command: 'pytest -q', working_dir: '/workspace' },
              result: { stdout: '3 passed', exit_code: 0 },
            }),
          ]),
        ],
      },
    })

    render(<TaskScenePanel taskId="task-2" />)
    // 默认视图不渲染终端块（流水已退到历史）
    expect(screen.queryByTestId('terminal-block')).not.toBeInTheDocument()

    await expandAllGroups(user)
    await user.click(screen.getByTestId('scene-row').querySelector('button') as HTMLElement)

    const block = screen.getByTestId('terminal-block')
    expect(block.textContent).toContain('pytest -q')
    expect(block.textContent).toContain('3 passed')
    expect(block.textContent).toContain('exit 0')
  })

  it('无现场记录：实例区与历史区各自给空态文案，不白屏', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({ messagesByPipeline: { 'task-3': [] } })
    render(<TaskScenePanel taskId="task-3" />)
    expect(screen.getByTestId('scene-instance-empty')).toBeInTheDocument()
    await expandHistory(user)
    expect(screen.getByTestId('task-scene-empty')).toHaveTextContent('还没有产生现场记录')
    expect(screen.getByTestId('task-scene-panel')).toHaveAttribute('data-task-id', 'task-3')
  })

  it('缺 taskId：给出明确提示而不是渲染假现场', () => {
    render(<TaskScenePanel />)
    expect(screen.queryByTestId('task-scene-panel')).not.toBeInTheDocument()
    expect(screen.getByText(/缺少任务坐标/)).toBeInTheDocument()
  })

  it('类别筛选：只看浏览器会滤掉命令行（展开历史后可用）', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-4': [
          messageWith([
            toolCall({ callId: 'c1', name: 'bash', args: { command: 'ls' } }),
            toolCall({ callId: 'c2', name: 'browser_navigate', args: { url: 'https://a.test' } }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="task-4" />)
    await expandAllGroups(user)
    expect(screen.getAllByTestId('scene-row')).toHaveLength(2)

    await user.click(screen.getByTestId('task-scene-filter-browser'))
    const rows = screen.getAllByTestId('scene-row')
    expect(rows).toHaveLength(1)
    expect(rows[0]?.getAttribute('data-scene-kind')).toBe('browser')
  })

  it('筛选后「清除筛选」恢复全部行；无筛选时不给清除入口', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-clear': [
          messageWith([
            toolCall({ callId: 'c1', name: 'bash', args: { command: 'ls' } }),
            toolCall({ callId: 'c2', name: 'browser_navigate', args: { url: 'https://a.test' } }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="task-clear" />)
    await expandAllGroups(user)
    expect(screen.queryByText('清除筛选')).not.toBeInTheDocument()

    await user.click(screen.getByTestId('task-scene-filter-browser'))
    expect(screen.getAllByTestId('scene-row')).toHaveLength(1)

    await user.click(screen.getByText('清除筛选'))
    expect(screen.getAllByTestId('scene-row')).toHaveLength(2)
    expect(screen.queryByText('清除筛选')).not.toBeInTheDocument()
  })

  it('单组超过渲染上限（100）出现组内「加载更多」，点击补齐且按钮消失', async () => {
    const user = userEvent.setup()
    const parts = Array.from({ length: 105 }, (_, i) =>
      toolCall({ callId: `c-${i}`, name: 'bash', args: { command: `echo ${i}` } }),
    )
    usePipelineMessageStore.setState({
      messagesByPipeline: { 'task-more': [messageWith(parts)] },
    })
    render(<TaskScenePanel taskId="task-more" />)
    await expandAllGroups(user)

    expect(screen.getAllByTestId('scene-row')).toHaveLength(100)
    const loadMore = screen.getByTestId('scene-history-load-more')
    expect(loadMore).toHaveTextContent('还剩 5 条')

    await user.click(loadMore)
    expect(screen.getAllByTestId('scene-row')).toHaveLength(105)
    expect(screen.queryByTestId('scene-history-load-more')).not.toBeInTheDocument()
  })

  it('文件树按钮：按 taskId 打开工作空间文件树页签（带会话归属）', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({ messagesByPipeline: { 'task-tree': [] } })
    render(<TaskScenePanel taskId="task-tree" title="产物任务" sessionId="sess-1" />)

    await user.click(screen.getByTestId('task-scene-open-file-tree'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({
      id: 'ws-tree-task-tree',
      title: '产物任务',
      component: 'file_tree',
      dataSource: 'workspace://task-tree',
      ownerSessionId: 'sess-1',
    })
  })
})

describe('TaskScenePanel — 七类筛选恒在 + 设备/软件条目可筛（历史区）', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      initialLoadingByPipeline: {},
    })
  })

  it('筛选条恒有 7 类按钮（含 desktop/phone/software），无数据类别计数为 0', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-7': [messageWith([toolCall({ callId: 'c1', name: 'bash', args: { command: 'ls' } })])],
      },
    })
    render(<TaskScenePanel taskId="task-7" />)
    await expandHistory(user)

    for (const category of ['terminal', 'browser', 'desktop', 'phone', 'software', 'file', 'other']) {
      expect(screen.getByTestId(`task-scene-filter-${category}`)).toBeInTheDocument()
    }
    expect(screen.getByTestId('task-scene-filter-terminal')).toHaveAttribute('data-scene-count', '1')
    expect(screen.getByTestId('task-scene-filter-desktop')).toHaveAttribute('data-scene-count', '0')
    expect(screen.getByTestId('task-scene-filter-phone')).toHaveAttribute('data-scene-count', '0')
    expect(screen.getByTestId('task-scene-filter-software')).toHaveAttribute('data-scene-count', '0')
  })

  it('空任务展开历史也渲染 7 类筛选条（计数全 0），不隐藏筛选面', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({ messagesByPipeline: { 'task-8': [] } })
    render(<TaskScenePanel taskId="task-8" />)
    await expandHistory(user)
    expect(screen.getByTestId('task-scene-empty')).toBeInTheDocument()
    expect(screen.getByTestId('task-scene-filters')).toBeInTheDocument()
    expect(screen.getByTestId('task-scene-filter-desktop')).toHaveAttribute('data-scene-count', '0')
  })

  it('设备/软件条目按类别派生并可单独筛出（desktop_capture / desktop_launch / phone_control）', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        'task-9': [
          messageWith([
            toolCall({ callId: 'd1', name: 'desktop_capture', args: { window: 'Notepad' } }),
            toolCall({ callId: 's1', name: 'desktop_launch', args: { app: 'notepad' } }),
            toolCall({ callId: 'p1', name: 'phone_control', args: { action: 'screenshot' } }),
            toolCall({ callId: 'b1', name: 'bash', args: { command: 'ls' } }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="task-9" />)
    await expandAllGroups(user)

    const kinds = screen.getAllByTestId('scene-row').map((r) => r.getAttribute('data-scene-kind'))
    expect(kinds).toEqual(['desktop', 'software', 'phone', 'terminal'])
    expect(screen.getByTestId('task-scene-filter-desktop')).toHaveAttribute('data-scene-count', '1')
    expect(screen.getByTestId('task-scene-filter-software')).toHaveAttribute('data-scene-count', '1')
    expect(screen.getByTestId('task-scene-filter-phone')).toHaveAttribute('data-scene-count', '1')

    await user.click(screen.getByTestId('task-scene-filter-desktop'))
    const filtered = screen.getAllByTestId('scene-row')
    expect(filtered).toHaveLength(1)
    expect(filtered[0]?.getAttribute('data-scene-kind')).toBe('desktop')
  })
})
