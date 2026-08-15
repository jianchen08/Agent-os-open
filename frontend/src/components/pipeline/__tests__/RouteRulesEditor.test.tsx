/**
 * RouteRulesEditor 组件测试
 *
 * 覆盖路由规则编辑器核心功能：
 * - 渲染已有规则（when / next 各形态 / then.set 键值）
 * - next 类型切换与 step/phase 目标选择 → ops 写入正确形态
 * - 添加/删除/移动规则 → ops 路径正确
 * - then.set 清空 → remove（避免写出空 set 对象）
 */

import { screen, fireEvent, render  } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { RouteRulesEditor } from '../RouteRulesEditor'
import type { PipelineEditorOps, RouteRule } from '@/services/pipeline/model'

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

const ARRAY_PATH = ['loop_bodies', 1, 'routes']

function renderRules(rules: RouteRule[] | undefined, ops: PipelineEditorOps) {
  return render(
    <RouteRulesEditor
      rules={rules}
      arrayPath={ARRAY_PATH}
      ops={ops}
      knownStepIds={['prepare', 'core', 'post']}
      knownPhaseIds={['init', 'main', 'exit']}
      label="路由分支"
    />,
  )
}

describe('RouteRulesEditor', () => {
  let ops: PipelineEditorOps
  let calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }>

  beforeEach(() => {
    ;({ ops, calls } = makeOps())
  })

  it('渲染规则：when 值 + next 各形态 + set 键值', () => {
    renderRules(
      [
        {
          when: 'raw_tool_calls != []',
          then: { next: 'loop', set: { core_plugin: 'pipeline_tool_core' } },
        },
        { when: 'True', then: { next: { phase: 'exit' } } },
        { when: 'done', then: { next: { step: 'post' } } },
      ],
      ops,
    )

    expect(screen.getByLabelText('规则 1 when 条件')).toHaveValue('raw_tool_calls != []')
    expect(screen.getByDisplayValue('pipeline_tool_core')).toBeInTheDocument()
    // phase/step 目标下拉回显现值
    expect(screen.getByLabelText('规则 2 next 目标')).toHaveValue('exit')
    expect(screen.getByLabelText('规则 3 next 目标')).toHaveValue('post')
  })

  it('切换 next 类型 → ops.set 写入字符串形态', () => {
    renderRules([{ when: 'True', then: { next: 'end' } }], ops)

    fireEvent.change(screen.getByLabelText('规则 1 next 类型'), {
      target: { value: 'wait' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...ARRAY_PATH, 0, 'then', 'next'], 'wait'],
    })
  })

  it('选择 phase 目标 → ops.set 写入 {phase} 对象形态', () => {
    renderRules([{ when: 'True', then: { next: { phase: 'exit' } } }], ops)

    fireEvent.change(screen.getByLabelText('规则 1 next 目标'), {
      target: { value: 'main' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...ARRAY_PATH, 0, 'then', 'next'], { phase: 'main' }],
    })
  })

  it('添加规则 → ops.insert 追加默认兜底规则', () => {
    renderRules([{ when: 'True', then: { next: 'end' } }], ops)

    fireEvent.click(screen.getByText('添加规则'))
    expect(calls.at(-1)).toEqual({
      op: 'insert',
      args: [ARRAY_PATH, 1, { when: 'True', then: { next: 'end' } }],
    })
  })

  it('删除规则与移动规则 → ops 路径正确', () => {
    renderRules(
      [
        { when: 'a', then: { next: 'end' } },
        { when: 'b', then: { next: 'end' } },
      ],
      ops,
    )

    fireEvent.click(screen.getByLabelText('规则 2 删除'))
    expect(calls.at(-1)).toEqual({ op: 'remove', args: [[...ARRAY_PATH, 1]] })

    fireEvent.click(screen.getByLabelText('规则 2 上移'))
    expect(calls.at(-1)).toEqual({ op: 'move', args: [ARRAY_PATH, 1, -1] })
  })

  it('then.set 清空 → ops.remove 删除 set 键', () => {
    renderRules(
      [{ when: 'True', then: { next: 'loop', set: { core_type: 'llm_call' } } }],
      ops,
    )

    fireEvent.click(screen.getByLabelText('删除 core_type'))
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...ARRAY_PATH, 0, 'then', 'set']],
    })
  })
})
