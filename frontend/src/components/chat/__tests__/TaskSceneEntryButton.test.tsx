/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */

/**
 * 右上角「任务现场」入口契约（2026-11-17 用户裁定：入口放右上角与待办并列）。
 *
 * 断行为（三条不可省）：
 * - 未加载过该任务消息 → 先按需拉取再开签（否则现场页签是空页）
 * - 已加载 → 不重复拉取（区分度用例：同一入口两种前置状态走不同路径）
 * - 无任务坐标 → 按钮置灰、title 说明、点击不拉取也不开签
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { TaskSceneEntryButton, openTaskScene } from '../TaskSceneEntryButton'
import type { Message } from '@/types/models'

const sceneTab = vi.hoisted(() => ({
  opened: [] as Array<[string, string | undefined, string | undefined]>,
}))

vi.mock('@/components/workspace/taskScene/taskSceneTab', () => ({
  openTaskSceneTab: (taskId: string, title?: string, ownerSessionId?: string) => {
    sceneTab.opened.push([taskId, title, ownerSessionId])
  },
}))

const loadCalls = vi.hoisted(() => [] as Array<{ pipelineId: string; threadId: string }>)

function seedLoad(result: { ok: boolean } = { ok: true }): void {
  usePipelineMessageStore.setState({
    loadPipelineMessages: (async (
      pipelineId: string,
      options: { threadId: string },
    ) => {
      loadCalls.push({ pipelineId, threadId: options.threadId })
      return result
    }) as unknown as ReturnType<typeof usePipelineMessageStore.getState>['loadPipelineMessages'],
  })
}

describe('TaskSceneEntryButton — 右上角入口与按需加载', () => {
  beforeEach(() => {
    sceneTab.opened.length = 0
    loadCalls.length = 0
    usePipelineMessageStore.setState({ messagesByPipeline: {}, pipelines: {} })
    useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })
    seedLoad()
  })

  it('消息未加载：先按需拉取该任务消息，再开现场页签', async () => {
    await openTaskScene('task-a', '任务A', 'sess-1')
    expect(loadCalls).toEqual([{ pipelineId: 'task-a', threadId: 'sess-1' }])
    expect(sceneTab.opened).toEqual([['task-a', '任务A', 'sess-1']])
  })

  it('消息已加载：不重复拉取，直接开签（与未加载用例同契约不同路径）', async () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: { 'task-b': [{ id: 'm1' } as Message] },
    })
    await openTaskScene('task-b', '任务B')
    expect(loadCalls).toEqual([])
    expect(sceneTab.opened).toEqual([['task-b', '任务B', undefined]])
  })

  it('拉取失败：仍开签（页签自带空态），但不静默——写入失败通知', async () => {
    seedLoad({ ok: false })
    await openTaskScene('task-c', '任务C', 'sess-2')
    expect(sceneTab.opened).toHaveLength(1)
    const notices = useNotificationStore.getState().notifications
    expect(notices).toHaveLength(1)
    expect(notices[0].title).toBe('任务现场消息拉取失败')
    expect(notices[0].category).toBe('alert')
  })

  it('无任务坐标：按钮置灰、title 说明，点击既不拉取也不开签', async () => {
    render(<TaskSceneEntryButton sessionId="sess-1" />)
    const btn = screen.getByTestId('chat-task-scene-btn')
    expect(btn).toBeDisabled()
    expect(btn.getAttribute('title')).toContain('未定位到任务')
    await userEvent.click(btn)
    expect(loadCalls).toEqual([])
    expect(sceneTab.opened).toEqual([])
  })

  it('有任务坐标：点击 → 拉取 + 开签（坐标与会话原样透传）', async () => {
    render(<TaskSceneEntryButton taskId="task-d" title="任务D" sessionId="sess-9" />)
    const btn = screen.getByTestId('chat-task-scene-btn')
    expect(btn).not.toBeDisabled()
    await userEvent.click(btn)
    await waitFor(() => expect(sceneTab.opened).toEqual([['task-d', '任务D', 'sess-9']]))
    expect(loadCalls).toEqual([{ pipelineId: 'task-d', threadId: 'sess-9' }])
  })

  it('纯空白坐标视同无任务（不建无坐标的现场页签）', async () => {
    render(<TaskSceneEntryButton taskId="   " />)
    const btn = screen.getByTestId('chat-task-scene-btn')
    expect(btn).toBeDisabled()
    await userEvent.click(btn)
    expect(sceneTab.opened).toEqual([])
  })
})
