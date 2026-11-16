/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * registerWidgets 审批三视图适配件接线补测：text_diff / image_annotation /
 * media_timeline 三个适配 widget 经组合根注册后可按名取用，并把声明 props
 * 透传为对应视图组件的属性（缺省值兜底不崩）。
 *
 * 断行为：注册表按名可查到组件，渲染产物呈现声明内容。
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

// 审批视图本体重量级（markdown/媒体面）：以契约替身承接，断言 props 透传
vi.mock('@/components/shared/TextDiffView', () => ({
  TextDiffView: (props: { oldContent: string; newContent: string }) => (
    <div data-testid="text-diff-view" data-old={props.oldContent} data-new={props.newContent} />
  ),
}))
vi.mock('@/components/approval/ImageAnnotationView', () => ({
  ImageAnnotationView: (props: { imageUrl: string; readOnly?: boolean }) => (
    <div data-testid="image-annotation-view" data-url={props.imageUrl} data-readonly={String(props.readOnly)} />
  ),
}))
vi.mock('@/components/approval/MediaTimelineView', () => ({
  MediaTimelineView: (props: { mediaUrl: string; mediaType?: string }) => (
    <div data-testid="media-timeline-view" data-url={props.mediaUrl} data-type={props.mediaType} />
  ),
}))

function renderWidget(name: string, props: Record<string, unknown>) {
  const Widget = widgetRegistry.get(name)
  expect(Widget).toBeTruthy()
  const Component = Widget as (p: Record<string, unknown>) => JSX.Element
  render(<Component {...props} />)
}

beforeEach(() => {
  widgetRegistry.clear()
  contributionRegistry.clear()
  initializeWidgets()
})

describe('registerWidgets — 审批三视图适配件', () => {
  it('text_diff：声明内容透传为 old/new（缺省空串）', () => {
    renderWidget('text_diff', { oldContent: '旧值', newContent: '新值' })

    const view = screen.getByTestId('text-diff-view')
    expect(view).toHaveAttribute('data-old', '旧值')
    expect(view).toHaveAttribute('data-new', '新值')
  })

  it('image_annotation：图片地址与只读态透传', () => {
    renderWidget('image_annotation', { imageUrl: 'blob:img-1', readOnly: true })

    const view = screen.getByTestId('image-annotation-view')
    expect(view).toHaveAttribute('data-url', 'blob:img-1')
    expect(view).toHaveAttribute('data-readonly', 'true')
  })

  it('media_timeline：媒体地址/类型透传（非 audio 一律按 video）', () => {
    renderWidget('media_timeline', { mediaUrl: 'blob:media-1', mediaType: 'audio' })

    const view = screen.getByTestId('media-timeline-view')
    expect(view).toHaveAttribute('data-url', 'blob:media-1')
    expect(view).toHaveAttribute('data-type', 'audio')
  })
})
