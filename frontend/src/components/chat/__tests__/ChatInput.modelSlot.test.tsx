/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ChatInput — 显式模型选择槽位（model_id 受控桥）补测
 *
 * 宿主 → 声明组件的受控注入契约：当前值经 value[field] 注入、声明组件回传
 * onChange 后宿主透出到 onModelIdChange、禁用态经 extra 透传（执行中/禁用时
 * 选择器不可改）。声明由测试桩按 llm_core 声明形态注册（槽位 id=model_id，
 * 单字段 form）。
 */

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { renderWithProviders } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'

const fetchStatesMock = vi.fn()
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/pipelines')>()),
  fetchPipelineStates: (...args: unknown[]) => fetchStatesMock(...args),
}))

const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))

vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: { canDragDrop: false, canPasteImage: false, showAttachmentButton: false },
    capabilities: { supportsAudio: false },
  }),
}))

vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false,
    isRecording: false,
    state: 'idle',
    error: null,
    recordingDuration: 0,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
  }),
}))

vi.mock('../VoiceInputButton', () => ({ VoiceInputButton: () => null }))
vi.mock('../ChatInputActions', () => ({ ChatInputActions: () => null }))

/** 声明组件替身：读宿主注入的 value[model_id]/fallbackModelName，回传 onChange 模拟用户选择 */
function ModelSelectStub(props: Record<string, unknown>) {
  const value = props.value as { model_id?: string } | undefined
  const onChange = props.onChange as ((v: Record<string, unknown>) => void) | undefined
  return (
    <button
      type="button"
      data-testid="model-select-stub"
      data-model={value?.model_id ?? ''}
      data-fallback={(props.fallbackModelName as string) ?? ''}
      disabled={Boolean(props.disabled)}
      onClick={() => onChange?.({ model_id: 'provider/model-x' })}
    >
      选择模型
    </button>
  )
}

function registerModelIdSlot() {
  widgetRegistry.register('test_model_select_stub', ModelSelectStub, {
    name: 'test_model_select_stub',
    supportedSpaces: ['chat', 'workspace'],
  })
  contributionRegistry.loadFromSchema({
    pipelines: [
      {
        id: 'pipeline_llm_core',
        ui_schema: {
          widgets: [{ id: 'model_id', type: 'test_model_select_stub', space: 'chat-input' }],
        },
      },
    ],
  } as never)
}

beforeEach(() => {
  fetchStatesMock.mockImplementation(() => new Promise(() => {}))
  getModelsMock.mockImplementation(() => new Promise(() => {}))
  widgetRegistry.clear()
  contributionRegistry.clear()
  registerModelIdSlot()
})

describe('ChatInput — 显式模型选择槽位', () => {
  it('当前选择经受控 value 注入声明组件；选择后透出 onModelIdChange', () => {
    const onModelIdChange = vi.fn()
    renderWithProviders(
      <ChatInput
        mode="full"
        onSendMessage={() => true}
        modelName="deepseek-v3"
        modelId="provider/model-a"
        onModelIdChange={onModelIdChange}
      />,
    )

    const stub = screen.getByTestId('model-select-stub')
    expect(stub).toHaveAttribute('data-model', 'provider/model-a')

    fireEvent.click(stub)
    expect(onModelIdChange).toHaveBeenCalledWith('provider/model-x')
  })

  it('未选择时注入空串（帧不带 model_id，跟随配置）', () => {
    renderWithProviders(<ChatInput mode="full" onSendMessage={() => true} modelName="deepseek-v3" />)

    expect(screen.getByTestId('model-select-stub')).toHaveAttribute('data-model', '')
  })

  it('禁用态经 extra 透传：输入区禁用时选择器同步禁用', () => {
    renderWithProviders(
      <ChatInput mode="full" disabled onSendMessage={() => true} modelName="deepseek-v3" />,
    )

    expect(screen.getByTestId('model-select-stub')).toBeDisabled()
  })

  it('宿主 modelName 经 overrideProps 透传为 fallbackModelName（新会话默认模型显示面）', () => {
    renderWithProviders(<ChatInput mode="full" onSendMessage={() => true} modelName="deepseek-v3" />)

    expect(screen.getByTestId('model-select-stub')).toHaveAttribute('data-fallback', 'deepseek-v3')
  })
})
