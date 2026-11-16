// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * DeclaredWidgetLayer fieldsUri 数据源解析测试（声明自持数据源，渲染容器零业务知识）：
 * - 渲染前 GET 拉取 {fields} 并入声明 props（fieldsUri 键摘除，组件拿到纯 fields）；
 * - uri 中 {key} 占位符以 contextVars 替换（渲染上下文，如 {model}=当前标签模型）；
 * - fields 空 / 拉取失败 → 不渲染（选择器隐藏语义在声明层）；
 * - 受控桥 overrideProps 与 fields 共存（宿主值通道 + 声明选项面）；
 * - 装载链全路径：注册表装载晚于挂载（真机时序：bootstrap 先渲染 React，
 *   GrowthLoop 异步拉 schema 装载注册表）→ 渲染层必须跟随装载结果。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
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

/** 仅注册 widget 类型（渲染链另一半；不装载声明——装载时序由各测试自持） */
function registerProbeType() {
  widgetRegistry.register('test_fields_probe', Probe, { name: 'test_fields_probe' })
}

/** 装载 thinking_strength 声明（fieldsUri 数据源形态） */
function loadProbeDeclaration(widgets: Array<Record<string, unknown>> = [
  {
    id: 'thinking_strength',
    type: 'test_fields_probe',
    space: 'chat-input',
    props: {
      fieldsUri: '/ext/llm_service/config/llm/thinking-levels?model={model}',
      title: '思考强度',
    },
  },
]) {
  contributionRegistry.loadFromSchema({
    pipelines: [
      {
        id: 'pipeline_llm_core',
        ui_schema: { widgets },
      },
    ],
  } as never)
}

function registerProbe() {
  registerProbeType()
  loadProbeDeclaration()
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

describe('DeclaredWidgetLayer — 装载链全路径（挂载先于注册表装载，真机回归复现）', () => {
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

  it('空注册表挂载 → 装载后槽位渲染跟随（迟到装载不得永空）', async () => {
    // 真机时序：widget 类型已预注册（bootstrap initializeWidgets），ChatInput
    // 挂载时 GrowthLoop 的 schema 装载尚未完成 → 注册表空
    registerProbeType()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { fields: FIELDS } })
    render(
      <DeclaredWidgetLayer
        space="chat-input"
        slotId="thinking_strength"
        contextVars={{ model: 'glm-5.3' }}
      />,
    )
    expect(screen.queryByTestId('probe')).toBeNull()

    // GrowthLoop 装载注册表（晚于挂载）
    act(() => {
      loadProbeDeclaration()
    })

    await waitFor(() => expect(screen.getByTestId('probe')).toBeInTheDocument())
    expect(apiClient.get).toHaveBeenCalledWith(
      '/ext/llm_service/config/llm/thinking-levels?model=glm-5.3',
    )
  })

  it('附加式渲染同样跟随迟到装载与二次替换装载（schema_updated 路径）', async () => {
    registerProbeType()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { fields: FIELDS } })
    render(<DeclaredWidgetLayer space="chat-input" />)
    expect(screen.queryByTestId('probe')).toBeNull()

    act(() => {
      loadProbeDeclaration([
        { id: 'thinking_strength', type: 'test_fields_probe', space: 'chat-input' },
      ])
    })
    await waitFor(() => expect(screen.getByTestId('probe')).toBeInTheDocument())

    // schema_updated / resync → 幂等重载替换声明集：声明消失即渲染消失
    act(() => {
      loadProbeDeclaration([])
    })
    await waitFor(() => expect(screen.queryByTestId('probe')).toBeNull())
  })
})
