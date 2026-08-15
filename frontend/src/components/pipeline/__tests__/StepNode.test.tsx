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
})
