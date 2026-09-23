/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ChatInput × task_mode 声明式任务模式选择器 — 端到端发送测试（通用选择器契约）
 *
 * 架构：任务模式选择器 = 通用表单选择器（task_form 插件的 task_mode form select
 * 声明 + FormWidget compact 渲染 + 宿主受控桥接 chatInputStore），无专属组件；
 * 模式选项由各模式插件 select-option 声明追加（DeclaredWidgetLayer 合并）。
 *
 * 核验：选择器常驻底部工具栏，触发器显示当前档（自动/模式名）；菜单切换后
 * chatInputStore 更新、发送形态随变——带 mode 键/不带键（SendMessageParams
 * 出口）；激活状态按 draftKey 会话保持。execution_context 并入逻辑在
 * modeOptions.withTaskMode 车道覆盖。
 *
 * mock 仅外部依赖：管道 state 查询（网络）、会话查询（网络）、语音输入 hook
 * （浏览器媒体 API）。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatInput } from '../ChatInput'
import type { SendMessageParams } from '../types'
import { useChatInputStore } from '@/stores/chatInputStore'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { renderWithProviders } from '@/test/renderWithProviders'

const mocks = vi.hoisted(() => ({
  fetchPipelineStates: vi.fn(),
}))

vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchPipelineStates: mocks.fetchPipelineStates,
}))
// FormWidget 现消费会话隔离形态（useSessionsQuery）计算权限档显示默认；
// 聊天输入域无会话列表上下文，mock 为空集
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false,
    isRecording: false,
    isTranscribing: false,
    transcript: '',
    recordingDuration: 0,
    state: 'idle',
    mode: 'browser',
    error: null,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
  }),
}))

/** 测试用会话键（ChatInput 状态提升后的 draftKey = tabId/sessionId） */
const TAB_KEY = 'test-tab'

/** 种子声明链：task_mode 选择器本体（仅「自动」兜底档）+ 插件追加的选项声明 */
function seedTaskModeDeclarations(): void {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'task_form',
        plugin_name: 'TaskForm',
        ui_schema: {
          widgets: [
            {
              id: 'task_mode',
              type: 'form',
              space: 'chat-input',
              title: '任务模式',
              props: {
                title: '任务模式',
                icon: 'sparkles',
                fields: [
                  {
                    name: 'mode',
                    type: 'select',
                    label: '任务模式',
                    default: '',
                    options: [
                      { label: '自动', value: '', icon: '✨' },
                    ],
                  },
                ],
              },
            },
          ],
        },
      },
      {
        plugin_id: 'mode_writing',
        plugin_name: 'ModeWriting',
        ui_schema: {
          widgets: [
            {
              id: 'mode_opt_writing',
              type: 'select-option',
              space: 'chat-input',
              order: 10,
              props: { target: 'task_mode', value: 'writing', label: '写作', icon: '✍️' },
            },
          ],
        },
      },
      {
        plugin_id: 'mode_research',
        plugin_name: 'ModeResearch',
        ui_schema: {
          widgets: [
            {
              id: 'mode_opt_research',
              type: 'select-option',
              space: 'chat-input',
              order: 10,
              props: { target: 'task_mode', value: 'research', label: '调研', icon: '🔎' },
            },
          ],
        },
      },
    ],
    plugin_configs: [],
  })
}

/** Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click） */
function openDropdown(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

/** 渲染 ChatInput 并填入正文（发送前置态） */
function setupInput(onSendMessage: (params: SendMessageParams) => boolean, draftKey = TAB_KEY) {
  const utils = renderWithProviders(
    <ChatInput onSendMessage={onSendMessage} modelName={undefined} draftKey={draftKey} />,
  )
  fireEvent.change(screen.getByTestId('chat-input-textarea'), {
    target: { value: '修复登录超时问题' },
  })
  return utils
}

beforeEach(() => {
  initializeWidgets()
  mocks.fetchPipelineStates.mockResolvedValue({})
  seedTaskModeDeclarations()
  useChatInputStore.setState({ taskModes: {} })
})

describe('ChatInput — 任务模式选择器（声明式，底部工具栏）', () => {
  it('自动态 → 选择器渲染且显示「自动」，发送不带 mode 键', async () => {
    const onSendMessage = vi.fn(() => true)
    setupInput(onSendMessage)

    const trigger = screen.getByTestId('compact-select-trigger')
    // 可见文案只留裸值；设置名在可访问名（BUG-6 自标识）
    expect(trigger).toHaveTextContent('自动')
    expect(trigger).not.toHaveTextContent('任务模式')
    expect(screen.getByRole('button', { name: '任务模式：自动' })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('chat-send-button'))
    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1))
    expect((onSendMessage.mock.calls[0][0] as SendMessageParams).mode).toBeUndefined()
  })

  it('外部入口预置激活模式 → 触发器显示「写作」，发送 mode=writing', async () => {
    useChatInputStore.getState().setTaskMode(TAB_KEY, 'writing')
    const onSendMessage = vi.fn(() => true)
    setupInput(onSendMessage)

    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('写作')

    fireEvent.click(screen.getByTestId('chat-send-button'))
    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1))
    expect((onSendMessage.mock.calls[0][0] as SendMessageParams).mode).toBe('writing')
  })

  it('菜单选「调研」→ store 更新，发送 mode=research（select-option 追加选项可选中）', async () => {
    const onSendMessage = vi.fn(() => true)
    setupInput(onSendMessage)

    openDropdown(screen.getByTestId('compact-select-trigger'))
    fireEvent.click(screen.getByRole('menuitem', { name: /调研/ }))
    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('调研')

    fireEvent.click(screen.getByTestId('chat-send-button'))
    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1))
    expect((onSendMessage.mock.calls[0][0] as SendMessageParams).mode).toBe('research')
  })

  it('激活后菜单选「自动」→ 回兜底档，发送不带 mode 键', async () => {
    useChatInputStore.getState().setTaskMode(TAB_KEY, 'writing')
    const onSendMessage = vi.fn(() => true)
    setupInput(onSendMessage)

    openDropdown(screen.getByTestId('compact-select-trigger'))
    fireEvent.click(screen.getByRole('menuitem', { name: /自动/ }))

    fireEvent.click(screen.getByTestId('chat-send-button'))
    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1))
    expect((onSendMessage.mock.calls[0][0] as SendMessageParams).mode).toBeUndefined()
  })

  it('激活状态按会话保持：tab-a 激活、tab-b 自动画，切回恢复', () => {
    useChatInputStore.getState().setTaskMode('tab-a', 'writing')
    const onSendMessage = vi.fn(() => true)

    const first = setupInput(onSendMessage, 'tab-a')
    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('写作')
    first.unmount()

    const second = setupInput(onSendMessage, 'tab-b')
    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('自动')
    second.unmount()

    const back = setupInput(onSendMessage, 'tab-a')
    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('写作')
    back.unmount()
  })
})
