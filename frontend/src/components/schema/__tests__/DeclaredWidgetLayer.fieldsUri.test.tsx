// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * DeclaredWidgetLayer fieldsUri 数据源解析测试（声明自持数据源，渲染容器零业务知识）：
 * - 渲染前 GET 拉取 {fields} 并入声明 props（fieldsUri 键摘除，组件拿到纯 fields）；
 * - uri 中 {key} 占位符以 contextVars 替换（渲染上下文，如 {model}=当前标签模型）；
 * - fields 空 / 拉取失败 → 不渲染（选择器隐藏语义在声明层）；
 * - 受控桥 overrideProps 与 fields 共存（宿主值通道 + 声明选项面）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DeclaredWidgetLayer } from '../DeclaredWidgetLayer'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { apiClient } from '@/services/api/client'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

const FIELDS = [
  {
    name: 'strength',
    type: 'select',
    options: [
      { value: '{"reasoning_effort":"max"}', label: 'reasoning_effort=max' },
      { value: '{"thinking":{"type":"disabled"}}', label: 'thinking={"type":"disabled"}' },
    ],
  },
]

/** 捕获最终注入组件的 props（验证 fields 合入与 fieldsUri 摘除） */
let lastProps: Record<string, unknown> | null = null
const Probe = (props: Record<string, unknown>) => {
  lastProps = props
  const fields = props.fields as Array<{ options?: Array<{ label?: string }> }> | undefined
  return (
    <div data-testid="probe">
      {(fields?.[0]?.options ?? []).map((o) => (
        <span key={o.label} data-testid="probe-option">
          {o.label}
        </span>
      ))}
    </div>
  )
}

function registerProbe() {
  widgetRegistry.register('test_fields_probe', Probe, { name: 'test_fields_probe' })
  contributionRegistry.loadFromSchema({
    pipelines: [
      {
        id: 'pipeline_llm_core',
        ui_schema: {
          widgets: [
            {
              id: 'thinking_strength',
              type: 'test_fields_probe',
              space: 'chat-input',
              props: {
                fieldsUri: '/ext/llm_service/config/llm/thinking-levels?model={model}',
                title: '思考强度',
              },
            },
          ],
        },
      },
    ],
  } as never)
}

describe('DeclaredWidgetLayer — fieldsUri 声明数据源', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    widgetRegistry.clear()
    contributionRegistry.clear()
    lastProps = null
  })
  afterEach(() => {
    widgetRegistry.clear()
    contributionRegistry.clear()
  })

  it('拉取 {fields} 合入声明 props，{model} 占位符以 contextVars 替换', async () => {
    registerProbe()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { fields: FIELDS } })
    render(
      <DeclaredWidgetLayer
        space="chat-input"
        contextVars={{ model: 'glm-5.3' }}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('probe-option').length).toBe(2))
    expect(apiClient.get).toHaveBeenCalledWith(
      '/ext/llm_service/config/llm/thinking-levels?model=glm-5.3',
    )
    expect(lastProps?.fields).toEqual(FIELDS)
    // fieldsUri 已消费摘除，目标组件不感知 datasource 形态
    expect(lastProps?.fieldsUri).toBeUndefined()
    expect(lastProps?.title).toBe('思考强度')
  })

  it('fields 空 → 不渲染（模型未配置参数组，选择器隐藏）', async () => {
    registerProbe()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { fields: [] } })
    render(<DeclaredWidgetLayer space="chat-input" contextVars={{ model: 'm' }} />)
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    expect(screen.queryByTestId('probe')).toBeNull()
  })

  it('拉取失败 → 不渲染（渲染容器不因数据源故障阻塞）', async () => {
    registerProbe()
    vi.mocked(apiClient.get).mockRejectedValue(new Error('网络异常'))
    render(<DeclaredWidgetLayer space="chat-input" contextVars={{ model: 'm' }} />)
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    expect(screen.queryByTestId('probe')).toBeNull()
  })

  it('宿主 overrideProps（受控桥值通道）与声明 fields 共存', async () => {
    registerProbe()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { fields: FIELDS } })
    render(
      <DeclaredWidgetLayer
        space="chat-input"
        contextVars={{ model: 'm' }}
        overrideProps={() => ({ value: { strength: '{"reasoning_effort":"max"}' }, onChange: vi.fn() })}
      />,
    )
    await waitFor(() => expect(screen.getAllByTestId('probe-option').length).toBe(2))
    expect(lastProps?.value).toEqual({ strength: '{"reasoning_effort":"max"}' })
    expect(lastProps?.fields).toEqual(FIELDS)
  })
})
