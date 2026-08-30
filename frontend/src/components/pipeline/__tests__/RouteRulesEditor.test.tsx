/**
 * RouteRulesEditor 组件测试
 *
 * 覆盖 G10 文件 DSL 路由规则编辑器核心功能：
 * - 渲染已有规则（when / then 目标字符串 / set 键值）
 * - then 目标选项按 scope 收窄（step 级含 loop/本地 step/体 id；体级仅 end/体 id）
 * - 编辑 when / then → ops 写入文件 DSL 形态（then 为字符串、set 平级）
 * - 空条件 → 摘掉 when 键；set 清空 → remove（避免写出空 set 对象）
 * - 添加/删除/移动规则 → ops 路径正确
 * - wait 已退役不出现
 */

import { screen, fireEvent, render } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { RouteRulesEditor } from '../RouteRulesEditor'
import type { PipelineEditorOps, TransitionRule } from '@/services/pipeline/model'

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

const ARRAY_PATH = ['loop_bodies', 1, 'steps', 2, 'next']

function renderRules(
  rules: TransitionRule[] | undefined,
  ops: PipelineEditorOps,
  overrides?: { scope?: 'step' | 'body'; localStepIds?: string[] },
) {
  return render(
    <RouteRulesEditor
      rules={rules}
      arrayPath={ARRAY_PATH}
      ops={ops}
      scope={overrides?.scope ?? 'step'}
      localStepIds={overrides?.localStepIds ?? ['prepare', 'core', 'post']}
      knownBodyIds={['init', 'main', 'exit']}
      label="next（step 级出口转移）"
    />,
  )
}

function selectOptions(label: string): string[] {
  const select = screen.getByLabelText(label) as HTMLSelectElement
  return Array.from(select.options).map((o) => o.value)
}

describe('RouteRulesEditor（G10 文件 DSL）', () => {
  let ops: PipelineEditorOps
  let calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }>

  beforeEach(() => {
    ;({ ops, calls } = makeOps())
  })

  it('渲染规则：when 值 + then 目标字符串 + set 键值', () => {
    renderRules(
      [
        {
          when: 'conversation_mode == True and raw_tool_calls == []',
          then: 'loop',
          set: { suspended: true },
        },
        { when: "task.status == 'completed'", then: 'end' },
        { then: 'exit' },
      ],
      ops,
    )

    expect(screen.getByLabelText('规则 1 when 条件')).toHaveValue(
      'conversation_mode == True and raw_tool_calls == []',
    )
    expect(screen.getByLabelText('规则 1 then 目标')).toHaveValue('loop')
    expect(screen.getByLabelText('规则 2 then 目标')).toHaveValue('end')
    // 目标不在合法集也保留显示（历史遗留目标不静默丢失）
    expect(screen.getByLabelText('规则 3 then 目标')).toHaveValue('exit')
    expect(screen.getByLabelText('suspended 键名')).toBeInTheDocument()
  })

  it('step 级目标选项含 end/loop/本地 step/循环体；不含退役 wait', () => {
    renderRules([{ when: 'True', then: 'end' }], ops)
    expect(selectOptions('规则 1 then 目标')).toEqual([
      'end',
      'loop',
      'prepare',
      'core',
      'post',
      'init',
      'main',
      'exit',
    ])
  })

  it('体级目标选项仅 end/循环体 id（loop 非法、step 目标不接受）', () => {
    render(
      <RouteRulesEditor
        rules={[{ when: 'True', then: 'end' }]}
        arrayPath={ARRAY_PATH}
        ops={ops}
        scope="body"
        knownBodyIds={['init', 'main', 'exit']}
        label="next（循环体结束转移）"
      />,
    )
    expect(selectOptions('规则 1 then 目标')).toEqual(['end', 'init', 'main', 'exit'])
  })

  it('改 then 目标 → ops.set 写字符串到 then 路径（非 then.next 对象）', () => {
    renderRules([{ when: 'True', then: 'end' }], ops)

    fireEvent.change(screen.getByLabelText('规则 1 then 目标'), {
      target: { value: 'prepare' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'set',
      args: [[...ARRAY_PATH, 0, 'then'], 'prepare'],
    })
  })

  it('清空 when → ops.remove 摘掉 when 键（缺省恒真语义）', () => {
    renderRules([{ when: 'True', then: 'end' }], ops)

    fireEvent.change(screen.getByLabelText('规则 1 when 条件'), {
      target: { value: '' },
    })
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...ARRAY_PATH, 0, 'when']],
    })
  })

  it('添加规则 → ops.insert 追加默认兜底规则（DSL 形态）', () => {
    renderRules([{ then: 'end' }], ops)

    fireEvent.click(screen.getByText('添加规则'))
    expect(calls.at(-1)).toEqual({
      op: 'insert',
      args: [ARRAY_PATH, 1, { when: 'True', then: 'end' }],
    })
  })

  it('删除规则与移动规则 → ops 路径正确', () => {
    renderRules(
      [
        { when: 'a', then: 'end' },
        { when: 'b', then: 'end' },
      ],
      ops,
    )

    fireEvent.click(screen.getByLabelText('规则 2 删除'))
    expect(calls.at(-1)).toEqual({ op: 'remove', args: [[...ARRAY_PATH, 1]] })

    fireEvent.click(screen.getByLabelText('规则 2 上移'))
    expect(calls.at(-1)).toEqual({ op: 'move', args: [ARRAY_PATH, 1, -1] })
  })

  it('set 清空 → ops.remove 删除平级 set 键', () => {
    renderRules([{ when: 'True', then: 'loop', set: { core_type: 'llm_call' } }], ops)

    fireEvent.click(screen.getByLabelText('删除 core_type'))
    expect(calls.at(-1)).toEqual({
      op: 'remove',
      args: [[...ARRAY_PATH, 0, 'set']],
    })
  })

  it('无规则显示空态（顺序执行语义）', () => {
    renderRules(undefined, ops)
    expect(screen.getByText(/无路由规则/)).toBeInTheDocument()
  })
})
