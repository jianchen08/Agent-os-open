/**
 * ImageAnnotationView 图片标注 - 单元测试
 *
 * 测试覆盖（对齐现行组件契约）：
 * - 基础渲染（图片、工具栏、标注计数）
 * - 已有标注渲染（矩形区域、编号、建议文字）
 * - 只读模式（无删除按钮）
 * - 空标注列表
 * - 标注类型过滤（只显示 image_area 且有 area 的类型）
 *
 * 已删用例说明：onRemoveAnnotation prop 及删除按钮已从组件移除
 * （删除交互已不在 ImageAnnotationView 内实现），相关「显示删除按钮/
 * 点击删除回调」用例随之删除。
 */

import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { ImageAnnotationView } from '../ImageAnnotationView'
import type { Annotation } from '@/types/review'

/** 构造测试用标注 */
function makeAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'ann1',
    type: 'image_area',
    area: { x: 10, y: 20, width: 30, height: 40 },
    imageUrl: 'test.png',
    suggestion: 'test suggestion',
    createdAt: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('ImageAnnotationView', () => {
  describe('基础渲染', () => {
    it('应渲染图片元素', () => {
      render(
        <ImageAnnotationView
          imageUrl="https://example.com/img.png"
          annotations={[]}
        />,
      )

      const img = screen.getByTestId('annotation-image') as HTMLImageElement
      expect(img).toBeInTheDocument()
      expect(img).toHaveAttribute('src', 'https://example.com/img.png')
    })

    it('应渲染工具栏（标题 + 标注计数）', () => {
      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={[]}
          readOnly={false}
        />,
      )

      // 现行契约：工具栏为标题「图片标注」+ 计数（非只读提示文案已移除）
      expect(screen.getByText('图片标注')).toBeInTheDocument()
      expect(screen.getByTestId('annotation-count')).toBeInTheDocument()
    })

    it('应显示标注计数', () => {
      const annotations = [
        makeAnnotation({ id: 'a1' }),
        makeAnnotation({ id: 'a2' }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      expect(screen.getByTestId('annotation-count')).toHaveTextContent('2 个标注')
    })
  })

  describe('已有标注渲染', () => {
    it('应渲染标注矩形', () => {
      const annotations = [
        makeAnnotation({ id: 'rect1', suggestion: '此处需要修改' }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      // 现行契约：矩形叠加层 testid 按索引命名
      expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
    })

    it('应显示标注编号', () => {
      const annotations = [
        makeAnnotation({ id: 'r1' }),
        makeAnnotation({ id: 'r2' }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      // 编号从 1 开始
      expect(screen.getByText('1')).toBeInTheDocument()
      expect(screen.getByText('2')).toBeInTheDocument()
    })

    it('应渲染多个标注', () => {
      const annotations = [
        makeAnnotation({ id: 'm1', area: { x: 0, y: 0, width: 10, height: 10 } }),
        makeAnnotation({ id: 'm2', area: { x: 50, y: 50, width: 20, height: 20 } }),
        makeAnnotation({ id: 'm3', area: { x: 80, y: 80, width: 15, height: 15 } }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      // 现行契约：矩形叠加层 testid 按索引命名
      expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
      expect(screen.getByTestId('annotation-overlay-1')).toBeInTheDocument()
      expect(screen.getByTestId('annotation-overlay-2')).toBeInTheDocument()
    })
  })

  describe('只读模式', () => {
    it('只读模式不显示拖拽绘制提示', () => {
      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={[]}
          readOnly={true}
        />,
      )

      expect(screen.queryByText('在图片上拖拽绘制标注区域')).not.toBeInTheDocument()
    })

    it('只读模式不显示删除按钮', () => {
      const annotations = [
        makeAnnotation({ id: 'ro1' }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
          readOnly={true}
        />,
      )

      // 现行契约：组件已无删除交互，任何模式下都不出现删除按钮
      expect(screen.queryByTestId('remove-annotation-ro1')).not.toBeInTheDocument()
    })
  })

  describe('空标注列表', () => {
    it('空标注列表应显示 0 个标注', () => {
      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('annotation-count')).toHaveTextContent('0 个标注')
    })

    it('空标注列表不应崩溃', () => {
      expect(() => {
        render(
          <ImageAnnotationView
            imageUrl="test.png"
            annotations={[]}
          />,
        )
      }).not.toThrow()
    })
  })

  describe('标注类型过滤', () => {
    it('只渲染 image_area 类型的标注', () => {
      const annotations = [
        makeAnnotation({ id: 'img1', type: 'image_area', area: { x: 0, y: 0, width: 10, height: 10 } }),
        {
          id: 'vid1',
          type: 'video_timestamp',
          timestamp: 5,
          suggestion: 'video annotation',
          createdAt: '2024-01-01',
        } as Annotation,
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      // 现行契约：矩形叠加层按索引命名，video_timestamp 被过滤（无第 2 个 overlay）
      expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
      expect(screen.queryByTestId('annotation-overlay-1')).not.toBeInTheDocument()
    })

    it('没有 area 属性的 image_area 标注不应渲染矩形', () => {
      const annotations = [
        makeAnnotation({ id: 'noarea', area: undefined }),
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      expect(screen.queryByTestId('annotation-overlay-0')).not.toBeInTheDocument()
    })
  })

  describe('标注计数', () => {
    it('只统计 image_area 类型的标注', () => {
      const annotations = [
        makeAnnotation({ id: 'a1', type: 'image_area' }),
        makeAnnotation({ id: 'a2', type: 'image_area' }),
        {
          id: 'v1',
          type: 'video_timestamp',
          timestamp: 10,
          suggestion: 'video',
          createdAt: '2024-01-01',
        } as Annotation,
      ]

      render(
        <ImageAnnotationView
          imageUrl="test.png"
          annotations={annotations}
        />,
      )

      expect(screen.getByTestId('annotation-count')).toHaveTextContent('2 个标注')
    })
  })
})
