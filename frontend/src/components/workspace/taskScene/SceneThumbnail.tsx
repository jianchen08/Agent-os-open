/**
 * 现场截图缩略图（双来源：workspace 落盘路径 / 结果内 base64）
 *
 * 40×40 缩略图 + 点击灯箱看原图（灯箱复用 ActivityBlockViews 的 ImageLightbox，
 * 与对话卡同一交互）。取流拼法抄 FilePreview（`workspaces_file_content_get?path=`）
 * ——`useAuthedAttachmentUrl` 只认 /uploads 直链，对 workspace 相对路径会 404。
 *
 * **G2（base64 撞 8KB 截断）处置**：结果被 `truncateToolResultData` 换成截断标记时
 * 取不到 base64，此位显示「图」角标（SceneBadge），点击触发既有
 * `expandTruncatedToolResult` 回取全量；回取成功后 store 更新，缩略图自然出现。
 * 回取失败如实显示「截图待回取」，不假装有图。
 */

import { useState } from 'react'
import { ImageLightbox } from '@/components/chat/ActivityBlockViews'
import { cn } from '@/lib/utils'
import { expandTruncatedToolResult } from '@/utils/toolResultBackfill'
import { SceneBadge } from './SceneBadge'
import { extractSceneImageSource, workspaceFileUrl } from './sceneImageSource'
import type { SceneActivity } from './sceneActivities'
import type { FC } from 'react'

/** 缩略图边长（与方案 §3.3.2 一致：40×40 object-contain） */
const THUMB_SIZE_CLASS = 'h-10 w-10'

/** 缩略图数据 URL（base64 直用） */
function base64Url(mime: string, data: string): string {
  return `data:${mime};base64,${data}`
}

export interface SceneThumbnailProps {
  activity: SceneActivity
  /** 容器任务 ID 兜底（条目自身没带 containerTaskId 时用页签的 taskId） */
  containerTaskId?: string
}

/** 现场截图缩略图（无图返回 null，调用方不渲染占位） */
export const SceneThumbnail: FC<SceneThumbnailProps> = ({ activity, containerTaskId }) => {
  const [open, setOpen] = useState(false)
  const [decodeFailed, setDecodeFailed] = useState(false)
  const [refetching, setRefetching] = useState(false)
  const [refetchFailed, setRefetchFailed] = useState(false)

  const source = extractSceneImageSource(activity)
  if (!source) return null

  // 截断态：base64 被吞 → 角标提示「有图可回取」，点击走既有回取通道
  if (source.kind === 'truncated') {
    const handleRefetch = async (): Promise<void> => {
      if (refetching) return
      setRefetching(true)
      setRefetchFailed(false)
      const ok = await expandTruncatedToolResult({
        messageId: activity.messageId,
        callId: activity.callId,
        ...(source.sequence !== undefined ? { sequence: source.sequence } : {}),
      })
      setRefetching(false)
      if (!ok) setRefetchFailed(true)
    }
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          void handleRefetch()
        }}
        className="flex-shrink-0"
        title={
          refetchFailed
            ? '截图原图回取失败（结果已截断），可展开结果查看预览'
            : '截图被截断（base64 超阈），点击回取原图'
        }
        data-testid="scene-thumbnail-truncated"
      >
        <SceneBadge
          label={refetching ? '取图中' : refetchFailed ? '图待回取' : '图'}
          tone={refetchFailed ? 'warning' : 'info'}
        />
      </button>
    )
  }

  const url = source.kind === 'base64' ? base64Url(source.mime, source.data) : workspaceFileUrl(containerTaskId, source.path)

  // 取不到流（无容器坐标/路径为空）：角标提示，不渲染破图
  if (!url || decodeFailed) {
    return (
      <span className="flex-shrink-0" data-testid="scene-thumbnail-fallback">
        <SceneBadge label="截图" tone="muted" />
      </span>
    )
  }

  return (
    <>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setOpen(true)
        }}
        className="flex-shrink-0 overflow-hidden rounded ring-border/40 hover:ring-1"
        title="点击查看原图"
        data-testid="scene-thumbnail"
      >
        <img
          src={url}
          alt="截图缩略图"
          loading="lazy"
          decoding="async"
          onError={() => setDecodeFailed(true)}
          className={cn(THUMB_SIZE_CLASS, 'cursor-zoom-in object-contain')}
        />
      </button>
      {open && <ImageLightbox src={url} alt="截图原图" onClose={() => setOpen(false)} />}
    </>
  )
}
