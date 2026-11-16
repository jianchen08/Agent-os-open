/** @feature FP-0.2.四 前端Schema模型选择器 | @ci: frontend-test */
/**
 * ModelSelectWidget 单测：输入框模型选择器的显示/回落契约
 *
 * 数据源三路（前两路为外部依赖，本文件 mock；第三路宿主注入）：
 * - 模型注册表 useLlmConfigQuery（llm.yaml models）→ 下拉选项
 * - 管道 state 行 usePipelineStatesQuery + 活动标签管道 id → 停留值来源
 * - fallbackModelName prop（宿主经 overrideProps 注入）→ 新会话默认模型显示
 *
 * 断行为不断实现：
 * - 选项标签 display_name 优先、缺失回退注册表键
 * - 无显式选择时停留当前实际模型（state 行 llm_model 三级反查：model_name
 *   精确 → 键精确 → 键忽略大小写）
 * - 显式选择优先；已不在注册表的显式值回落 state 停留值（不显示空）
 * - 求值序 = 显式命中 > state 停留值 > fallbackModelName 反查（新会话默认面）；
 *   反查失败/无 fallback → 空态占位（title 或「模型」），fail-soft 不新造 option
 * - 选中回写 {model_id: 注册表键}；缺省 onChange 不崩
 */
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { ModelSelectWidget } from '../ModelSelectWidget'

const seed = vi.hoisted(() => ({
  loaded: true,
  models: {} as Record<string, { model_name: string; display_name: string }>,
  pipelineId: '',
  states: undefined as Record<string, unknown> | undefined,
}))

vi.mock('@/hooks/queries/useLlmQueries', () => ({
  useLlmConfigQuery: () => (seed.loaded ? { data: { models: seed.models } } : { data: undefined }),
}))
vi.mock('@/hooks/useActiveExecutionContext', () => ({
  useActiveTabPipelineId: () => seed.pipelineId,
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineStatesQuery: () => ({ data: seed.states }),
}))

/** 管道 state 行（只带本组件消费的 llm_model 真值） */
function stateRow(pipelineId: string, llmModel?: string): Record<string, unknown> {
  return {
    pipeline_id: pipelineId,
    source: 'memory',
    state: llmModel ? { llm_model: llmModel } : {},
  }
}

function selectRoot(): HTMLElement {
  return screen.getByTestId('model-select')
}

/** 停留显示的选项标签（antd v6：有值时 content 带 title，无值时为占位节点） */
function selectionText(): string {
  return document.querySelector('.ant-select-content')?.getAttribute('title') ?? ''
}

function placeholderText(): string {
  return document.querySelector('.ant-select-placeholder')?.textContent ?? ''
}

/** 展开下拉并返回 dropdown 容器（antd 渲染在 body 末尾） */
async function openDropdown(): Promise<HTMLElement> {
  fireEvent.mouseDown(document.querySelector('.ant-select-content') as Element)
  await waitFor(() => expect(document.querySelector('.ant-select-dropdown')).not.toBeNull())
  return document.querySelector('.ant-select-dropdown') as HTMLElement
}

/** 受控宿主：选择回流到 value，模拟 RJSF 槽位桥（useControlledSlotBridge） */
function Harness(props: { initial?: string; onChange?: (values: Record<string, unknown>) => void }) {
  const [value, setValue] = useState<{ model_id?: string }>({ model_id: props.initial ?? '' })
  return (
    <ModelSelectWidget
      value={value}
      onChange={(next) => {
        setValue(next as { model_id?: string })
        props.onChange?.(next)
      }}
    />
  )
}

beforeEach(() => {
  seed.loaded = true
  seed.models = {}
  seed.pipelineId = ''
  seed.states = undefined
})

afterEach(cleanup)

describe('ModelSelectWidget — 选项面', () => {
  it('选项标签 display_name 优先、缺失回退注册表键', async () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: '' },
    }

    renderWithProviders(<ModelSelectWidget />)
    const dropdown = await openDropdown()

    expect(within(dropdown).getByText('MiniMax M3')).toBeInTheDocument()
    // display_name 空串 → 标签回退注册表键（不是空白项）
    expect(within(dropdown).getByText('deepseek-v3')).toBeInTheDocument()
  })

  it('选中选项回写 {model_id: 注册表键}（标签 ≠ 键，证明回写的是键）', async () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: 'DeepSeek V3' },
    }
    const onChange = vi.fn()

    renderWithProviders(<Harness onChange={onChange} />)
    const dropdown = await openDropdown()
    fireEvent.click(within(dropdown).getByText('DeepSeek V3'))

    expect(onChange).toHaveBeenCalledWith({ model_id: 'deepseek-v3' })
    await waitFor(() => expect(selectionText()).toBe('DeepSeek V3'))
  })

  it('未传 onChange：选择选项不崩（可选回调）', async () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }

    renderWithProviders(<ModelSelectWidget />)
    const dropdown = await openDropdown()
    expect(() => fireEvent.click(within(dropdown).getByText('模型一'))).not.toThrow()
  })

  it('注册表尚未加载（query 无数据）→ 空选项空态，不崩', async () => {
    seed.loaded = false

    renderWithProviders(<ModelSelectWidget />)
    expect(placeholderText()).toBe('模型')
    expect(selectionText()).toBe('')
    await openDropdown()
    expect(document.querySelectorAll('.ant-select-item-option')).toHaveLength(0)
  })

  it('disabled：选择器禁用且按下不展开下拉', () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }

    renderWithProviders(<ModelSelectWidget disabled />)
    expect(selectRoot()).toHaveClass('ant-select-disabled')

    fireEvent.mouseDown(document.querySelector('.ant-select-content') as Element)
    expect(document.querySelector('.ant-select-dropdown')).not.toBeInTheDocument()
  })
})

describe('ModelSelectWidget — 停留值（state 行反查）', () => {
  it('无显式选择：停留 state 行 llm_model 反查到的模型（model_name 精确命中）', () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: 'DeepSeek V3' },
    }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', 'MiniMax-M3') }

    renderWithProviders(<ModelSelectWidget value={{}} />)
    expect(selectionText()).toBe('MiniMax M3')
    expect(placeholderText()).toBe('')
  })

  it('state 行与注册表键形态不一致：键忽略大小写命中', () => {
    seed.models = { 'DeepSeek-V3': { model_name: 'ds-v3', display_name: 'DeepSeek V3' } }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', 'deepseek-v3') }

    renderWithProviders(<ModelSelectWidget value={{}} />)
    expect(selectionText()).toBe('DeepSeek V3')
    expect(placeholderText()).toBe('')
  })

  it('state 行 llm_model 不在注册表 → 空态占位用 title', () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', '未注册模型') }

    renderWithProviders(<ModelSelectWidget value={{}} title="选个模型" />)
    expect(placeholderText()).toBe('选个模型')
    expect(selectionText()).toBe('')
  })

  it('无活动管道 → 不反查 state，空态占位缺省「模型」', () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }
    seed.pipelineId = ''
    seed.states = { p1: stateRow('p1', 'm1') }

    renderWithProviders(<ModelSelectWidget value={{}} />)
    expect(placeholderText()).toBe('模型')
    expect(selectionText()).toBe('')
  })

  it('显式选择已不在注册表 → 回落 state 停留值（用户看不到空选择器）', () => {
    seed.models = { 'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' } }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', 'MiniMax-M3') }

    renderWithProviders(<ModelSelectWidget value={{ model_id: '已下线模型' }} />)
    expect(selectionText()).toBe('MiniMax M3')
  })

  it('显式选择命中注册表 → 显式优先于 state 停留值', () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: 'DeepSeek V3' },
    }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', 'MiniMax-M3') }

    renderWithProviders(<ModelSelectWidget value={{ model_id: 'deepseek-v3' }} />)
    expect(selectionText()).toBe('DeepSeek V3')
  })
})

describe('ModelSelectWidget — fallback 显示（新会话默认模型）', () => {
  it('无显式选择、无管道 state、有 fallback（注册表键形态）→ 显示反查后的模型', () => {
    seed.models = {
      'minimax-m3.1': { model_name: 'MiniMax-M3.1', display_name: 'MiniMax M3.1' },
    }

    renderWithProviders(<ModelSelectWidget value={{}} fallbackModelName="minimax-m3.1" />)
    expect(selectionText()).toBe('MiniMax M3.1')
    expect(placeholderText()).toBe('')
  })

  it('fallback 为 model_name 形态 → 同一三级反查命中（注册表键 ≠ model_name）', () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
    }

    renderWithProviders(<ModelSelectWidget value={{}} fallbackModelName="MiniMax-M3" />)
    expect(selectionText()).toBe('MiniMax M3')
  })

  it('fallback 为空 → 仍显示占位「模型」（完全无数据不发明选项）', () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }

    renderWithProviders(<ModelSelectWidget value={{}} />)
    expect(placeholderText()).toBe('模型')
    expect(selectionText()).toBe('')

    renderWithProviders(<ModelSelectWidget value={{}} fallbackModelName="" />)
    expect(placeholderText()).toBe('模型')
    expect(selectionText()).toBe('')
  })

  it('显式选择优先于 fallback（用户手动选择压过默认模型）', () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: 'DeepSeek V3' },
    }

    renderWithProviders(
      <ModelSelectWidget value={{ model_id: 'deepseek-v3' }} fallbackModelName="MiniMax-M3" />,
    )
    expect(selectionText()).toBe('DeepSeek V3')
  })

  it('管道 state 停留值优先于 fallback（实际运行模型压过配置默认）', () => {
    seed.models = {
      'gw-minimax': { model_name: 'MiniMax-M3', display_name: 'MiniMax M3' },
      'deepseek-v3': { model_name: 'deepseek-v3-chat', display_name: 'DeepSeek V3' },
    }
    seed.pipelineId = 'p1'
    seed.states = { p1: stateRow('p1', 'deepseek-v3-chat') }

    renderWithProviders(<ModelSelectWidget value={{}} fallbackModelName="MiniMax-M3" />)
    expect(selectionText()).toBe('DeepSeek V3')
  })

  it('fallback 名反查注册表失败 → 回退占位（fail-soft，不新造 option）', async () => {
    seed.models = { m1: { model_name: 'm1', display_name: '模型一' } }

    renderWithProviders(<ModelSelectWidget value={{}} fallbackModelName="未注册模型" />)
    expect(placeholderText()).toBe('模型')
    expect(selectionText()).toBe('')

    const dropdown = await openDropdown()
    expect(within(dropdown).getAllByRole('option')).toHaveLength(1)
  })
})
