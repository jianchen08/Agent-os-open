/**
 * 审批视图组件 - 单元测试
 *
 * 覆盖组件：
 * 1. ApprovalRouter - 路由分发与无效 view_mode 降级
 * 2. TextDiffView - 文本差异对比与增/删/改行高亮
 * 3. ImageAnnotationView - 图片显示与标注层叠加
 * 4. MediaTimelineView - 媒体播放器与时间轴标记
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'

import { ApprovalRouter } from '../ApprovalRouter'
import { TextDiffView } from '../TextDiffView'
import { ImageAnnotationView } from '../ImageAnnotationView'
import { MediaTimelineView } from '../MediaTimelineView'

import type { Annotation } from '@/types/review'

// ============================================================
// 测试数据工厂
// ============================================================

/** 创建图片区域批注 */
function createImageAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: `ann-${Math.random().toString(36).slice(2, 8)}`,
    type: 'image_area',
    area: { x: 10, y: 20, width: 30, height: 40 },
    imageUrl: 'https://example.com/image.png',
    suggestion: '标注说明',
    createdAt: new Date().toISOString(),
    ...overrides,
  }
}

/** 创建视频时间轴批注 */
function createVideoAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: `ann-${Math.random().toString(36).slice(2, 8)}`,
    type: 'video_timestamp',
    timestamp: 5.0,
    suggestion: '视频标注',
    createdAt: new Date().toISOString(),
    ...overrides,
  }
}

// ============================================================
// ApprovalRouter 测试
// ============================================================

describe('ApprovalRouter', () => {
  it('view_mode 为 text_diff 时应路由到 TextDiffView', () => {
    render(
      <ApprovalRouter
        viewMode="text_diff"
        oldContent="hello"
        newContent="world"
      />,
    )

    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
  })

  it('view_mode 为 image_annotation 时应路由到 ImageAnnotationView', () => {
    render(
      <ApprovalRouter
        viewMode="image_annotation"
        imageUrl="https://example.com/img.png"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('approval-route-image_annotation')).toBeInTheDocument()
    expect(screen.getByTestId('image-annotation-view')).toBeInTheDocument()
  })

  it('view_mode 为 media_timeline 时应路由到 MediaTimelineView', () => {
    render(
      <ApprovalRouter
        viewMode="media_timeline"
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('approval-route-media_timeline')).toBeInTheDocument()
    expect(screen.getByTestId('media-timeline-view')).toBeInTheDocument()
  })

  it('无效 view_mode 应降级到 text_diff 视图', () => {
    render(
      <ApprovalRouter
        viewMode="invalid_mode"
        oldContent="old"
        newContent="new"
      />,
    )

    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
  })

  it('空 view_mode 应降级到 text_diff 视图', () => {
    render(
      <ApprovalRouter
        viewMode=""
        oldContent="a"
        newContent="b"
      />,
    )

    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
  })

  it('未知的 view_mode 应降级到 text_diff 视图', () => {
    render(
      <ApprovalRouter
        viewMode="unknown_view"
        oldContent="foo"
        newContent="bar"
      />,
    )

    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
  })

  it('应将 props 传递给子组件', () => {
    const annotations = [createImageAnnotation({ suggestion: '测试标注' })]

    render(
      <ApprovalRouter
        viewMode="image_annotation"
        imageUrl="https://example.com/img.png"
        annotations={annotations}
        readOnly={true}
      />,
    )

    expect(screen.getByTestId('annotation-count')).toHaveTextContent('1 个标注')
  })
})

// ============================================================
// TextDiffView 测试
// ============================================================

describe('TextDiffView', () => {
  it('应正确渲染差异对比视图', () => {
    render(
      <TextDiffView
        oldContent="hello\nworld"
        newContent="hello\nearth"
      />,
    )

    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    // 有差异时应显示 diff-content
    expect(screen.getByTestId('diff-content')).toBeInTheDocument()
  })

  it('应显示新增行并高亮', () => {
    render(
      <TextDiffView
        oldContent="line1"
        newContent="line1\nline2"
      />,
    )

    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')

    const diffLines = screen.getAllByTestId(/^diff-line-/)
    const addedLines = diffLines.filter((el) => el.getAttribute('data-line-type') === 'added')
    expect(addedLines.length).toBe(1)
    expect(addedLines[0]).toHaveTextContent('line2')
  })

  it('应显示删除行并高亮', () => {
    render(
      <TextDiffView
        oldContent="line1\nline2"
        newContent="line1"
      />,
    )

    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')

    const diffLines = screen.getAllByTestId(/^diff-line-/)
    const removedLines = diffLines.filter((el) => el.getAttribute('data-line-type') === 'removed')
    expect(removedLines.length).toBe(1)
    expect(removedLines[0]).toHaveTextContent('line2')
  })

  it('内容完全相同时应显示无差异提示', () => {
    render(
      <TextDiffView
        oldContent="same text"
        newContent="same text"
      />,
    )

    // 无差异时显示 diff-empty
    expect(screen.getByTestId('diff-empty')).toBeInTheDocument()
    expect(screen.getByTestId('diff-empty')).toHaveTextContent('两个版本内容相同')
    // 不应显示 diff-content
    expect(screen.queryByTestId('diff-content')).not.toBeInTheDocument()
  })

  it('内容相同时变更统计应为零', () => {
    render(
      <TextDiffView
        oldContent="same text"
        newContent="same text"
      />,
    )

    // 内容相同：无增删，只有未变更行
    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+0')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-0')
  })

  it('应正确统计变更行数', () => {
    render(
      <TextDiffView
        oldContent="line1"
        newContent="line2"
      />,
    )

    // line1 整行被替换: 1 removed + 1 added
    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')
  })

  it('两侧都为空内容应显示无差异', () => {
    render(
      <TextDiffView
        oldContent=""
        newContent=""
      />,
    )

    expect(screen.getByTestId('diff-empty')).toBeInTheDocument()
    expect(screen.getByTestId('diff-empty')).toHaveTextContent('无内容')
  })

  it('旧内容为空时应全部标记为新增', () => {
    render(
      <TextDiffView
        oldContent=""
        newContent="only new content"
      />,
    )

    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-0')
  })

  it('新内容为空时应全部标记为删除', () => {
    render(
      <TextDiffView
        oldContent="only old content"
        newContent=""
      />,
    )

    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+0')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')
  })

  it('多行差异应正确交错显示删除和新增', () => {
    render(
      <TextDiffView
        oldContent={"line1\nline2"}
        newContent={"line1\nline3"}
      />,
    )

    // 应有1个删除和1个新增，1个未变更
    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')

    // 差异内容应存在
    expect(screen.getByTestId('diff-content')).toBeInTheDocument()

    // 验证存在 removed 和 added 类型的行
    const diffLines = screen.getAllByTestId(/^diff-line-/)
    const removedLines = diffLines.filter((el) => el.getAttribute('data-line-type') === 'removed')
    const addedLines = diffLines.filter((el) => el.getAttribute('data-line-type') === 'added')
    const unchangedLines = diffLines.filter((el) => el.getAttribute('data-line-type') === 'unchanged')

    expect(removedLines.length).toBeGreaterThanOrEqual(1)
    expect(addedLines.length).toBeGreaterThanOrEqual(1)
    expect(unchangedLines.length).toBeGreaterThanOrEqual(1)
  })
})

// ============================================================
// ImageAnnotationView 测试
// ============================================================

describe('ImageAnnotationView', () => {
  it('应正确渲染图片标注视图', () => {
    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('image-annotation-view')).toBeInTheDocument()
  })

  it('应显示图片元素', () => {
    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={[]}
      />,
    )

    const img = screen.getByTestId('annotation-image')
    expect(img).toBeInTheDocument()
    expect(img).toHaveAttribute('src', 'https://example.com/test.png')
  })

  it('应使用自定义 alt 文本', () => {
    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        altText="自定义描述"
        annotations={[]}
      />,
    )

    const img = screen.getByTestId('annotation-image')
    expect(img).toHaveAttribute('alt', '自定义描述')
  })

  it('无标注时应显示无标注占位', () => {
    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('no-annotations')).toBeInTheDocument()
    expect(screen.getByTestId('no-annotations')).toHaveTextContent('暂无标注')
  })

  it('应显示标注数量', () => {
    const annotations = [
      createImageAnnotation({ suggestion: '标注1' }),
      createImageAnnotation({ suggestion: '标注2' }),
    ]

    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('annotation-count')).toHaveTextContent('2 个标注')
  })

  it('应渲染标注叠加层', () => {
    const annotations = [
      createImageAnnotation({
        id: 'ann-1',
        area: { x: 10, y: 20, width: 30, height: 40 },
        suggestion: '标注区域1',
      }),
      createImageAnnotation({
        id: 'ann-2',
        area: { x: 50, y: 60, width: 20, height: 25 },
        suggestion: '标注区域2',
      }),
    ]

    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
    expect(screen.getByTestId('annotation-overlay-1')).toBeInTheDocument()

    // 标注编号
    expect(screen.getByTestId('annotation-badge-0')).toHaveTextContent('1')
    expect(screen.getByTestId('annotation-badge-1')).toHaveTextContent('2')
  })

  it('应过滤非 image_area 类型的批注', () => {
    const annotations = [
      createImageAnnotation({ suggestion: '图片标注' }),
      createVideoAnnotation({ suggestion: '视频标注' }),
    ]

    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('annotation-count')).toHaveTextContent('1 个标注')
    expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
  })

  it('应正确设置标注区域的 CSS 定位', () => {
    const annotations = [
      createImageAnnotation({
        area: { x: 15, y: 25, width: 35, height: 45 },
      }),
    ]

    render(
      <ImageAnnotationView
        imageUrl="https://example.com/test.png"
        annotations={annotations}
      />,
    )

    const overlay = screen.getByTestId('annotation-overlay-0')
    expect(overlay.style.left).toBe('15%')
    expect(overlay.style.top).toBe('25%')
    expect(overlay.style.width).toBe('35%')
    expect(overlay.style.height).toBe('45%')
  })
})

// ============================================================
// MediaTimelineView 测试
// ============================================================

describe('MediaTimelineView', () => {
  it('应正确渲染媒体时间轴视图', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('media-timeline-view')).toBeInTheDocument()
  })

  it('video 类型应渲染视频播放器', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('video-player')).toBeInTheDocument()
  })

  it('audio 类型应渲染音频播放器', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/audio.mp3"
        mediaType="audio"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('audio-player')).toBeInTheDocument()
  })

  it('应显示播放/暂停按钮', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        annotations={[]}
      />,
    )

    const playBtn = screen.getByTestId('play-pause-btn')
    expect(playBtn).toBeInTheDocument()
    expect(playBtn).toHaveTextContent('▶')
  })

  it('应显示时间显示区域', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={120}
        annotations={[]}
      />,
    )

    const timeDisplay = screen.getByTestId('time-display')
    expect(timeDisplay).toBeInTheDocument()
    expect(timeDisplay).toHaveTextContent('00:00')
    expect(timeDisplay).toHaveTextContent('02:00')
  })

  it('应显示时间轴条', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('timeline-bar')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-progress')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-playhead')).toBeInTheDocument()
  })

  it('应在时间轴上显示标注标记', () => {
    const annotations = [
      createVideoAnnotation({ timestamp: 5.0, suggestion: '标注1' }),
      createVideoAnnotation({ timestamp: 15.0, suggestion: '标注2' }),
    ]

    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={60}
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('timeline-marker-0')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-marker-1')).toBeInTheDocument()
  })

  it('应显示标注列表', () => {
    const annotations = [
      createVideoAnnotation({ timestamp: 5.0, suggestion: '第一段标注' }),
      createVideoAnnotation({ timestamp: 20.0, suggestion: '第二段标注' }),
    ]

    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={60}
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('annotation-list')).toBeInTheDocument()
    expect(screen.getByTestId('annotation-item-0')).toBeInTheDocument()
    expect(screen.getByTestId('annotation-item-1')).toBeInTheDocument()

    expect(screen.getByTestId('annotation-item-0')).toHaveTextContent('第一段标注')
    expect(screen.getByTestId('annotation-item-1')).toHaveTextContent('第二段标注')
  })

  it('无视频时间轴批注时不应显示标注列表', () => {
    const annotations = [
      createImageAnnotation({ suggestion: '图片标注' }),
    ]

    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={60}
        annotations={annotations}
      />,
    )

    expect(screen.queryByTestId('annotation-list')).not.toBeInTheDocument()
  })

  it('标注应按时间戳排序', () => {
    const annotations = [
      createVideoAnnotation({ timestamp: 30.0, suggestion: '后面的标注' }),
      createVideoAnnotation({ timestamp: 5.0, suggestion: '前面的标注' }),
    ]

    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={60}
        annotations={annotations}
      />,
    )

    const firstItem = screen.getByTestId('annotation-item-0')
    expect(firstItem).toHaveTextContent('前面的标注')

    const secondItem = screen.getByTestId('annotation-item-1')
    expect(secondItem).toHaveTextContent('后面的标注')
  })

  it('应接受 duration 属性作为总时长', () => {
    render(
      <MediaTimelineView
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={180}
        annotations={[]}
      />,
    )

    const timeDisplay = screen.getByTestId('time-display')
    expect(timeDisplay).toHaveTextContent('03:00')
  })
})

// ============================================================
// 集成测试 - ApprovalRouter 与子组件的协作
// ============================================================

describe('ApprovalRouter 集成测试', () => {
  it('text_diff 模式应正确传递内容到 TextDiffView', () => {
    render(
      <ApprovalRouter
        viewMode="text_diff"
        oldContent="old text"
        newContent="new text"
      />,
    )

    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+1')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')
  })

  it('image_annotation 模式应正确传递批注', () => {
    const annotations = [
      createImageAnnotation({ suggestion: '集成测试标注' }),
    ]

    render(
      <ApprovalRouter
        viewMode="image_annotation"
        imageUrl="https://example.com/img.png"
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('annotation-count')).toHaveTextContent('1 个标注')
    expect(screen.getByTestId('annotation-overlay-0')).toBeInTheDocument()
  })

  it('media_timeline 模式应正确传递媒体和批注', () => {
    const annotations = [
      createVideoAnnotation({ timestamp: 10.0, suggestion: '集成标注' }),
    ]

    render(
      <ApprovalRouter
        viewMode="media_timeline"
        mediaUrl="https://example.com/video.mp4"
        mediaType="video"
        duration={120}
        annotations={annotations}
      />,
    )

    expect(screen.getByTestId('video-player')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-marker-0')).toBeInTheDocument()
    expect(screen.getByTestId('annotation-item-0')).toHaveTextContent('集成标注')
  })

  it('切换 viewMode 应渲染不同的组件', () => {
    const { rerender } = render(
      <ApprovalRouter
        viewMode="text_diff"
        oldContent="a"
        newContent="b"
      />,
    )

    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
    expect(screen.queryByTestId('approval-route-image_annotation')).not.toBeInTheDocument()

    rerender(
      <ApprovalRouter
        viewMode="image_annotation"
        imageUrl="https://example.com/img.png"
        annotations={[]}
      />,
    )

    expect(screen.getByTestId('approval-route-image_annotation')).toBeInTheDocument()
    expect(screen.queryByTestId('approval-route-text_diff')).not.toBeInTheDocument()
  })
})
