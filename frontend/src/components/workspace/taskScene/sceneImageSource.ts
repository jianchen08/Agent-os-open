/**
 * 截图来源派生（纯函数，任务现场批 3）
 *
 * 四类设备/软件的截图回落点不同（方案 §3.3.2）：
 * - 浏览器 `browser_take_screenshot` → `result.file_path`（任务工作区内落盘路径）
 * - 桌面 `Screenshot`（Windows-MCP）→ MCP 归一 `{output:{images:[{base64,mime_type}]}}`
 *   （内核 `kernel/crates/invoker/src/invoker/mcp_result.rs`）或直挂 `images`
 * - 手机 `phone_control`（action=screenshot）→ `result.data`（base64 原样回流，
 *   `plugins/shared/phone_control/phone_control.py`）
 *
 * **G2 风险**：`truncateToolResultData` 在 8KB 截断（`utils/toolResultTruncate.ts`），
 * base64 截图通常几十 KB～数 MB → 结果被换成截断标记，base64 取不出来。此情形
 * 返回 `truncated`，由缩略图位给「图」角标 + 展开回取（既有
 * `expandTruncatedToolResult`），不静默丢图。
 */

import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { isTruncatedToolResult } from '@/utils/toolResultTruncate'
import type { SceneActivity } from './sceneActivities'

/** 截图来源三态（undefined = 该条目本来就没有图） */
export type SceneImageSource =
  | { kind: 'file'; path: string }
  | { kind: 'base64'; mime: string; data: string }
  | { kind: 'truncated'; sequence?: number; totalBytes: number }

/** 扩展名 → MIME（落盘截图猜类型用；未知按 png） */
const IMAGE_MIME_BY_EXT: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
}

/**
 * 是否截图类工具（结果形态判定 + 截断态给角标的依据）。
 *
 * 工具名取活工具面真名：浏览器 `browser_take_screenshot`、桌面 `desktop_capture`
 * （返回图像块）、手机 `phone_control(action=screenshot)`；`read_image` 读的是已有
 * 图片文件，同样具备可展示图像。`Screenshot` 为历史落库消息兼容名。
 */
export function isScreenshotActivity(activity: SceneActivity): boolean {
  if (activity.toolName === 'browser_take_screenshot') return true
  if (activity.toolName === 'desktop_capture') return true
  if (activity.toolName === 'read_image') return true
  if (activity.toolName === 'Screenshot') return true
  if (activity.toolName === 'phone_control' && activity.args.action === 'screenshot') return true
  return false
}

/**
 * 条目 → 截图来源（无图返回 undefined）。
 *
 * 判定顺序：截断标记（仅截图类）→ 浏览器 file_path → MCP images → 手机 data。
 */
export function extractSceneImageSource(activity: SceneActivity): SceneImageSource | undefined {
  const truncated = pickTruncated(activity)
  if (truncated) return truncated

  const data = activity.resultData
  const result = activity.result

  // 浏览器截图：落盘路径（output_schema 的 file_path）
  const filePath = strOf(record(data)?.file_path) ?? strOf(record(result)?.file_path)
  if (filePath) return { kind: 'file', path: filePath }

  // 桌面截图：MCP 归一 images[] / 直挂 images[]
  const images = imagesOf(data) ?? imagesOf(result)
  if (images) return images

  // 手机截图：data 字段为 base64（mime 按 png——上游 uia2-server 回 PNG）
  if (activity.toolName === 'phone_control' && activity.args.action === 'screenshot') {
    const base64 = strOf(record(data)?.data) ?? strOf(record(result)?.data)
    if (base64 && base64.length > 64) return { kind: 'base64', mime: 'image/png', data: base64 }
  }

  // 兜底：图片扩展名路径（部分工具回 path 而非 file_path）
  const otherPath = strOf(record(data)?.path) ?? strOf(record(result)?.path)
  if (otherPath && isImagePath(otherPath)) {
    return { kind: 'file', path: otherPath }
  }
  return undefined
}

/** 截断态：截图类且结果被换成截断标记 → 给出「需回取」信号 */
function pickTruncated(activity: SceneActivity): SceneImageSource | undefined {
  if (!isScreenshotActivity(activity)) return undefined
  for (const value of [activity.resultData, activity.result]) {
    if (isTruncatedToolResult(value)) {
      return {
        kind: 'truncated',
        ...(value.sequence !== undefined ? { sequence: value.sequence } : {}),
        totalBytes: value.totalBytes,
      }
    }
  }
  return undefined
}

/** images 数组 → base64 来源（取首张；空数组/坏结构返回 undefined） */
function imagesOf(value: unknown): SceneImageSource | undefined {
  const recordValue = record(value)
  const direct = Array.isArray(recordValue?.images) ? recordValue?.images : undefined
  const nested = record(recordValue?.output)
  const nestedImages = Array.isArray(nested?.images) ? nested?.images : undefined
  const list = direct ?? nestedImages
  if (!list || list.length === 0) return undefined
  const first = record(list[0])
  const data = strOf(first?.base64) ?? strOf(first?.data)
  if (!data) return undefined
  const mime = strOf(first?.mime_type) ?? strOf(first?.mimeType) ?? 'image/png'
  return { kind: 'base64', mime, data }
}

/** 图片路径判定（扩展名白名单） */
function isImagePath(path: string): boolean {
  const lower = path.toLowerCase()
  return Object.keys(IMAGE_MIME_BY_EXT).some((ext) => lower.endsWith(ext))
}

/**
 * workspace 落盘路径 → 取流 URL（拼法同 FilePreview.tsx）。
 *
 * `useAuthedAttachmentUrl` 只认 /uploads 直链，workspace 相对路径必须拼该端点；
 * 容器坐标缺省返回空串（调用方给「截图」角标，不渲染破图）。
 */
export function workspaceFileUrl(containerTaskId: string | undefined, path: string): string {
  if (!containerTaskId) return ''
  return `${WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace('{container_task_id}', containerTaskId)}?path=${encodeURIComponent(path)}`
}

/** 图片路径 → MIME（未知按 png） */
export function imageMimeOfPath(path: string): string {
  const lower = path.toLowerCase()
  for (const [ext, mime] of Object.entries(IMAGE_MIME_BY_EXT)) {
    if (lower.endsWith(ext)) return mime
  }
  return 'image/png'
}

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined
}

function strOf(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}
