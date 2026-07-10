/**
 * HTML 预览组件
 *
 * 使用 iframe srcDoc 渲染内联 HTML 内容。
 * 支持两种输入方式：
 * - html: 直接传入 HTML 字符串
 * - filePath: 通过 API 读取文件内容（降级方案，优先使用 html）
 *
 * @module widgets/HtmlPreviewWidget
 */

import React, { useState, useEffect, useRef } from 'react'
import { FileWarning } from 'lucide-react'

/** HTML 预览组件属性 */
export interface HtmlPreviewWidgetProps {
  /** 内联 HTML 内容 */
  html?: string
  /** 文件路径（通过 API 读取，html 优先） */
  filePath?: string
  /** 标题（显示在 Tab 上） */
  title?: string
  /** 工作空间容器任务 ID */
  containerTaskId?: string
}

/**
 * HTML 预览组件
 *
 * 将 HTML 内容通过 iframe 的 srcDoc 渲染，无 sandbox 限制以支持完整交互。
 * 优先使用传入的 html 属性，其次通过 filePath + API 读取。
 * 使用 absolute 定位确保 iframe 填满父容器，不受 flex/overflow 嵌套影响。
 *
 * @param props - 组件属性
 * @returns HTML 预览 iframe
 */
export function HtmlPreviewWidget({
  html,
  filePath,
  title,
  containerTaskId,
}: HtmlPreviewWidgetProps): React.ReactNode {
  const [content, setContent] = useState<string | null>(html ?? null)
  const [error, setError] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (html) {
      setContent(html)
      return
    }
    if (!filePath || !containerTaskId) return

    const encoded = encodeURIComponent(filePath)
    fetch(`/api/v1/workspaces/${containerTaskId}/file-content?path=${encoded}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => {
        if (data.success) {
          setContent(data.content as string)
        } else {
          setError(data.message ?? '读取失败')
        }
      })
      .catch((e) => setError((e as Error).message))
  }, [html, filePath, containerTaskId])

  if (error) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        <FileWarning className="mr-2 h-5 w-5" />
        加载失败: {error}
      </div>
    )
  }

  if (!content) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        加载中...
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative h-full w-full">
      <iframe
        srcDoc={content}
        title={title ?? 'HTML Preview'}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals"
        className="absolute inset-0 border-0 bg-white"
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  )
}

export default HtmlPreviewWidget
