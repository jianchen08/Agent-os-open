/**
 * 文件预览组件
 *
 * 在工作区面板中提供多格式文件的在线预览功能。
 * 支持图片、PDF、Markdown、音视频、Excel、代码只读预览和二进制文件友好提示。
 *
 * @module components/workspace/FilePreview
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { FileQuestion, Download, ZoomIn, ZoomOut, RotateCw } from '@/assets/icons'
import { codeHighlightStyle } from '@/components/shared/markdown/codeHighlightStyle'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import {
  EXTENSION_TO_LANGUAGE,
  extractExtension,
  getLanguageForFileName,
  PrismSyntaxHighlighter,
} from '@/components/shared/markdown/prismLanguages'
import { cn } from '@/lib/utils'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import type { ExcelWorkbook } from '@/components/workspace/excelSheet'

/** 图片扩展名集合 */
const IMAGE_EXTENSIONS = new Set([
  '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
])

/** PDF 扩展名 */
const PDF_EXTENSION = '.pdf'

/** 音视频扩展名（浏览器原生 <video>/<audio> 可播；取流走原始字节面不解码文本） */
const MEDIA_EXTENSIONS = new Set([
  '.mp4', '.webm', '.mov', '.m4v', '.mp3', '.wav', '.ogg', '.m4a', '.flac',
])

/** Markdown 扩展名（渲染态预览 + 一键切源码） */
const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown'])

/** Excel 扩展名（@e965/xlsx 解析；.xlsb 未声明的取舍见 fileViewerRegistry excel_viewer 注释） */
const EXCEL_EXTENSIONS = new Set(['.xlsx', '.xlsm', '.xls'])

/** 视频扩展名（<video> 与 <audio> 按此分流） */
const VIDEO_EXTENSIONS = new Set(['.mp4', '.webm', '.mov', '.m4v'])

/** 代码只读预览扩展名集合 */
const CODE_PREVIEW_EXTENSIONS = new Set([
  '.json', '.yaml', '.yml', '.toml', '.xml', '.csv', '.tsv', '.md',
  '.markdown', '.txt', '.log', '.env', '.properties', '.ini', '.cfg',
  '.conf', '.graphql', '.gql', '.proto', '.map', '.lock', '.svg',
  '.gitignore', '.dockerignore', '.editorconfig', '.eslintrc',
  '.prettierrc', '.dockerfile', '.makefile', '.gradle', '.cmake',
])

/** 预览类型 */
type PreviewType = 'image' | 'pdf' | 'markdown' | 'media' | 'excel' | 'code' | 'binary'

/**
 * SyntaxHighlighter 行级样式
 *
 * react-syntax-highlighter@16 在同时开启 showLineNumbers + wrapLongLines 时，
 * 会在每行 span 上强制注入 `display: flex`（见其
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
  /** 附件内容 URL（内存 blob URL——附件经 openFileWithLoader 鉴权取流生成）；存在时优先于 containerTaskId 拼接的 workspaces URL */
  url?: string
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
  // markdown / 音视频 / excel 先于代码判定：这些扩展名部分同时命中
  // CODE_PREVIEW_EXTENSIONS（.md）或无语言映射，落进代码分支就只剩源码，
  // 用户要的是预览态
  if (MARKDOWN_EXTENSIONS.has(ext)) return 'markdown'
  if (MEDIA_EXTENSIONS.has(ext)) return 'media'
  if (EXCEL_EXTENSIONS.has(ext)) return 'excel'
  if (CODE_PREVIEW_EXTENSIONS.has(ext)) return 'code'
  // 尝试通过语言映射判断是否为可读文本
  if (ext in EXTENSION_TO_LANGUAGE) return 'code'
  return 'binary'
}

/**
 * 取预览字节（Excel 用）：url 优先（附件 blob 直链 = 真字节），
 * content 兜底按 UTF-8 编码。两者都缺返回 undefined（调用方显式报错）。
 *
 * 已知局限（如实交代）：工作区 file-content 端点对二进制做 UTF-8 replace 解码
 * 后包 JSON 返回（plugins/shared/workspace/server.py），工作区场景拿到的 content
 * 非真字节，xlsx（zip 容器）解析必失败 → 显式错误态。附件 blob url 场景为真字节
 * 可正常渲染。修复归属 workspace 插件（超出本模块边界）。
 */
async function loadPreviewBytes(url?: string, content?: string): Promise<Uint8Array | undefined> {
  if (url) {
    const response = await fetch(url)
    if (!response.ok) throw new Error(`取流失败: ${response.status}`)
    return new Uint8Array(await response.arrayBuffer())
  }
  if (typeof content === 'string' && content !== '') {
    return new TextEncoder().encode(content)
  }
  return undefined
}

/**
 * Excel 正文：@e965/xlsx 解析（动态 import，独立 lazy chunk）+ 只读表格渲染。
 * 状态机：loading → success / empty / error；多 sheet 可切换；超限表截断提示。
 */
function ExcelBody({ url, content }: { url?: string; content?: string }) {
  const [workbook, setWorkbook] = useState<ExcelWorkbook | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [activeSheet, setActiveSheet] = useState(0)

  useEffect(() => {
    let cancelled = false
    setError(null)
    setWorkbook(null)
    setActiveSheet(0)
    void (async () => {
      try {
        const bytes = await loadPreviewBytes(url, content)
        if (!bytes) throw new Error('缺少文件数据，无法预览')
        const { parseExcelWorkbook } = await import('@/components/workspace/excelSheet')
        if (cancelled) return
        // parseExcelWorkbook 为 async（OLE2 需先装载 codepage 表，审查 MF-1 修复）
        const workbook = await parseExcelWorkbook(bytes)
        // await 之后必须复查 cancelled：旧文件解析完成晚于新文件挂载时，
        // 无守卫会把过期 workbook 写进新文件的状态（查看器复验 MF 修复面）
        if (cancelled) return
        setWorkbook(workbook)
      } catch (err) {
        if (!cancelled) setError((err as Error).message)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [content, url])

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="text-center">
          <p className="text-foreground text-sm font-medium">Excel 渲染失败</p>
          <p className="text-muted-foreground mt-1 text-xs">{error}</p>
        </div>
      </div>
    )
  }
  if (!workbook) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        Excel 解析中…
      </div>
    )
  }
  const sheet = workbook.sheets[activeSheet] ?? workbook.sheets[0]
  if (!sheet) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        工作簿不含任何工作表
      </div>
    )
  }
  return (
    <div className="flex h-full flex-col" data-testid="file-preview-excel">
      {workbook.sheets.length > 1 && (
        <div className="border-border bg-muted/30 flex flex-wrap gap-1 border-b px-3 py-1.5">
          {workbook.sheets.map((s, i) => (
            <button
              key={s.name}
              type="button"
              onClick={() => setActiveSheet(i)}
              className={cn(
                'rounded px-2 py-0.5 text-xs transition-colors',
                i === activeSheet
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      {sheet.truncated && (
        <div className="text-status-waiting bg-status-waiting/10 px-3 py-1 text-[11px]">
          仅显示前 {sheet.rows.length} 行（共 {sheet.totalRows} 行）
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        {sheet.rows.length === 0 ? (
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            空工作表
          </div>
        ) : (
          <table className="w-full border-collapse text-xs" data-testid="excel-table">
            <tbody>
              {sheet.rows.map((row, r) => (
                <tr key={r} className="border-border/60 border-b">
                  {row.map((cell, c) => (
                    <td
                      key={c}
                      className="border-border/40 text-foreground px-2 py-1 whitespace-nowrap"
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

/**
 * 文件预览组件
 *
 * 根据文件类型自动选择预览方式：
 * - 图片：居中显示，支持缩放
 * - PDF：iframe/embed 渲染
 * - Markdown：渲染态 + 一键切源码
 * - 音视频：浏览器原生播放
 * - Excel：@e965/xlsx 解析 + 只读表格（选型真值 = docs/working/971600b7ff1e_查看器选型与双链方案.md §1.2）
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
  /** markdown 预览态 / 源码态（默认渲染态） */
  const [markdownSource, setMarkdownSource] = useState(false)

  const fileName = useMemo(() => {
    const lastSlash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
    return filePath.substring(lastSlash + 1)
  }, [filePath])

  const previewType = useMemo(() => getPreviewType(filePath), [filePath])
  const language = useMemo(() => getLanguageForFileName(filePath), [filePath])

  /** 构建文件访问 URL（用于图片和 PDF） */
  const fileUrl = useMemo(() => {
    // 附件直链优先（不依赖 workspaces API）
    if (url) return url
    if (!containerTaskId) return ''
    return `${WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace('{container_task_id}', containerTaskId)}?path=${encodeURIComponent(filePath)}`
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
              className="hover:bg-muted text-muted-foreground rounded p-1 transition-colors"
              title="缩小"
            >
              <ZoomOut className="h-4 w-4" />
            </button>
            <span className="text-muted-foreground min-w-[3rem] text-center text-xs">
              {zoomLevel}%
            </span>
            <button
              onClick={handleZoomIn}
              className="hover:bg-muted text-muted-foreground rounded p-1 transition-colors"
              title="放大"
            >
              <ZoomIn className="h-4 w-4" />
            </button>
            <button
              onClick={handleRotate}
              className="hover:bg-muted text-muted-foreground rounded p-1 transition-colors"
              title="旋转"
            >
              <RotateCw className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 图片显示区域 */}
        <div className="flex flex-1 items-center justify-center overflow-auto bg-[rgb(var(--code-bg-rgb,26_26_46)/0.3)] p-4">
          {fileUrl ? (
            <img
              src={fileUrl}
              alt={fileName}
              className="max-w-full object-contain"
              // eslint-disable-next-line no-restricted-syntax -- 缩放/旋转变换每帧动态值（zoomLevel/rotation state），无法静态类化
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

  /** Markdown 预览（渲染态 + 一键切源码） */
  if (previewType === 'markdown') {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2">
            <span className="text-foreground text-sm font-medium">{fileName}</span>
            <span className="text-muted-foreground text-xs">Markdown</span>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setMarkdownSource((prev) => !prev)}
              className="text-muted-foreground hover:text-foreground text-xs transition-colors"
              title={markdownSource ? '切到渲染预览' : '查看 Markdown 源码'}
              data-testid="file-preview-markdown-toggle"
            >
              {markdownSource ? '预览' : '源码'}
            </button>
            <button
              type="button"
              onClick={handleDownload}
              className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs transition-colors"
              title="下载文件"
            >
              <Download className="h-3.5 w-3.5" />
              下载
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4" data-testid="file-preview-markdown">
          {markdownSource ? (
            <pre className="font-mono text-xs whitespace-pre-wrap">{content}</pre>
          ) : (
            <MarkdownRenderer content={content} />
          )}
        </div>
      </div>
    )
  }

  /** 音视频播放（浏览器原生；取流 URL 与图片同为 file-content 端点） */
  if (previewType === 'media') {
    const isVideo = VIDEO_EXTENSIONS.has(extractExtension(filePath))
    return (
      <div className={cn('flex h-full flex-col', className)} data-testid="file-preview-media">
        <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <span className="text-muted-foreground text-xs">{isVideo ? '视频' : '音频'}</span>
        </div>
        <div className="flex flex-1 items-center justify-center overflow-auto p-4">
          {fileUrl ? (
            isVideo ? (
              <video src={fileUrl} controls className="max-h-full max-w-full" />
            ) : (
              <audio src={fileUrl} controls className="w-full max-w-xl" />
            )
          ) : (
            <div className="text-muted-foreground text-xs">
              缺少文件坐标，无法取流播放
            </div>
          )}
        </div>
      </div>
    )
  }

  /** Excel 预览（解析与取流归 ExcelBody，见上） */
  if (previewType === 'excel') {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <span className="text-muted-foreground text-xs">Excel（只读）</span>
        </div>
        <div className="min-h-0 flex-1">
          <ExcelBody url={fileUrl || undefined} content={content} />
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
          <PrismSyntaxHighlighter
            language={language}
            // eslint-disable-next-line no-restricted-syntax -- react-syntax-highlighter 官方 API style prop（主题高亮配色对象直传），非 DOM 内联样式
            style={codeHighlightStyle}
            showLineNumbers={true}
            wrapLongLines={true}
            lineProps={HIGHLIGHTER_LINE_PROPS}
            customStyle={{
              margin: 0,
              borderRadius: 0,
              fontSize: '0.8125rem',
              background: 'var(--code-bg)',
              color: 'var(--code-text)',
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
          </PrismSyntaxHighlighter>
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
