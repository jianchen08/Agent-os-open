/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 循环体卡片「体 id 编辑」补测：改 id 输入框写回该体路径（编辑落点正确，
 * 不是写到相邻字段）。
 */

import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { LoopBodyCard } from '../LoopBodyCard'
import { makeOps } from './helpers/pipelineTestUtils'
import type { LoopBodyV2 } from '@/services/pipeline/model'

const BODY_PATH = ['loop_bodies', 1]

function renderBody(body: LoopBodyV2) {
  const { ops, calls } = makeOps()
  renderWithProviders(
    <LoopBodyCard
      body={body}
      bodyPath={BODY_PATH}
      bodyIndex={1}
      ops={ops}
      catalog={[]}
      knownStepIds={[]}
      knownPhaseIds={['init', 'main', 'exit']}
      knownStepIdSet={new Set()}
    />,
  )
  return calls
}

describe('LoopBodyCard — 体 id 编辑', () => {
  it('改 id → 写回该体路径的 id 字段', () => {
    const calls = renderBody({ id: 'main', steps: [] } as LoopBodyV2)

    fireEvent.change(screen.getByLabelText('循环体 id'), { target: { value: 'worker' } })

    expect(calls).toContainEqual({ op: 'set', args: [[...BODY_PATH, 'id'], 'worker'] })
  })

  it('体缺 id（脏数据）时输入框为空且有可编辑入口', () => {
    renderBody({ steps: [] } as LoopBodyV2)

    expect(screen.getByLabelText('循环体 id')).toHaveValue('')
  })
})
