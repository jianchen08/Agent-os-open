/**
 * MediaTimelineView 媒体时间轴 - 单元测试
 *
 * 测试覆盖：
 * - 基础渲染（播放器、时间轴、控制按钮）
 * - 时间显示格式化
 * - 已有标注渲染（标记点、标注列表）
 * - 只读模式（无添加输入框）
 * - 空标注列表
 * - 标注类型过滤（只显示 video_timestamp）
 * - 删除标注按钮
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { MediaTimelineView } from '../MediaTimelineView'
import type { Annotation } from '@/types/review'

/** 构造测试用时间轴标注 */
function makeTimestampAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'ts1',
    type: 'video_timestamp',
    timestamp: 5,
    suggestion: 'at 5 seconds',
    createdAt: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('MediaTimelineView', () => {
  describe('基础渲染', () => {
    it('应渲染视频播放器', () => {
      render(
        <MediaTimelineView
          mediaUrl="https://example.com/video.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('media-player')).toBeInTheDocument()
    })

    it('应将 mediaUrl 设置为 video src', () => {
      render(
        <MediaTimelineView
          mediaUrl="https://example.com/movie.mp4"
          annotations={[]}
        />,
      )

      const video = screen.getByTestId('media-player') as HTMLVideoElement
      expect(video).toHaveAttribute('src', 'https://example.com/movie.mp4')
    })

    it('应渲染播放控制按钮', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('toggle-play')).toBeInTheDocument()
      expect(screen.getByTestId('skip-back')).toBeInTheDocument()
      expect(screen.getByTestId('skip-forward')).toBeInTheDocument()
    })

    it('应渲染时间轴条', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('timeline-bar')).toBeInTheDocument()
    })

    it('应渲染时间显示', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('time-display')).toBeInTheDocument()
    })

    it('初始时间显示应为 00:00 / 00:00', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('time-display')).toHaveTextContent('00:00 / 00:00')
    })
  })

  describe('时间格式化', () => {
    it('传入 duration prop 应正确显示', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={120}
          annotations={[]}
        />,
      )

      // 初始 currentTime=0, videoDuration=120
      expect(screen.getByTestId('time-display')).toHaveTextContent('00:00 / 02:00')
    })

    it('30秒应显示为 00:30', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('time-display')).toHaveTextContent('00:00 / 00:30')
    })

    it('3600秒应显示为 60:00', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={3600}
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('time-display')).toHaveTextContent('00:00 / 60:00')
    })
  })

  describe('已有标注渲染', () => {
    it('应渲染时间轴上的标注标记点', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'm1', timestamp: 10 }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={60}
          annotations={annotations}
        />,
      )

      expect(screen.getByTestId('annotation-marker-m1')).toBeInTheDocument()
    })

    it('应在标注列表中显示标注', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'l1', timestamp: 5, suggestion: 'first annotation' }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
        />,
      )

      expect(screen.getByTestId('annotation-item-l1')).toBeInTheDocument()
      expect(screen.getByText('first annotation')).toBeInTheDocument()
    })

    it('应按 timestamp 排序标注', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'late', timestamp: 20, suggestion: 'late' }),
        makeTimestampAnnotation({ id: 'early', timestamp: 5, suggestion: 'early' }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
        />,
      )

      const items = screen.getAllByTestId(/^annotation-item-/)
      // early 应排在前面
      expect(items[0]).toHaveAttribute('data-testid', 'annotation-item-early')
      expect(items[1]).toHaveAttribute('data-testid', 'annotation-item-late')
    })

    it('应显示标注编号', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'n1', timestamp: 5 }),
        makeTimestampAnnotation({ id: 'n2', timestamp: 10 }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
        />,
      )

      // 列表中应有编号 1 和 2
      const items = screen.getAllByTestId(/^annotation-item-/)
      expect(items[0]).toHaveTextContent('1')
      expect(items[1]).toHaveTextContent('2')
    })
  })

  describe('只读模式', () => {
    it('只读模式不显示添加标注输入框（点击时间轴后）', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={60}
          annotations={[]}
          readOnly={true}
        />,
      )

      // 只读模式下，点击时间轴不会触发添加输入
      // 输入框不应出现
      expect(screen.queryByTestId('annotation-input')).not.toBeInTheDocument()
    })

    it('只读模式不显示删除按钮', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'ro1' }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
          readOnly={true}
          onRemoveAnnotation={vi.fn()}
        />,
      )

      expect(screen.queryByTestId('remove-annotation-ro1')).not.toBeInTheDocument()
    })

    it('非只读 + 有 onRemoveAnnotation 时显示删除按钮', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'rm1' }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
          readOnly={false}
          onRemoveAnnotation={vi.fn()}
        />,
      )

      expect(screen.getByTestId('remove-annotation-rm1')).toBeInTheDocument()
    })
  })

  describe('空标注列表', () => {
    it('空标注不应崩溃', () => {
      expect(() => {
        render(
          <MediaTimelineView
            mediaUrl="test.mp4"
            annotations={[]}
          />,
        )
      }).not.toThrow()
    })

    it('空标注不显示标注列表', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.queryByTestId('annotation-list')).not.toBeInTheDocument()
    })
  })

  describe('标注类型过滤', () => {
    it('只渲染 video_timestamp 类型的标注', () => {
      const annotations = [
        makeTimestampAnnotation({ id: 'vid1', type: 'video_timestamp', timestamp: 5 }),
        {
          id: 'img1',
          type: 'image_area',
          area: { x: 0, y: 0, width: 10, height: 10 },
          suggestion: 'image annotation',
          createdAt: '2024-01-01',
        } as Annotation,
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
        />,
      )

      expect(screen.getByTestId('annotation-item-vid1')).toBeInTheDocument()
      expect(screen.queryByTestId('annotation-item-img1')).not.toBeInTheDocument()
    })

    it('timestamp 为 null 的标注不应显示', () => {
      const annotations = [
        {
          id: 'nots',
          type: 'video_timestamp',
          suggestion: 'no timestamp',
          createdAt: '2024-01-01',
        } as Annotation,
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
        />,
      )

      expect(screen.queryByTestId('annotation-item-nots')).not.toBeInTheDocument()
    })
  })

  describe('删除标注回调', () => {
    it('点击删除按钮应调用 onRemoveAnnotation', () => {
      const onRemove = vi.fn()
      const annotations = [
        makeTimestampAnnotation({ id: 'del1' }),
      ]

      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          duration={30}
          annotations={annotations}
          onRemoveAnnotation={onRemove}
        />,
      )

      const deleteBtn = screen.getByTestId('remove-annotation-del1')
      fireEvent.click(deleteBtn)

      expect(onRemove).toHaveBeenCalledWith('del1')
    })
  })

  describe('播放控制', () => {
    it('应渲染播放/暂停按钮', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      const toggleBtn = screen.getByTestId('toggle-play')
      expect(toggleBtn).toBeInTheDocument()
      expect(toggleBtn).toHaveAttribute('title', '播放')
    })
  })

  describe('进度显示', () => {
    it('应渲染播放进度条', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('timeline-progress')).toBeInTheDocument()
    })

    it('应渲染播放头', () => {
      render(
        <MediaTimelineView
          mediaUrl="test.mp4"
          annotations={[]}
        />,
      )

      expect(screen.getByTestId('timeline-playhead')).toBeInTheDocument()
    })
  })
})
