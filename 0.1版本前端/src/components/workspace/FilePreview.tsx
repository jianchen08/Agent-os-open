/**
 * 文件预览组件
 *
 * 在工作区面板中提供多格式文件的在线预览功能。
 * 支持图片、PDF、代码只读预览和二进制文件友好提示。
 *
 * @module components/workspace/FilePreview
 */

import { FileQuestion, Download, ZoomIn, ZoomOut, RotateCw } from 'lucide-react'
import React, { useCallback, useMemo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { cn } from '@/lib/utils'

/** 图片扩展名集合 */
const IMAGE_EXTENSIONS = new Set([
  '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
])

/** PDF 扩展名 */
const PDF_EXTENSION = '.pdf'

/** 代码只读预览扩展名集合 */
const CODE_PREVIEW_EXTENSIONS = new Set([
  '.json', '.yaml', '.yml', '.toml', '.xml', '.csv', '.tsv', '.md',
  '.markdown', '.txt', '.log', '.env', '.properties', '.ini', '.cfg',
  '.conf', '.graphql', '.gql', '.proto', '.map', '.lock', '.svg',
  '.gitignore', '.dockerignore', '.editorconfig', '.eslintrc',
  '.prettierrc', '.dockerfile', '.makefile', '.gradle', '.cmake',
])

/** 文件扩展名 → Prism 语言映射 */
const EXTENSION_TO_LANGUAGE: Record<string, string> = {
  '.json': 'json',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.xml': 'xml',
  '.csv': 'text',
  '.tsv': 'text',
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.txt': 'text',
  '.log': 'text',
  '.env': 'bash',
  '.properties': 'properties',
  '.ini': 'ini',
  '.cfg': 'ini',
  '.conf': 'ini',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.proto': 'protobuf',
  '.map': 'json',
  '.lock': 'text',
  '.svg': 'xml',
  '.gitignore': 'text',
  '.dockerignore': 'text',
  '.editorconfig': 'ini',
  '.eslintrc': 'json',
  '.prettierrc': 'json',
  '.dockerfile': 'docker',
  '.makefile': 'makefile',
  '.gradle': 'groovy',
  '.cmake': 'cmake',
}

/** 预览类型 */
type PreviewType = 'image' | 'pdf' | 'code' | 'binary'

/**
 * SyntaxHighlighter 行级样式
 *
 * 修复 react-syntax-highlighter@16 在同时开启 showLineNumbers + wrapLongLines 时的
 * "竖排/每行只剩几个字"问题：库会在每行 span 上强制注入 `display: flex`（见其
 * highlight.js 的 `wrapLongLines & showLineNumbers` 分支），flex 容器打破 inline 文本
 * 流并折叠前导空格（缩进），导致 yaml 等缩进敏感语言被挤压成竖条、无法阅读。
 * 这里用 `display: block` 覆盖库注入的 `display: flex`（库内部
 * `_objectSpread({display:'flex'}, lineProps.style)`，后展开的同名属性胜），
 * `whiteSpace: pre-wrap` 保留缩进与长行换行。
 */
const HIGHLIGHTER_LINE_PROPS = { style: { whiteSpace: 'pre-wrap', display: 'block' } } as const

/** 文件预览组件属性 */
export interface FilePreviewProps {
  /** 文件路径（如 assets/logo.png） */
  filePath: string
  /** 文件内容（文本文件） */
  content: string
  /** 文件大小（字节） */
  size?: number
  /** 用于构建文件下载/访问 URL 的容器任务 ID */
  containerTaskId: string
  /** 自定义类名 */
  className?: string
  /** 附件直链 URL（如 /uploads/xxx.pdf）；存在时优先于 containerTaskId 拼接的 workspaces URL */
  url?: string
}

/**
 * 从文件名提取扩展名
 *
 * @param fileName - 文件名或文件路径
 * @returns 小写扩展名
 */
function extractExtension(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)
  if (baseName.startsWith('.') && baseName.lastIndexOf('.') === 0) {
    return baseName.toLowerCase()
  }
  const dotIndex = baseName.lastIndexOf('.')
  if (dotIndex === -1) {
    return baseName.toLowerCase()
  }
  return baseName.substring(dotIndex).toLowerCase()
}

/**
 * 判断预览类型
 *
 * @param filePath - 文件路径
 * @returns 预览类型
 */
function getPreviewType(filePath: string): PreviewType {
  const ext = extractExtension(filePath)
  if (IMAGE_EXTENSIONS.has(ext)) return 'image'
  if (ext === PDF_EXTENSION) return 'pdf'
  if (CODE_PREVIEW_EXTENSIONS.has(ext)) return 'code'
  // 尝试通过语言映射判断是否为可读文本
  if (ext in EXTENSION_TO_LANGUAGE) return 'code'
  return 'binary'
}

/**
 * 文件预览组件
 *
 * 根据文件类型自动选择预览方式：
 * - 图片：居中显示，支持缩放
 * - PDF：iframe/embed 渲染
 * - 代码：只读语法高亮
 * - 二进制：友好提示
 */
export function FilePreview({
  filePath,
  content,
  size,
  containerTaskId,
  className,
  url,
}: FilePreviewProps) {
  const [zoomLevel, setZoomLevel] = useState(100)
  const [rotation, setRotation] = useState(0)

  const fileName = useMemo(() => {
    const lastSlash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
    return filePath.substring(lastSlash + 1)
  }, [filePath])

  const previewType = useMemo(() => getPreviewType(filePath), [filePath])
  const language = useMemo(() => {
    const ext = extractExtension(filePath)
    return EXTENSION_TO_LANGUAGE[ext] ?? 'text'
  }, [filePath])

  /** 构建文件访问 URL（用于图片和 PDF） */
  const fileUrl = useMemo(() => {
    // 附件直链优先（不依赖 workspaces API）
    if (url) return url
    if (!containerTaskId) return ''
    return `/api/v1/workspaces/${containerTaskId}/file-content?path=${encodeURIComponent(filePath)}`
  }, [url, containerTaskId, filePath])

  const handleZoomIn = useCallback(() => {
    setZoomLevel((prev) => Math.min(prev + 25, 400))
  }, [])

  const handleZoomOut = useCallback(() => {
    setZoomLevel((prev) => Math.max(prev - 25, 25))
  }, [])

  const handleRotate = useCallback(() => {
    setRotation((prev) => (prev + 90) % 360)
  }, [])

  const handleDownload = useCallback(() => {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    a.click()
    URL.revokeObjectURL(url)
  }, [content, fileName])

  /** 图片预览 */
  if (previewType === 'image') {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        {/* 工具栏 */}
        <div className="border-border bg-muted/30 flex items-center justify-between border-b px-4 py-2">
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <div className="flex items-center gap-1">
            <button
              onClick={handleZoomOut}
              className="hover:bg-accent text-muted-foreground rounded p-1 transition-colors"
              title="缩小"
            >
              <ZoomOut className="h-4 w-4" />
            </button>
            <span className="text-muted-foreground min-w-[3rem] text-center text-xs">
              {zoomLevel}%
            </span>
            <button
              onClick={handleZoomIn}
              className="hover:bg-accent text-muted-foreground rounded p-1 transition-colors"
              title="放大"
            >
              <ZoomIn className="h-4 w-4" />
            </button>
            <button
              onClick={handleRotate}
              className="hover:bg-accent text-muted-foreground rounded p-1 transition-colors"
              title="旋转"
            >
              <RotateCw className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 图片显示区域 */}
        <div className="flex flex-1 items-center justify-center overflow-auto bg-[var(--code-bg,#1a1a2e)]/30 p-4">
          {fileUrl ? (
            <img
              src={fileUrl}
              alt={fileName}
              className="max-w-full object-contain"
              style={{
                transform: `scale(${zoomLevel / 100}) rotate(${rotation}deg)`,
                transformOrigin: 'center center',
                transition: 'transform 0.2s ease',
              }}
              onError={(e) => {
                const target = e.target as HTMLImageElement
                target.style.display = 'none'
                const parent = target.parentElement
                if (parent) {
                  parent.innerHTML =
                    '<div class="text-center"><p class="text-foreground text-sm font-medium">图片加载失败</p><p class="text-muted-foreground text-xs mt-1">无法预览此图片文件</p></div>'
                }
              }}
            />
          ) : (
            <p className="text-muted-foreground text-sm">无法获取图片地址</p>
          )}
        </div>
      </div>
    )
  }

  /** PDF 预览 */
  if (previewType === 'pdf') {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <span className="text-muted-foreground text-xs">（PDF 预览）</span>
        </div>
        <div className="flex-1 overflow-hidden">
          {fileUrl ? (
            <iframe
              src={fileUrl}
              className="h-full w-full border-0"
              title={`预览 ${fileName}`}
            />
          ) : (
            <div className="flex h-full items-center justify-center">
              <p className="text-muted-foreground text-sm">无法加载 PDF 文件</p>
            </div>
          )}
        </div>
      </div>
    )
  }

  /** 代码只读预览 */
  if (previewType === 'code') {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2">
            <span className="text-foreground text-sm font-medium">{fileName}</span>
            <span className="text-muted-foreground text-xs lowercase">
              {language !== 'text' ? language : ''}
            </span>
            <span className="text-muted-foreground text-xs">（只读）</span>
          </div>
          <button
            onClick={handleDownload}
            className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs transition-colors"
            title="下载文件"
          >
            <Download className="h-3.5 w-3.5" />
            下载
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          <SyntaxHighlighter
            language={language}
            style={oneDark}
            showLineNumbers={true}
            wrapLongLines={true}
            lineProps={HIGHLIGHTER_LINE_PROPS}
            customStyle={{
              margin: 0,
              borderRadius: 0,
              fontSize: '0.8125rem',
              background: 'var(--code-bg, #1e1e1e)',
              minHeight: '100%',
            }}
            codeTagProps={{
              style: {
                fontFamily:
                  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
              },
            }}
          >
            {content}
          </SyntaxHighlighter>
        </div>
      </div>
    )
  }

  /** 二进制文件提示 */
  return (
    <div className={cn('flex h-full flex-col', className)}>
      <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
        <span className="text-foreground text-sm font-medium">{fileName}</span>
        {size != null && (
          <span className="text-muted-foreground text-xs">
            ({(size / 1024).toFixed(1)} KB)
          </span>
        )}
      </div>
      <div className="flex flex-1 items-center justify-center p-8">
        <div className="text-center">
          <FileQuestion className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
          <p className="text-foreground mb-1 text-sm font-medium">无法预览此文件</p>
          <p className="text-muted-foreground text-xs">
            该文件类型（{extractExtension(filePath) || '未知'}）暂不支持在线预览。
          </p>
          <button
            onClick={handleDownload}
            className="mt-3 flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <Download className="h-3.5 w-3.5" />
            下载文件到本地查看
          </button>
        </div>
      </div>
    </div>
  )
}
