/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 设备与软件操作条目测试（现场面板「活动历史」区）。
 *
 * 判据（沿用原能力，路径改为新结构真实交互）：
 * 1. 桌面 Click 行含 label/坐标；手机 click 含 (x,y)；手机 key 含 keycode 语义；App 含应用名
 * 2. 手机 status='blocked' 显示「已拦截（交还用户）」，与失败（error）区分
 * 3. 桌面 Snapshot 行可展开看到元素树文本
 * 4. 非设备类工具（file_read）不误标成设备类别
 */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { TaskScenePanel } from '@/components/workspace/taskScene/TaskScenePanel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { expandAllGroups } from './sceneTestHelpers'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'
import type { Message } from '@/types/models'

function msg(parts: MessagePart[]): Message {
  return {
    id: 'm1',
    sessionId: 's',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: '2026-11-16T10:00:00.000Z',
    parts,
  }
}

function part(p: Partial<ToolCallPart>): MessagePart {
  return {
    type: 'tool_call',
    callId: `call-${p.name ?? 'x'}-${Math.random().toString(36).slice(2, 6)}`,
    name: 'bash',
    args: {},
    state: 'done',
    ...p,
  } as MessagePart
}

describe('任务现场 — 设备与软件操作条目', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      initialLoadingByPipeline: {},
    })
  })

  it('桌面 Click 行显示元素 label；无 label 时显示坐标（两组有区分度输入）', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        't-desktop': [
          msg([
            part({ name: 'Click', args: { label: '[12] 保存按钮' } }),
            part({ name: 'Click', args: { x: 640, y: 480 } }),
            part({ name: 'Type', args: { text: '报告标题' } }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="t-desktop" />)
    await expandAllGroups(user)

    const rows = screen.getAllByTestId('scene-row')
    expect(rows).toHaveLength(3)
    for (const row of rows) expect(row.getAttribute('data-scene-kind')).toBe('desktop')
    expect(within(rows[0] as HTMLElement).getByText('点击 [12] 保存按钮')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('点击 (640, 480)')).toBeInTheDocument()
    expect(within(rows[2] as HTMLElement).getByText('输入 "报告标题"')).toBeInTheDocument()
  })

  it('手机 click 显示坐标；key 显示 keycode 语义；input 显示文本；App 显示应用名', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        't-phone': [
          msg([
            part({ name: 'phone_control', args: { action: 'click', x: 120, y: 340 } }),
            part({ name: 'phone_control', args: { action: 'key', keycode: 4 } }),
            part({ name: 'phone_control', args: { action: 'input', text: '你好' } }),
            part({ name: 'App', args: { name: '记事本' } }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="t-phone" />)
    await expandAllGroups(user)

    const rows = screen.getAllByTestId('scene-row')
    expect(rows.map((r) => r.getAttribute('data-scene-kind'))).toEqual([
      'phone',
      'phone',
      'phone',
      'software',
    ])
    expect(within(rows[0] as HTMLElement).getByText('点击 (120, 340)')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('返回(4)')).toBeInTheDocument()
    expect(within(rows[2] as HTMLElement).getByText('输入 "你好"')).toBeInTheDocument()
    expect(within(rows[3] as HTMLElement).getByText('启动 记事本')).toBeInTheDocument()
  })

  it('手机安全闸拦截（blocked）显示「已拦截（交还用户）」，与失败状态区分', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        't-blocked': [
          msg([
            part({
              name: 'phone_control',
              args: { action: 'click', x: 10, y: 20 },
              state: 'done',
              resultData: { status: 'blocked', message: '命中安全闸，交还用户' },
            }),
            part({
              name: 'phone_control',
              args: { action: 'click', x: 30, y: 40 },
              state: 'error',
              error: '设备连接失败',
            }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="t-blocked" />)
    await expandAllGroups(user)

    const rows = screen.getAllByTestId('scene-row')
    expect(within(rows[0] as HTMLElement).getByTestId('scene-row-blocked')).toHaveTextContent(
      '已拦截（交还用户）',
    )
    // 失败行不误标拦截，走失败状态表达
    expect(within(rows[1] as HTMLElement).queryByTestId('scene-row-blocked')).toBeNull()
    expect(within(rows[1] as HTMLElement).getByText('失败')).toBeInTheDocument()
  })

  it('桌面 Snapshot 展开可见元素树文本', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        't-snap': [
          msg([
            part({
              name: 'Snapshot',
              args: {},
              result: { output: { text: '窗口「记事本」\n [1] 菜单栏\n [12] 保存按钮' } },
            }),
          ]),
        ],
      },
    })
    render(<TaskScenePanel taskId="t-snap" />)
    await expandAllGroups(user)

    const row = screen.getByTestId('scene-row')
    expect(row.getAttribute('data-scene-kind')).toBe('desktop')
    await user.click(within(row).getByRole('button'))
    const detail = screen.getByTestId('scene-row-detail')
    expect(detail.textContent).toContain('保存按钮')
  })

  it('区分度：file_read 不误标成设备类别', async () => {
    const user = userEvent.setup()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        't-file': [msg([part({ name: 'file_read', args: { path: 'a.md' }, result: { content: 'x' } })])],
      },
    })
    render(<TaskScenePanel taskId="t-file" />)
    await expandAllGroups(user)

    const row = screen.getByTestId('scene-row')
    expect(row.getAttribute('data-scene-kind')).toBe('file')
    expect(row.getAttribute('data-scene-kind')).not.toBe('desktop')
    expect(row.getAttribute('data-scene-kind')).not.toBe('phone')
    expect(row.getAttribute('data-scene-kind')).not.toBe('software')
  })
})
