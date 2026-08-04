/**
 * TextDiffView 文本差异对比 - 单元测试
 *
 * 测试覆盖：
 * - 完全相同的文本（无差异）
 * - 纯新增内容
 * - 纯删除内容
 * - 混合变更（新增+删除+不变）
 * - 空内容边界
 * - side-by-side 模式渲染
 * - unified 模式切换
 * - 差异统计计数
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { TextDiffView } from '../TextDiffView'

describe('TextDiffView', () => {
  describe('相同内容（无差异）', () => {
    it('相同文本应只有 unchanged 行', () => {
      render(
        <TextDiffView oldContent="hello\nworld" newContent="hello\nworld" />,
      )

      // 不应有 added 或 removed 类型的行
      expect(screen.queryByTestId('diff-line-added')).not.toBeInTheDocument()
      expect(screen.queryByTestId('diff-line-removed')).not.toBeInTheDocument()
    })

    it('统计数字应为 0', () => {
      render(
        <TextDiffView oldContent="same" newContent="same" />,
      )

      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+0')
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-0')
    })
  })

  describe('纯新增内容', () => {
    it('空内容到有内容应标记为 added', () => {
      render(
        <TextDiffView oldContent="" newContent="new line" />,
      )

      expect(screen.getByTestId('diff-line-added')).toBeInTheDocument()
      expect(screen.getByText('new line')).toBeInTheDocument()
    })

    it('新增行计数应正确', () => {
      render(
        <TextDiffView oldContent="" newContent="line1\nline2\nline3" />,
      )

      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+3')
    })
  })

  describe('纯删除内容', () => {
    it('有内容到空应标记为 removed', () => {
      render(
        <TextDiffView oldContent="old line" newContent="" />,
      )

      expect(screen.getByTestId('diff-line-removed')).toBeInTheDocument()
      expect(screen.getByText('old line')).toBeInTheDocument()
    })

    it('删除行计数应正确', () => {
      render(
        <TextDiffView oldContent="a\nb\nc" newContent="" />,
      )

      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-3')
    })
  })

  describe('混合变更', () => {
    it('应正确区分 unchanged / added / removed', () => {
      render(
        <TextDiffView
          oldContent="keep\nremove\nkeep2"
          newContent="keep\nadd\nkeep2"
        />,
      )

      // 有删除行
      const removedLines = screen.queryAllByTestId('diff-line-removed')
      expect(removedLines.length).toBeGreaterThanOrEqual(1)

      // 有新增行
      const addedLines = screen.queryAllByTestId('diff-line-added')
      expect(addedLines.length).toBeGreaterThanOrEqual(1)
    })

    it('统计数字应反映实际变更', () => {
      render(
        <TextDiffView
          oldContent="keep\nremove1\nremove2"
          newContent="keep\nadd1\nadd2\nadd3"
        />,
      )

      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+3')
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-2')
    })
  })

  describe('空内容边界', () => {
    it('两者都为空字符串时不应崩溃', () => {
      render(<TextDiffView oldContent="" newContent="" />)

      expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    })

    it('单行差异应正确', () => {
      render(<TextDiffView oldContent="a" newContent="b" />)

      // "a" 被删除，"b" 被新增
      expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')
      expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    })
  })

  describe('side-by-side 模式', () => {
    it('默认应渲染 side-by-side 视图', () => {
      render(
        <TextDiffView oldContent="old" newContent="new" />,
      )

      expect(screen.getByTestId('side-by-side-view')).toBeInTheDocument()
      expect(screen.queryByTestId('unified-view')).not.toBeInTheDocument()
    })

    it('应显示旧版本和新版本标题', () => {
      render(
        <TextDiffView oldContent="a" newContent="b" />,
      )

      expect(screen.getByText('旧版本')).toBeInTheDocument()
      expect(screen.getByText('新版本')).toBeInTheDocument()
    })
  })

  describe('unified 模式', () => {
    it('应显示"版本对比"标题', () => {
      render(
        <TextDiffView oldContent="a" newContent="b" />,
      )

      expect(screen.getByText('版本对比')).toBeInTheDocument()
    })

    it('点击统一按钮应切换到 unified 视图', () => {
      render(
        <TextDiffView oldContent="old" newContent="new" />,
      )

      // 默认是 side-by-side
      expect(screen.getByTestId('side-by-side-view')).toBeInTheDocument()

      // 点击统一按钮
      fireEvent.click(screen.getByTestId('mode-unified'))

      // 应切换到 unified
      expect(screen.getByTestId('unified-view')).toBeInTheDocument()
      expect(screen.queryByTestId('side-by-side-view')).not.toBeInTheDocument()
    })

    it('切换回 side-by-side 应正常', () => {
      render(
        <TextDiffView oldContent="old" newContent="new" mode="unified" />,
      )

      // 初始 unified
      expect(screen.getByTestId('unified-view')).toBeInTheDocument()

      // 点击左右按钮
      fireEvent.click(screen.getByTestId('mode-side-by-side'))

      expect(screen.getByTestId('side-by-side-view')).toBeInTheDocument()
      expect(screen.queryByTestId('unified-view')).not.toBeInTheDocument()
    })

    it('unified 视图相同内容时显示提示', () => {
      render(
        <TextDiffView oldContent="same" newContent="same" mode="unified" />,
      )

      expect(screen.getByText('两个版本内容相同')).toBeInTheDocument()
    })
  })

  describe('mode 属性', () => {
    it('mode=unified 应初始渲染 unified 视图', () => {
      render(
        <TextDiffView oldContent="a" newContent="b" mode="unified" />,
      )

      expect(screen.getByTestId('unified-view')).toBeInTheDocument()
    })

    it('mode=side-by-side 应初始渲染 side-by-side 视图', () => {
      render(
        <TextDiffView oldContent="a" newContent="b" mode="side-by-side" />,
      )

      expect(screen.getByTestId('side-by-side-view')).toBeInTheDocument()
    })
  })
})
