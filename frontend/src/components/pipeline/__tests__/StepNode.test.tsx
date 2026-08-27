/**
 * StepNode 组件测试
 *
 * 覆盖管道 step 节点卡片核心功能：
 * - 插件组合 chips 渲染与四类引用分类（plugin/step/template/unknown）
 * - chip 移除/上移/下移 → ops 路径正确
 * - 添加插件弹窗（目录选择 → ops.insert）
 * - context 键值编辑 / step loop_config 编辑 → ops 路径正确
 *
 * 测试策略：ops 用录制桩（断言调用路径），UI 组件真实渲染。
 */

import { screen, fireEvent, within } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { StepNode } from '../StepNode'
import type { PipelineEditorOps, PipelineStepV2 } from '@/services/pipeline/model'

/** ops 录制桩：记录调用以断言 path */
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
  {
    id: 'pipeline_tool_core',
    name: 'Tool Core',
    role: 'core',
    hostType: 'in_process',
    version: '1.0.0',
    enabled: false,
    configFiles: [],
  },
]

const STEP_PATH = ['loop_bodies', 1, 'steps', 1]

function renderStep(step: PipelineStepV2, ops: PipelineEditorOps) {
  return renderWithProviders(
    <StepNode
      step={step}
      stepPath={STEP_PATH}
      stepIndex={1}
      totalSteps={3}
      ops={ops}
      catalog={catalog}
      knownStepIds={['prepare', 'core', 'post', 'other_step']}
      knownPhaseIds={['init', 'main', 'exit']}
    />,
  )
}

describe('StepNode', () => {
  let ops: PipelineEditorOps
  let calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }>

  beforeEach(() => {
    ;({ ops, calls } = makeOps())
  })

  it('渲染 step id 与四类引用 chip（plugin/step/template/unknown）', () => {
    renderStep(
      {
        id: 'core',
        steps: [
          'pipeline_llm_core',
          'other_step',
          '{{state.core_plugin}}',
          'mystery_ref',
        ],
      },
      ops,
    )

    expect(screen.getByLabelText('step id')).toHaveValue('core')
    // 插件短名（去 pipeline_ 前缀）+ role 徽标 + step/模板原样
    expect(screen.getByText('llm_core')).toBeInTheDocument()
    expect(screen.getByText('core', { exact: true })).toBeInTheDocument()
    expect(screen.getByText('{{state.core_plugin}}')).toBeInTheDocument()
    expect(screen.getByText('other_step')).toBeInTheDocument()
    expect(screen.getByText('mystery_ref')).toBeInTheDocument()
    expect(screen.getByText('4 个引用')).toBeInTheDocument()
  })

  it('chip 移除/上移/下移调用 ops 且路径正确', () => {
    renderStep(
      { id: 'prepare', steps: ['pipeline_llm_core', 'pipeline_tool_core'] },
      ops,
    )

    const chip = screen.getByText('llm_core').closest('span.group') as HTMLElement
    fireEvent.click(within(chip).getByLabelText('移除'))
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...STEP_PATH, 'steps', 0]],
    })

    fireEvent.click(within(chip).getByLabelText('上移'))
    expect(calls.at(-1)).toEqual({
      op: 'move',
      args: [[...STEP_PATH, 'steps'], 0, -1],
    })

    fireEvent.click(within(chip).getByLabelText('下移'))
    expect(calls.at(-1)).toEqual({
      op: 'move',
      args: [[...STEP_PATH, 'steps'], 0, 1],
    })
  })

  it('添加插件弹窗：目录选择 → ops.insert 追加到组合末尾', async () => {
    renderStep({ id: 'prepare', steps: ['pipeline_tool_core'] }, ops)

    fireEvent.click(screen.getByLabelText('向 step prepare 添加插件'))
    // 目录中 pipeline_tool_core 已在组合（排除置灰），选 pipeline_llm_core
    fireEvent.click(await screen.findByLabelText('添加 pipeline_llm_core'))

    expect(calls.at(-1)).toEqual({
      op: 'insert',
      args: [[...STEP_PATH, 'steps'], 1, 'pipeline_llm_core'],
    })
  })

  it('context 键值编辑：改名与改值落到 step.context 路径', () => {
    renderStep(
      { id: 'prepare', steps: [], context: { agent_id: '{{state.agent_id}}' } },
      ops,
    )

    fireEvent.change(screen.getByLabelText('agent_id 键名'), {
      target: { value: 'agent' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...STEP_PATH, 'context'], { agent: '{{state.agent_id}}' }],
    })

    fireEvent.change(screen.getByLabelText('字符串值'), {
      target: { value: 'main' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...STEP_PATH, 'context'], { agent_id: 'main' }],
    })
  })

  it('step loop_config 编辑：启用循环写入路径', () => {
    renderStep({ id: 'prepare', steps: [] }, ops)

    fireEvent.click(screen.getByLabelText('启用循环'))
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...STEP_PATH, 'loop_config', 'enabled'], true],
    })
  })

  // ── G9 项级 when 门对象条目（{name, when}）回归 ──────────────────────

  it('G9 对象条目（{name, when}）正常渲染为 chip 并带 when 门徽标（崩溃回归）', () => {
    renderStep(
      {
        id: 'core',
        steps: [
          'pipeline_llm_core',
          { name: 'pipeline_tool_cache', when: 'raw_tool_calls != [] and raw_tool_calls != None' },
          '{{state.core_plugin}}',
        ],
      },
      ops,
    )

    expect(screen.getByText('pipeline_tool_cache')).toBeInTheDocument()
    const gate = screen.getByTitle('项级 when 门：raw_tool_calls != [] and raw_tool_calls != None')
    expect(gate).toBeInTheDocument()
    expect(screen.getByText('3 个引用')).toBeInTheDocument()
  })

  it('G9 对象条目的移除 ops 按原始下标操作', () => {
    renderStep(
      { id: 'core', steps: ['pipeline_llm_core', { name: 'pipeline_tool_cache' }] },
      ops,
    )

    const chip = screen.getByText('pipeline_tool_cache').closest('span.group') as HTMLElement
    fireEvent.click(within(chip).getByLabelText('移除'))
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...STEP_PATH, 'steps', 1]],
    })
  })

  it('畸形条目（缺 name）降级展示不崩溃', () => {
    renderStep({ id: 'core', steps: ['pipeline_llm_core', { when: 'True' }, 42] }, ops)

    expect(screen.getAllByTitle(/无法识别的 steps 条目/)).toHaveLength(2)
    expect(screen.getByText('llm_core')).toBeInTheDocument()
  })

  it('step 级 hooks 只读展示（事件 + run 目标）', () => {
    renderStep(
      {
        id: 'core',
        steps: [],
        hooks: [
          { on: 'stream_chunk', run: 'stream_duplicate_check.on_chunk' },
          { on: 'run_start', run: 'watcher' },
        ],
      },
      ops,
    )

    const region = screen.getByTestId('pipe-hooks-step:core')
    expect(within(region).getByText('stream_chunk')).toBeInTheDocument()
    expect(within(region).getByText('stream_duplicate_check.on_chunk')).toBeInTheDocument()
    expect(within(region).getByText('run_start')).toBeInTheDocument()
    expect(within(region).getAllByText('hook')).toHaveLength(2)
  })

  it('无 hooks 声明显示空态', () => {
    renderStep({ id: 'core', steps: [] }, ops)
    expect(screen.getByTestId('pipe-hooks-empty-step:core')).toBeInTheDocument()
  })
})
