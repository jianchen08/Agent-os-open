/**
 * 「无对应的加载器」卡（文件加载器路由设计 §9）。
 *
 * 文件类型未命中任何加载器时的工作区页签内容：显式说明 + 兜底动作
 * （下载到本地，按需鉴权取流）。绝不渲染裸 404/错误文本。
 */

import { useCallback } from 'react'
import { toast } from 'sonner'
import { Download, FileQuestion } from '@/assets/icons'
import apiClient from '@/services/api/client'

export interface NoLoaderPreviewProps {
  /** 文件路径/名（用于提取扩展名与下载命名） */
  filePath: string
  /** 文件大小（字节，可选） */
  size?: number
  /** 内容端点路径（下载动作按需取流；apiClient 携认证头） */
  url?: string
}

function extractExt(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)
  const dotIndex = baseName.lastIndexOf('.')
  return (dotIndex === -1 ? baseName : baseName.substring(dotIndex)).toLowerCase()
}

export function NoLoaderPreview({ filePath, size, url }: NoLoaderPreviewProps) {
  const ext = extractExt(filePath)
  const lastSlash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
  const fileName = filePath.substring(lastSlash + 1)

  const handleDownload = useCallback(async () => {
    if (!url) return
    try {
      const resp = await apiClient.get<Blob>(url, {
        baseURL: '',
        responseType: 'blob',
        silent: true,
      })
      const blobUrl = URL.createObjectURL(resp.data)
      const a = document.createElement('a')
      a.href = blobUrl
      a.download = fileName
      a.click()
      URL.revokeObjectURL(blobUrl)
    } catch (error) {
      const status = (error as { response?: { status?: number } } | null)?.response?.status
      toast.error(
        status
          ? `文件 "${fileName}" 下载失败（HTTP ${status}）`
          : `文件 "${fileName}" 下载失败，请检查网络或文件是否可用`,
      )
    }
  }, [url, fileName])

  return (
    <div className="flex h-full flex-col">
      <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
        <span className="text-foreground text-sm font-medium">{fileName}</span>
        {size != null && (
          <span className="text-muted-foreground text-xs">({(size / 1024).toFixed(1)} KB)</span>
        )}
      </div>
      <div className="flex flex-1 items-center justify-center p-8">
        <div className="text-center">
          <FileQuestion className="text-muted-foreground mx-auto mb-3 h-10 w-10" />
          <p className="text-foreground mb-1 text-sm font-medium">暂无支持 {ext || '未知'} 类型的加载器</p>
          <p className="text-muted-foreground text-xs">该文件类型暂无可用的在线查看器。</p>
          <button
            onClick={() => void handleDownload()}
            className="bg-primary hover:bg-primary/90 mt-3 flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-primary-foreground transition-colors"
          >
            <Download className="h-3.5 w-3.5" />
            下载到本地查看
          </button>
        </div>
      </div>
    </div>
  )
}
