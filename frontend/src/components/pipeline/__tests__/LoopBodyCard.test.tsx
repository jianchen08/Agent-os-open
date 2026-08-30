/**
 * LoopBodyCard 组件测试
 *
 * 覆盖循环体卡片核心功能（G10 文件 DSL 口径）：
 * - 循环徽标按 `while` 判定（有 while = 循环体；无 = 单次执行）
 * - while 编辑：写入 body.while 路径；清空 → 摘掉键（缺省单次执行语义）
 * - run_on_error 徽标与编辑
 * - 体级 next 转移规则经 RouteRulesEditor 渲染与编辑（ops 路径到 body.next）
 *
 * 测试策略：ops 用录制桩（断言调用路径），子组件真实渲染。
 */

import { screen, fireEvent, within } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { LoopBodyCard } from '../LoopBodyCard'
import type { LoopBodyV2, PipelineEditorOps } from '@/services/pipeline/model'

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

const BODY_PATH = ['loop_bodies', 1]

const catalog = [
  {
    id: 'pipeline_result_format',
    name: 'Result Format',
    role: 'output',
    hostType: 'in_process',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
  },
]

function renderBody(body: LoopBodyV2, ops: PipelineEditorOps) {
  return renderWithProviders(
    <LoopBodyCard
      body={body}
      bodyPath={BODY_PATH}
      bodyIndex={1}
      ops={ops}
      catalog={catalog}
      knownStepIds={['prepare', 'core', 'post', 'other_step']}
      knownPhaseIds={['init', 'main', 'exit']}
      knownStepIdSet={new Set(['prepare', 'core', 'post'])}
    />,
  )
}

describe('LoopBodyCard', () => {
  let ops: PipelineEditorOps
  let calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }>

  beforeEach(() => {
    ;({ ops, calls } = makeOps())
  })

  it('有 while 的循环体显示「循环体」徽标（autonomous main 同形）', () => {
    renderBody({
      id: 'main',
      while: 'True',
      steps: [{ id: 'prepare', steps: [] }],
    }, ops)

    expect(screen.getByText('循环体')).toBeInTheDocument()
    expect(screen.queryByText('单次执行')).not.toBeInTheDocument()
    expect(screen.getByLabelText('step id')).toHaveValue('prepare')
  })

  it('无 while 的体显示「单次执行」徽标；run_on_error 徽标', () => {
    renderBody({
      id: 'exit',
      run_on_error: true,
      steps: [],
    }, ops)

    expect(screen.getByText('单次执行')).toBeInTheDocument()
    expect(screen.getByText('错误必经')).toBeInTheDocument()
    expect(screen.queryByText('循环体')).not.toBeInTheDocument()
  })

  it('体设置：while 编辑写入 body.while 路径', () => {
    renderBody({ id: 'main', steps: [] }, ops)

    fireEvent.click(screen.getByRole('button', { name: /体设置/ }))
    const input = screen.getByLabelText('循环体 while 条件')
    fireEvent.change(input, { target: { value: 'state.keep_going == True' } })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...BODY_PATH, 'while'], 'state.keep_going == True'],
    })
  })

  it('体设置：while 清空 → ops.remove 摘掉键（缺省单次执行）', () => {
    renderBody({ id: 'main', while: 'True', steps: [] }, ops)

    fireEvent.click(screen.getByRole('button', { name: /体设置/ }))
    fireEvent.change(screen.getByLabelText('循环体 while 条件'), {
      target: { value: '' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...BODY_PATH, 'while']],
    })
  })

  it('体设置：run_on_error 编辑写入路径', () => {
    renderBody({ id: 'exit', steps: [] }, ops)

    fireEvent.click(screen.getByRole('button', { name: /体设置/ }))
    fireEvent.click(screen.getByLabelText('run_on_error（错误必经）'))
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...BODY_PATH, 'run_on_error'], true],
    })
  })

  it('体级 next 转移规则渲染与编辑（ops 落 body.next 路径）', () => {
    renderBody({
      id: 'main',
      while: 'True',
      steps: [],
      next: [{ when: 'suspended == True', then: 'end' }],
    }, ops)

    fireEvent.click(screen.getByRole('button', { name: /体设置/ }))
    expect(screen.getByText(/next（循环体结束转移/)).toBeInTheDocument()
    expect(screen.getByLabelText('规则 1 when 条件')).toHaveValue('suspended == True')
    expect(screen.getByLabelText('规则 1 then 目标')).toHaveValue('end')

    fireEvent.change(screen.getByLabelText('规则 1 then 目标'), {
      target: { value: 'exit' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...BODY_PATH, 'next', 0, 'then'], 'exit'],
    })
  })

  it('添加 step → ops.insert 追加并去重命名', () => {
    renderBody({ id: 'main', steps: [{ id: 'prepare', steps: [] }] }, ops)

    fireEvent.click(screen.getByRole('button', { name: '添加 step' }))
    expect(calls.at(-1)).toEqual({
      op: 'insert',
      args: [
        [...BODY_PATH, 'steps'],
        1,
        { id: 'new_step', steps: [] },
      ],
    })
  })

  it('子 step 的 next 本地目标集 = 本体 step id（他体 step 不入选项，跨体走体 id）', () => {
    renderBody({
      id: 'main',
      while: 'True',
      steps: [
        { id: 'prepare', steps: [] },
        { id: 'post', steps: [], next: [{ when: 'True', then: 'end' }] },
      ],
    }, ops)

    // 展开第二个 step 的 next 目标下拉：本地 step + 循环体 id（跨体转移）在列，
    // 他体 step（other_step）不出现——内核只接受同体内 step 目标
    const selects = screen.getAllByLabelText('规则 1 then 目标')
    const postSelect = selects.at(-1) as HTMLSelectElement
    const values = Array.from(postSelect.options).map((o) => o.value)
    expect(values).toContain('prepare')
    expect(values).toContain('post')
    expect(values).toContain('loop')
    expect(values).toContain('main')
    expect(values).not.toContain('other_step')
  })

  it('空循环体显示空态提示', () => {
    renderBody({ id: 'main', steps: [] }, ops)
    expect(screen.getByText('空循环体（不会执行任何 step）')).toBeInTheDocument()
  })

  it('hooks 只读展示透传（体级，体设置展开后可见）', () => {
    renderBody({
      id: 'main',
      steps: [],
      hooks: [{ on: 'run_start', run: 'watcher' }],
    }, ops)

    fireEvent.click(screen.getByRole('button', { name: /体设置/ }))
    const region = screen.getByTestId('pipe-hooks-body:main')
    expect(within(region).getByText('run_start')).toBeInTheDocument()
  })
})
