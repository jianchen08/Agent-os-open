/** @feature R10 双端优化轮 | @ci: frontend-test */
/**
 * ChatInput 输入行 chips 形态回归锁（R10③ 精简，2026-10-14 用户裁定）
 *
 * 裁定前：输入行三 chips = 任务模式（task_form 声明，会话级选择）+ 权限模式
 * （security_check，每消息级）+ 思考强度（llm_core，每消息级）。
 * 裁定后：
 * - 任务/助理模式 chip 从输入行收拢（会话级选择——出生即定，模式会话经侧栏
 *   模式面板等选择处出生；选项过滤走 taskModeOptionsFromModes 的
 *   foreground_selectable 声明通道，见 modeOptions.test.ts）
 * - 每消息级 chips（权限模式/思考强度）保留
 *
 * mock 仅外部依赖：管道 state 查询（网络）、会话查询（网络）、语音输入 hook
 * （浏览器媒体 API）、modes registry（网络）。
 */

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { useSessionStore } from '@/stores/sessionStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'

const mocks = vi.hoisted(() => ({
  fetchPipelineStates: vi.fn(),
  registryGet: vi.fn(),
}))

vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchPipelineStates: mocks.fetchPipelineStates,
}))
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

/** 种子声明：task_mode（R10③ 前的输入行声明）+ 权限模式 + 思考强度同款形态 */
function seedChatInputDeclarations(): void {
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
                    options: [{ label: '默认', value: '', icon: '✨' }],
                  },
                ],
              },
            },
          ],
        },
      },
      {
        plugin_id: 'security_check',
        plugin_name: 'SecurityCheck',
        ui_schema: {
          widgets: [
            {
              id: 'permission_mode_selector',
              type: 'form',
              space: 'chat-input',
              props: {
                title: '权限模式',
                endpoint: '/ext/pipeline_security_check/permission_mode',
                fields: [
                  {
                    name: 'mode',
                    type: 'select',
                    default: 'default',
                    options: [
                      { label: '默认（命中规则才确认）', value: 'default' },
                      { label: '旁路（跳过审批）', value: 'bypass' },
                    ],
                  },
                ],
              },
            },
          ],
        },
      },
      {
        plugin_id: 'llm_core',
        plugin_name: 'LlmCore',
        ui_schema: {
          widgets: [
            {
              id: 'thinking_strength',
              type: 'form',
              space: 'chat-input',
              props: {
                title: '思考强度',
                icon: 'brain',
                fields: [
                  {
                    name: 'strength',
                    type: 'select',
                    default: 'medium',
                    options: [
                      { label: '关闭', value: 'off' },
                      { label: '低', value: 'low' },
                      { label: '中', value: 'medium' },
                      { label: '高', value: 'high' },
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

beforeEach(() => {
  vi.clearAllMocks()
  initializeWidgets()
  mocks.fetchPipelineStates.mockResolvedValue({})
  mocks.registryGet.mockResolvedValue({
    data: { modes: [], total: 0, errors: [] },
  })
  seedChatInputDeclarations()
  localStorage.clear()
  useSessionStore.setState({ activeSessionId: null })
})

describe('ChatInput — 输入行 chips 形态（R10③）', () => {
  it('任务模式 chip 不再钉在输入行（声明在位也不渲染——会话级选择收拢到模式选择处）', async () => {
    renderWithProviders(
      <ChatInput
        onSendMessage={vi.fn(() => true)}
        modelName={undefined}
        draftKey="r10-tab"
        enableThinkingMode
      />,
    )
    await screen.findByTestId('chat-input-textarea')
    expect(screen.queryByRole('button', { name: /任务模式/ })).not.toBeInTheDocument()
  })

  it('每消息级 chips 保留：权限模式与思考强度照常渲染', async () => {
    renderWithProviders(
      <ChatInput
        onSendMessage={vi.fn(() => true)}
        modelName={undefined}
        draftKey="r10-tab"
        enableThinkingMode
      />,
    )
    await screen.findByTestId('chat-input-textarea')
    expect(screen.getByRole('button', { name: /权限模式/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /思考强度/ })).toBeInTheDocument()
  })

  it('权限 chip 可见文案去括注（「默认（命中规则才确认）」→「默认」），可访问名持全称', async () => {
    renderWithProviders(
      <ChatInput
        onSendMessage={vi.fn(() => true)}
        modelName={undefined}
        draftKey="r10-tab"
        enableThinkingMode
      />,
    )
    const trigger = await screen.findByRole('button', { name: '权限模式：默认（命中规则才确认）' })
    // 可见文本两档（窄屏收口 + 桌面去括注）都不含括注原文
    expect(trigger).toHaveTextContent('默认')
    expect(trigger.textContent).not.toContain('命中规则')
    // 菜单项恒为原文（全称不丢）
    fireEvent.pointerDown(trigger)
    fireEvent.pointerUp(trigger)
    fireEvent.click(trigger)
    expect(await screen.findByRole('menuitem', { name: '默认（命中规则才确认）' })).toBeInTheDocument()
  })
})
