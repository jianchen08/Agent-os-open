/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * StepNode 覆盖缺口补测（与 StepNode.test.tsx 互补，不重复）
 *
 * 覆盖契约（断言 ops 调用路径与用户可见交互）：
 * - 头部：step id 编辑提交 ops.set([...path,'id'])；详情收起/展开切换
 * - 头部排序/删除：上移/下移/删除 step 提交 ops.move/ops.remove，下标 0 的上移按钮
 *   与末位的下移按钮禁用
 * - 畸形 steps 条目（缺 name）：降级占位并支持移除/上移/下移（ops 路径带正确下标）
 * - 插件 chip 配置深链：有 configFiles 的插件显示「配置 X」按钮，点击经
 *   workspacePanelOpener 打开设置中枢页签（断言打开参数的 id 与 props）
 * - context 键值清空 → ops.remove([...stepPath,'context'])（回传 undefined 分支）
 * - loop_config：启用勾选、最大迭代输入（数字解析与空串保留）
 * - hooks 只读展示与 next 路由编辑区渲染
 *
 * 测试策略：ops 用录制桩（断言路径），catalog 用真实数据结构，渲染走
 * renderWithProviders（StepNode 依赖 workspacePanelOpener 的插件配置深链）。
 */

import { fireEvent, screen, within } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { StepNode } from '../StepNode'
import type { PipelineEditorOps, PipelineStepV2 } from '@/services/pipeline/model'
import type * as openerMod from '@/services/workspacePanelOpener'

vi.mock('@/services/workspacePanelOpener', async (importOriginal) => {
  const actual = await importOriginal<typeof openerMod>()
  return { ...actual, openWorkspacePanel: vi.fn(), openWorkspacePanelByPath: vi.fn() }
})

const { openWorkspacePanel } = await import('@/services/workspacePanelOpener')

function makeOps() {
  const calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }> = []
  const ops: PipelineEditorOps = {
    set: vi.fn((...args: unknown[]) => calls.push({ op: 'set', args })),
    remove: vi.fn((...args: unknown[]) => calls.push({ op: 'remove', args })),
    insert: vi.fn((...args: unknown[]) => calls.push({ op: 'insert', args })),
    move: vi.fn((...args: unknown[]) => calls.push({ op: 'move', args })),
  }
  return { ops, calls }
}

const catalog = [
  {
    id: 'pipeline_llm_core',
    name: 'LLM Core',
    role: 'core',
    hostType: 'in_process',
    version: '1.0.0',
    enabled: true,
    configFiles: [{ id: 'models', label: '模型', path: 'llm_models.yaml' }],
  },
] as never

const STEP_PATH = ['loop_bodies', 1, 'steps', 0]

function renderStep(
  step: PipelineStepV2,
  ops: PipelineEditorOps,
  extra: { stepIndex?: number; totalSteps?: number } = {},
) {
  return renderWithProviders(
    <StepNode
      step={step}
      stepPath={STEP_PATH}
      stepIndex={extra.stepIndex ?? 0}
      totalSteps={extra.totalSteps ?? 1}
      ops={ops}
      catalog={catalog}
      knownStepIds={['collect']}
      knownPhaseIds={['body_1']}
      bodyStepIds={[]}
    />,
  )
}

describe('StepNode — 头部编辑与排序', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('step id 输入提交 ops.set 到 id 路径', () => {
    const { ops, calls } = makeOps()
    renderStep({ id: 'old', steps: [] } as unknown as PipelineStepV2, ops)

    fireEvent.change(screen.getByLabelText('step id'), { target: { value: 'renamed' } })
    expect(calls).toContainEqual({
      op: 'set',
      args: [[...STEP_PATH, 'id'], 'renamed'],
    })
  })

  it('详情默认展开，点击「收起」后隐藏 context 等详情区，再点「展开」恢复', () => {
    const { ops } = makeOps()
    renderStep({ id: 's1', steps: [] } as unknown as PipelineStepV2, ops)
    expect(screen.getByText(/context（merge 进 state）/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByText(/context（merge 进 state）/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(screen.getByText(/context（merge 进 state）/)).toBeInTheDocument()
  })

  it('上移/下移/删除 step 提交对应 ops 与父数组路径与下标', () => {
    const { ops, calls } = makeOps()
    renderStep({ id: 's1', steps: [] } as unknown as PipelineStepV2, ops, {
      stepIndex: 1,
      totalSteps: 3,
    })

    fireEvent.click(screen.getByLabelText('step s1 上移'))
    fireEvent.click(screen.getByLabelText('step s1 下移'))
    fireEvent.click(screen.getByLabelText('删除 step s1'))

    expect(calls).toContainEqual({ op: 'move', args: [['loop_bodies', 1, 'steps'], 1, -1] })
    expect(calls).toContainEqual({ op: 'move', args: [['loop_bodies', 1, 'steps'], 1, 1] })
    expect(calls).toContainEqual({ op: 'remove', args: [STEP_PATH] })
  })

  it('首个 step 的上移与末个 step 的下移按钮禁用', () => {
    const { ops } = makeOps()
    const { unmount } = renderStep({ id: 'first', steps: [] } as unknown as PipelineStepV2, ops, {
      stepIndex: 0,
      totalSteps: 2,
    })
    expect(screen.getByLabelText('step first 上移')).toBeDisabled()
    expect(screen.getByLabelText('step first 下移')).toBeEnabled()
    unmount()

    renderStep({ id: 'last', steps: [] } as unknown as PipelineStepV2, ops, {
      stepIndex: 1,
      totalSteps: 2,
    })
    expect(screen.getByLabelText('step last 下移')).toBeDisabled()
    expect(screen.getByLabelText('step last 上移')).toBeEnabled()
  })
})

describe('StepNode — 引用 chip 与配置深链', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('有 configFiles 的插件 chip 提供「配置」按钮，点击打开设置中枢页签', () => {
    const { ops } = makeOps()
    const step = {
      id: 's1',
      steps: [{ name: 'pipeline_llm_core' }],
    } as unknown as PipelineStepV2
    renderStep(step, ops)

    fireEvent.click(screen.getByLabelText('配置 pipeline_llm_core'))
    expect(openWorkspacePanel).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'ws-plugin-config-pipeline_llm_core-models',
        component: 'settings_hub',
        moduleId: '__panel_settings__',
        props: { initialActive: 'plugin:pipeline_llm_core:models' },
      }),
    )
  })

  it('无 configFiles 的插件 chip 不显示配置按钮', () => {
    const { ops } = makeOps()
    const step = {
      id: 's1',
      steps: [{ name: 'pipeline_step_only' }],
    } as unknown as PipelineStepV2
    renderStep(step, ops)
    expect(screen.queryByLabelText('配置 pipeline_step_only')).toBeNull()
  })

  it('畸形条目（缺 name）降级占位并支持移除/上移/下移（下标对齐原数组）', () => {
    const { ops, calls } = makeOps()
    const step = {
      id: 's1',
      steps: ['pipeline_llm_core', { when: 'x' }, 42],
    } as unknown as PipelineStepV2
    renderStep(step, ops)

    // 两个畸形条目（对象缺 name / 非对象标量）各渲染一条降级占位
    const malformed = screen.getAllByTitle(/无法识别的 steps 条目/)
    expect(malformed).toHaveLength(2)
    expect(within(malformed[0]).getByText('条目 #2')).toBeInTheDocument()
    expect(within(malformed[1]).getByText('条目 #3')).toBeInTheDocument()

    fireEvent.click(within(malformed[0]).getByLabelText('移除'))
    fireEvent.click(within(malformed[0]).getByLabelText('上移'))
    fireEvent.click(within(malformed[0]).getByLabelText('下移'))

    const stepsPath = [...STEP_PATH, 'steps']
    expect(calls).toContainEqual({ op: 'remove', args: [[...stepsPath, 1]] })
    expect(calls).toContainEqual({ op: 'move', args: [stepsPath, 1, -1] })
    expect(calls).toContainEqual({ op: 'move', args: [stepsPath, 1, 1] })
  })

  it('chip 的移除/上移/下移按钮提交对应 steps 下标', () => {
    const { ops, calls } = makeOps()
    renderStep({ id: 's1', steps: ['collect', 'pipeline_llm_core'] } as unknown as PipelineStepV2, ops)

    const chips = screen.getByRole('group', { name: '插件组合' })
    const llmChip = within(chips).getByTitle(/pipeline_llm_core/)
    fireEvent.click(within(llmChip).getByLabelText('移除'))
    fireEvent.click(within(llmChip).getByLabelText('上移'))
    fireEvent.click(within(llmChip).getByLabelText('下移'))

    const stepsPath = [...STEP_PATH, 'steps']
    expect(calls).toContainEqual({ op: 'remove', args: [[...stepsPath, 1]] })
    expect(calls).toContainEqual({ op: 'move', args: [stepsPath, 1, -1] })
    expect(calls).toContainEqual({ op: 'move', args: [stepsPath, 1, 1] })
  })

  it('引用计数徽标显示 steps 条目数', () => {
    const { ops } = makeOps()
    renderStep({ id: 's1', steps: ['a', 'b'] } as unknown as PipelineStepV2, ops)
    expect(screen.getByText('2 个引用')).toBeInTheDocument()
  })
})

describe('StepNode — 详情区编辑', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('context 清空最后一个键 → ops.remove context 路径', () => {
    const { ops, calls } = makeOps()
    renderStep(
      { id: 's1', steps: [], context: { only: 'v' } } as unknown as PipelineStepV2,
      ops,
    )

    fireEvent.click(screen.getByRole('button', { name: '删除 only' }))
    expect(calls).toContainEqual({ op: 'remove', args: [[...STEP_PATH, 'context']] })
  })

  it('context 修改值 → ops.set context 路径', () => {
    const { ops, calls } = makeOps()
    renderStep({ id: 's1', steps: [], context: { k: 'v' } } as unknown as PipelineStepV2, ops)

    fireEvent.change(screen.getByLabelText('字符串值'), { target: { value: 'v2' } })
    expect(calls).toContainEqual({ op: 'set', args: [[...STEP_PATH, 'context'], { k: 'v2' }] })
  })

  it('启用循环勾选 → ops.set loop_config.enabled', () => {
    const { ops, calls } = makeOps()
    renderStep({ id: 's1', steps: [] } as unknown as PipelineStepV2, ops)
    fireEvent.click(screen.getByRole('checkbox'))

    expect(calls).toContainEqual({
      op: 'set',
      args: [[...STEP_PATH, 'loop_config', 'enabled'], true],
    })
  })

  it.each([
    ['5', 5],
    ['-1', -1],
  ] as const)('最大迭代输入 %s → ops.set 数值 %s', (typed, expected) => {
    const { ops, calls } = makeOps()
    renderStep({ id: 's1', steps: [] } as unknown as PipelineStepV2, ops)
    fireEvent.change(screen.getByLabelText('step 最大迭代次数'), { target: { value: typed } })

    expect(calls).toContainEqual({
      op: 'set',
      args: [[...STEP_PATH, 'loop_config', 'max_iterations'], expected],
    })
  })

  it('最大迭代清空 → 提交空串（由上层决定删除键）', () => {
    const { ops, calls } = makeOps()
    renderStep(
      { id: 's1', steps: [], loop_config: { enabled: true, max_iterations: 3 } } as unknown as PipelineStepV2,
      ops,
    )
    fireEvent.change(screen.getByLabelText('step 最大迭代次数'), { target: { value: '' } })

    expect(calls).toContainEqual({
      op: 'set',
      args: [[...STEP_PATH, 'loop_config', 'max_iterations'], ''],
    })
  })

  it('loop_config 启用时头部显示循环徽标（-1 显示 ∞）', () => {
    const { ops } = makeOps()
    const { unmount } = renderStep(
      { id: 's1', steps: [], loop_config: { enabled: true, max_iterations: -1 } } as unknown as PipelineStepV2,
      ops,
    )
    expect(screen.getByText('step 循环')).toBeInTheDocument()
    expect(screen.getByText('∞')).toBeInTheDocument()
    unmount()

    renderStep(
      { id: 's1', steps: [], loop_config: { enabled: true, max_iterations: 4 } } as unknown as PipelineStepV2,
      ops,
    )
    expect(screen.getByText('×4')).toBeInTheDocument()
  })

  it('hooks 只读区展示 {on, run} 声明；未声明时给空态提示', () => {
    const { ops } = makeOps()
    const { unmount } = renderStep(
      { id: 's1', steps: [], hooks: [{ on: 'step_enter', run: 'audit_hook' }] } as unknown as PipelineStepV2,
      ops,
    )
    expect(screen.getByText(/hooks（step 级钩子，只读）/)).toBeInTheDocument()
    expect(screen.getByText('step_enter')).toBeInTheDocument()
    expect(screen.getByText('audit_hook')).toBeInTheDocument()
    expect(screen.getByTestId('pipe-hooks-step:s1')).toBeInTheDocument()
    unmount()

    renderStep({ id: 's2', steps: [] } as unknown as PipelineStepV2, ops)
    expect(screen.getByTestId('pipe-hooks-empty-step:s2')).toHaveTextContent('无钩子声明')
  })

  it('next 路由编辑区随详情渲染', () => {
    const { ops } = makeOps()
    renderStep({ id: 's1', steps: [] } as unknown as PipelineStepV2, ops)
    expect(screen.getByText(/next（step 级出口转移/)).toBeInTheDocument()
  })
})
