/**
 * ApprovalRouter - 审批视图路由组件（widget 化 T10：声明驱动）
 *
 * view_mode → widget 路由：review_service 插件 tools[].ui.view_modes 声明
 * 优先（widgetRegistry 解析组件——插件声明新 view_mode 即可路由到已注册
 * widget，本组件零改动）；未声明回退内置三视图直连组件（前端默认件，
 * 不依赖 registry 初始化时序）；完全未知降级 text_diff。
 */

import { TextDiffView } from './TextDiffView'
import { ImageAnnotationView } from './ImageAnnotationView'
import { MediaTimelineView } from './MediaTimelineView'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { Annotation } from '@/types/review'
import { resolveViewModeRoute } from '@/utils/viewModeRoutes'

/** 视图模式类型（内置三视图；声明可扩展新 view_mode 字符串） */
export type ViewMode = 'text_diff' | 'image_annotation' | 'media_timeline'

export interface ApprovalRouterProps {
  /** 视图模式，决定展示哪个子组件（声明可扩展） */
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

/**
 * ApprovalRouter
 *
 * 根据 view_mode 路由到对应子组件：声明（widgetRegistry）→ 内置默认 →
 * text_diff 兜底。
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
  const route = resolveViewModeRoute(viewMode)

  // 声明路由：widgetRegistry 解析组件（插件声明的新 view_mode 走此通路）
  if (route?.source === 'declared') {
    const Component = widgetRegistry.get(route.widget)
    if (Component) {
      return (
        <div data-testid={`approval-route-${viewMode}`}>
          <Component
            oldContent={oldContent}
            newContent={newContent}
            imageUrl={imageUrl}
            mediaUrl={mediaUrl}
            mediaType={mediaType}
            duration={duration}
            annotations={annotations}
            readOnly={readOnly}
          />
        </div>
      )
    }
    // 声明了但 widget 未注册（拼写错/组件未上线）→ 落内置/兜底，不白屏
  }

  // 内置默认件：直连组件（不依赖 registry 初始化时序）
  if (route?.source === 'default') {
    switch (route.viewMode) {
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
      case 'text_diff':
        return (
          <div data-testid="approval-route-text_diff">
            <TextDiffView oldContent={oldContent} newContent={newContent} />
          </div>
        )
    }
  }

  // 完全未知 view_mode（含声明了但 widget 未注册）→ 降级文本差异视图
  return (
    <div data-testid="approval-route-text_diff">
      <TextDiffView oldContent={oldContent} newContent={newContent} />
    </div>
  )
}
