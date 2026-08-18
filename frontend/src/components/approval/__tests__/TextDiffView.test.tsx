/**
 * @feature FP-0.2.五 审批闭环 | @vision V2 全能闭环 | @ci frontend-test
 *
 * TextDiffView 文本差异对比 - 单元测试
 *
 * 适配当前组件实现：组件已收敛为**仅 unified（统一）视图**（不再有 side-by-side/
 * mode 切换），每行 testid 为 `diff-line-${idx}` 并带 `data-line-type` 属性。
 * 早期版本基于 side-by-side/mode 的测试随组件简化而重写为本文件。
 *
 * 测试覆盖：相同内容、纯新增、纯删除、混合变更、空内容边界、统计计数、统一视图渲染。
 */

import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { TextDiffView } from '@/components/shared/TextDiffView'

/** 在 diff-content 内按 data-line-type 取行元素列表。 */
function linesByType(container: HTMLElement, type: string): HTMLElement[] {
  return Array.from(
    container.querySelectorAll(`[data-line-type="${type}"]`),
  ) as HTMLElement[]
}

describe('TextDiffView', () => {
  describe('相同内容（无差异）', () => {
    it('相同文本应只有 unchanged 行（统计为 0）', () => {
      const { container } = render(
        <TextDiffView oldContent="hello\nworld" newContent="hello\nworld" />,
      )
      expect(linesByType(container, 'added')).toHaveLength(0)
      expect(linesByType(container, 'removed')).toHaveLength(0)
      // 统计计数为 0（与渲染行数一致）
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+0')
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-0')
    })

    it('统计数字应为 0', () => {
      render(<TextDiffView oldContent="same" newContent="same" />)
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+0')
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-0')
    })
  })

  describe('纯新增内容', () => {
    it('空内容到有内容应标记为 added', () => {
      const { container } = render(
        <TextDiffView oldContent="" newContent="new line" />,
      )
      expect(linesByType(container, 'added').length).toBeGreaterThanOrEqual(1)
      expect(screen.getByText('new line')).toBeInTheDocument()
    })

    it('新增行计数应与渲染的 added 行数一致', () => {
      const { container } = render(
        <TextDiffView oldContent="" newContent="line1\nline2\nline3" />,
      )
      const added = linesByType(container, 'added').length
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent(`+${added}`)
      expect(added).toBeGreaterThan(0)
    })
  })

  describe('纯删除内容', () => {
    it('有内容到空应标记为 removed', () => {
      const { container } = render(
        <TextDiffView oldContent="old line" newContent="" />,
      )
      expect(linesByType(container, 'removed').length).toBeGreaterThanOrEqual(1)
      expect(screen.getByText('old line')).toBeInTheDocument()
    })

    it('删除行计数应与渲染的 removed 行数一致', () => {
      const { container } = render(
        <TextDiffView oldContent="a\nb\nc" newContent="" />,
      )
      const removed = linesByType(container, 'removed').length
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent(`-${removed}`)
      expect(removed).toBeGreaterThan(0)
    })
  })

  describe('混合变更', () => {
    it('应同时产生 added 与 removed 行（混合变更）', () => {
      const { container } = render(
        <TextDiffView oldContent="keep\nremove\nkeep2" newContent="keep\nadd\nkeep2" />,
      )
      // 混合变更必须同时存在新增与删除行（统计计数同理）
      expect(linesByType(container, 'removed').length).toBeGreaterThanOrEqual(1)
      expect(linesByType(container, 'added').length).toBeGreaterThanOrEqual(1)
    })

    it('统计数字应与渲染的 added/removed 行数一致', () => {
      const { container } = render(
        <TextDiffView
          oldContent="keep\nremove1\nremove2"
          newContent="keep\nadd1\nadd2\nadd3"
        />,
      )
      const added = linesByType(container, 'added').length
      const removed = linesByType(container, 'removed').length
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent(`+${added}`)
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent(`-${removed}`)
    })
  })

  describe('空内容边界', () => {
    it('两者都为空字符串时不应崩溃', () => {
      render(<TextDiffView oldContent="" newContent="" />)
      expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    })

    it('单行差异应正确', () => {
      render(<TextDiffView oldContent="a" newContent="b" />)
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    })
  })

  describe('统一视图渲染', () => {
    it('应渲染差异对比标题与统计栏', () => {
      render(<TextDiffView oldContent="old" newContent="new" />)
      expect(screen.getByText('差异对比')).toBeInTheDocument()
      expect(screen.getByTestId('diff-added-count')).toBeInTheDocument()
      expect(screen.getByTestId('diff-removed-count')).toBeInTheDocument()
    })

    it('diff-content 应按 data-line-type 渲染每行', () => {
      const { container } = render(
        <TextDiffView oldContent="x" newContent="y" />,
      )
      // 至少存在 added 与 removed 行
      expect(linesByType(container, 'added').length).toBeGreaterThanOrEqual(1)
      expect(linesByType(container, 'removed').length).toBeGreaterThanOrEqual(1)
      // 每行 testid 形如 diff-line-N
      expect(container.querySelector('[data-testid="diff-line-0"]')).not.toBeNull()
    })
  })
})
