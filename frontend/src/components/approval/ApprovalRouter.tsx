/**
 * ApprovalRouter - 审批视图路由组件
 *
 * 根据制品的 view_mode 将审批请求路由到对应的子视图组件。
 * 支持的视图模式：text_diff、image_annotation、media_timeline。
 * 无效的 view_mode 会降级到文本差异视图。
 */

import React from 'react'
import { TextDiffView } from './TextDiffView'
import { ImageAnnotationView } from './ImageAnnotationView'
import { MediaTimelineView } from './MediaTimelineView'
import type { Annotation } from '@/types/review'

/** 视图模式类型 */
export type ViewMode = 'text_diff' | 'image_annotation' | 'media_timeline'

/** 默认视图模式 */
const DEFAULT_VIEW_MODE: ViewMode = 'text_diff'

export interface ApprovalRouterProps {
  /** 视图模式，决定展示哪个子组件 */
  viewMode: string
  /** 旧版文本内容（text_diff 模式使用） */
  oldContent?: string
  /** 新版文本内容（text_diff 模式使用） */
  newContent?: string
  /** 图片 URL（image_annotation 模式使用） */
  imageUrl?: string
  /** 媒体 URL（media_timeline 模式使用） */
  mediaUrl?: string
  /** 媒体类型（media_timeline 模式使用） */
  mediaType?: 'video' | 'audio'
  /** 媒体时长（media_timeline 模式使用） */
  duration?: number
  /** 批注列表 */
  annotations?: Annotation[]
  /** 是否只读 */
  readOnly?: boolean
}

/** 验证 viewMode 是否为有效值 */
function isValidViewMode(mode: string): mode is ViewMode {
  return ['text_diff', 'image_annotation', 'media_timeline'].includes(mode)
}

/**
 * ApprovalRouter
 *
 * 根据 view_mode 路由到对应子组件，无效模式降级为文本差异视图。
 */
export function ApprovalRouter({
  viewMode,
  oldContent = '',
  newContent = '',
  imageUrl = '',
  mediaUrl = '',
  mediaType = 'video',
  duration,
  annotations = [],
  readOnly = false,
}: ApprovalRouterProps) {
  const resolvedMode: ViewMode = isValidViewMode(viewMode) ? viewMode : DEFAULT_VIEW_MODE

  switch (resolvedMode) {
    case 'text_diff':
      return (
        <div data-testid="approval-route-text_diff">
          <TextDiffView
            oldContent={oldContent}
            newContent={newContent}
          />
        </div>
      )

    case 'image_annotation':
      return (
        <div data-testid="approval-route-image_annotation">
          <ImageAnnotationView
            imageUrl={imageUrl}
            annotations={annotations}
            readOnly={readOnly}
          />
        </div>
      )

    case 'media_timeline':
      return (
        <div data-testid="approval-route-media_timeline">
          <MediaTimelineView
            mediaUrl={mediaUrl}
            mediaType={mediaType}
            duration={duration}
            annotations={annotations}
            readOnly={readOnly}
          />
        </div>
      )

    default:
      return (
        <div data-testid="approval-route-text_diff">
          <TextDiffView
            oldContent={oldContent}
            newContent={newContent}
          />
        </div>
      )
  }
}
