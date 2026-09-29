/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ChatInput × task_mode 声明式任务模式选择器 — 出生语义端到端测试（批 G④）
 *
 * 架构：任务模式选择器 = 通用表单选择器（task_form 插件的 task_mode form select
 * 声明 + FormWidget compact 渲染），无专属组件；模式项 = modes registry 派生
 * （taskModeOptionsFromModes，D1 标签裁定）经宿主桥注入。选择语义（§3.2）：
 * 显示值 = 会话执行选项 modeBinding.mode（出生即定）；选择非当前档 = 经
 * modeSessionBinder 开新管道会话并跳转（同档重选零动作）。
 *
 * 核验：默认会话选择器显示「默认」；registry 派生模式项可选中并触发出生通道
 * （mode 参数）；同档重选零动作；模式会话（快照 modeBinding）触发器显示模式名，
 * 选「默认」= 出生默认会话；registry 不可达降级仅「默认」档。发送链 mode 键
 * 并入逻辑在 modeOptions/modeSessionBinder 车道覆盖。
 *
 * mock 仅外部依赖：管道 state 查询（网络）、会话查询（网络）、语音输入 hook
 * （浏览器媒体 API）、modes registry（网络）、出生通道（会话创建编排）。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { useSessionStore } from '@/stores/sessionStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'
import type { SendMessageParams } from '../types'

const mocks = vi.hoisted(() => ({
  fetchPipelineStates: vi.fn(),
  registryGet: vi.fn(),
  openModeSession: vi.fn(),
}))

vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchPipelineStates: mocks.fetchPipelineStates,
}))
// modes registry（选择器选项数据源，D1 registry 驱动）：默认合成两模式；
// 其余端点拒答（ChatInput 本域零外呼，fail-closed）
vi.mock('@/services/api/client', async (importOriginal) => {
  const original = await importOriginal<Record<string, unknown>>()
  const originalClient = original.apiClient as Record<string, unknown>
  return {
    ...original,
    apiClient: {
      ...originalClient,
      get: (url: string) =>
        url === '/ext/agent_manager/modes'
          ? mocks.registryGet()
          : Promise.reject(new Error(`unexpected get: ${url}`)),
    },
  }
})
// 出生通道（建会话+跳转编排）：mock 记录 mode 参数（WS/store 编排在
// modeSessionBinder 车道覆盖）
vi.mock('@/services/modeSessionBinder', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openModeSession: (...args: unknown[]) => mocks.openModeSession(...args),
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

/** registry 合成载荷（多模式态：writing 带 icon/description，godot 无声明字段） */
const REGISTRY_MODES = {
  modes: [
    {
      mode: 'writing',
      name: '写作模式',
      description: '长文创作与连载',
      pipelines: [],
      presenter: { source: 'none' },
      tool_card: 'native',
      icon: '✍️',
      plugin_id: 'mode_writing',
    },
    {
      mode: 'godot',
      name: 'Godot 模式',
      pipelines: [],
      presenter: { source: 'none' },
      tool_card: 'native',
      plugin_id: 'mode_godot',
    },
  ],
  total: 2,
  errors: [],
}

/** 种子声明：task_mode 选择器本体（仅「默认」兜底档，模式项归 registry 派生） */
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
                      { label: '默认', value: '', icon: '✨' },
                    ],
                  },
                ],
              },
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
function setupInput(onSendMessage: (params: SendMessageParams) => boolean) {
  const utils = renderWithProviders(
    <ChatInput onSendMessage={onSendMessage} modelName={undefined} draftKey="test-tab" />,
  )
  fireEvent.change(screen.getByTestId('chat-input-textarea'), {
    target: { value: '修复登录超时问题' },
  })
  return utils
}

beforeEach(() => {
  vi.clearAllMocks()
  initializeWidgets()
  mocks.fetchPipelineStates.mockResolvedValue({})
  mocks.registryGet.mockResolvedValue({ data: REGISTRY_MODES })
  mocks.openModeSession.mockResolvedValue({ sessionId: 'th-new' })
  seedTaskModeDeclarations()
  localStorage.clear()
  useSessionStore.setState({ activeSessionId: null })
})

describe('ChatInput — 任务模式选择器（出生语义，registry 派生选项）', () => {
  it('默认会话 → 选择器显示「默认」；发送不带 mode 键', async () => {
    const onSendMessage = vi.fn(() => true)
    setupInput(onSendMessage)

    const trigger = await screen.findByTestId('compact-select-trigger')
    expect(trigger).toHaveTextContent('默认')
    expect(screen.getByRole('button', { name: '任务模式：默认' })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('chat-send-button'))
    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1))
    expect((onSendMessage.mock.calls[0][0] as SendMessageParams).mode).toBeUndefined()
    // 默认会话选「默认」（同档重选）→ 零出生动作
    openDropdown(trigger)
    fireEvent.click(screen.getByRole('menuitem', { name: /默认/ }))
    expect(mocks.openModeSession).not.toHaveBeenCalled()
  })

  it('选「写作模式」→ 触发出生通道（mode=writing）', async () => {
    setupInput(vi.fn(() => true))

    const trigger = await screen.findByTestId('compact-select-trigger')
    openDropdown(trigger)
    const item = await screen.findByRole('menuitem', { name: /写作模式/ })
    fireEvent.click(item)

    expect(mocks.openModeSession).toHaveBeenCalledWith('writing')
  })

  it('registry 派生项含图标与描述（icon=decl.icon、description=decl.description）', async () => {
    setupInput(vi.fn(() => true))

    openDropdown(await screen.findByTestId('compact-select-trigger'))
    const item = await screen.findByRole('menuitem', { name: /写作模式/ })
    expect(item).toHaveTextContent('✍️')
    expect(item).toHaveTextContent('长文创作与连载')
  })

  it('模式会话（快照 modeBinding）→ 触发器显示模式名；选「默认」= 出生默认会话', async () => {
    useSessionStore.setState({ activeSessionId: 'th-mode' })
    localStorage.setItem(
      'session-exec-options:th-mode',
      JSON.stringify({
        values: {},
        modeBinding: { mode: 'writing', pipelineConfigId: 'writing' },
      }),
    )
    setupInput(vi.fn(() => true))

    const trigger = await screen.findByTestId('compact-select-trigger')
    await waitFor(() => expect(trigger).toHaveTextContent('写作模式'))
    // 同档（writing）重选零动作
    openDropdown(trigger)
    fireEvent.click(screen.getByRole('menuitem', { name: /写作模式/ }))
    expect(mocks.openModeSession).not.toHaveBeenCalled()
    // 选「默认」→ 出生默认会话（mode 空串）
    openDropdown(trigger)
    fireEvent.click(screen.getByRole('menuitem', { name: /默认/ }))
    expect(mocks.openModeSession).toHaveBeenCalledWith('')
  })

  it('registry 不可达 → 降级仅「默认」档（不报错阻断，菜单无模式项）', async () => {
    mocks.registryGet.mockRejectedValue(new Error('registry down'))
    setupInput(vi.fn(() => true))

    const trigger = await screen.findByTestId('compact-select-trigger')
    expect(trigger).toHaveTextContent('默认')
    openDropdown(trigger)
    expect(screen.queryByRole('menuitem', { name: /写作模式/ })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /默认/ })).toBeInTheDocument()
  })
})
