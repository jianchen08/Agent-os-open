// @feature FP-T12 前端组件补测 | @ci: frontend-test
/**
 * 挂起行归因展示（优化推进优先序 P-23 片2，ADR 2026-11-04-run-suspended-reason-key）：
 * 挂起条目详情在 run 域 suspended_reason 非空时渲染「挂起原因」行（人话
 * 对照 + 原词，词表外原词透传）；reason 缺席/空串不渲染归因行（回退现状，
 * 不伪造归因）。双向防拟合：带/不带（缺席 + 空串）两组输入，同构先例 =
 * 停止原因/失败归因行（failureAttribution.test.tsx 同款断言形态）。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PipelineTree } from '../PipelineManagerTreeParts'
import type { PipelineTreeNode } from '../PipelineManagerTreeParts'
import type { PipelineViewEntry } from '@/types/pipeline'

function suspendedNode(suspendedReason?: string): PipelineTreeNode {
  const entry = {
    key: 'pipe-s',
    pipelineId: 'pipe-s',
    runId: 'run-s',
    kind: 'task',
    status: 'suspended',
    name: '挂起任务',
    startedAt: '2026-11-04T00:00:00Z',
    taskId: 't-s',
    taskStatus: 'stopped',
    ...(suspendedReason !== undefined ? { suspendedReason } : {}),
  } as PipelineViewEntry
  return { key: entry.key, entry, depth: 0, children: [] }
}

function renderTree(node: PipelineTreeNode): void {
  render(
    <PipelineTree
      tree={[node]}
      nowMs={Date.parse('2026-11-04T00:01:00Z')}
      treeOpenKeys={new Set()}
      detailOpenKeys={new Set([node.key])}
      onTreeToggle={() => {}}
      onToggleDetail={() => {}}
      onEntryClick={() => {}}
      onAction={() => {}}
    />,
  )
}

describe('挂起行归因详情行（P-23 片2）', () => {
  it('reason 在场（词表内）：「挂起原因」行 = 人话（原词）', () => {
    renderTree(suspendedNode('memory watermark self-heal: private bytes 持续超阈值'))

    const row = screen.getByText('挂起原因').closest('div')
    expect(row?.textContent).toBe(
      '挂起原因内存水位自愈（memory watermark self-heal: private bytes 持续超阈值）',
    )
  })

  it('reason 在场（词表外）：原词透传，不猜语义', () => {
    renderTree(suspendedNode('plugin_watcher: InProcess(cdylib) plugin set changed'))

    const row = screen.getByText('挂起原因').closest('div')
    expect(row?.textContent).toBe('挂起原因plugin_watcher: InProcess(cdylib) plugin set changed')
  })

  it('reason 缺席：不渲染挂起原因行（回退现状，不伪造归因）', () => {
    renderTree(suspendedNode())

    expect(screen.queryByText('挂起原因')).toBeNull()
  })

  it('reason 空串（未署名语义）：不渲染挂起原因行', () => {
    renderTree(suspendedNode(''))

    expect(screen.queryByText('挂起原因')).toBeNull()
  })
})
