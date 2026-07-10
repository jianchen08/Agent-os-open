/** 代码编辑器组件 在工作区面板中提供带语法高亮的代码编辑功能。 */

import { Save, AlertTriangle, FileText, Eye, Pencil, RefreshCw, Quote } from 'lucide-react'
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { LobeChatMarkdown } from '../chat/LobeChatMarkdown'
import { cn } from '@/lib/utils'
import { useChatInputStore } from '@/stores/chatInputStore'
import { subscribeFileChange, unsubscribeFileChange } from '@/stores/fileEditorRegistry'

/** Markdown 扩展名集合 */
const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown'])

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

/** 大文件阈值（1MB） */
const LARGE_FILE_THRESHOLD = 1_000_000

/** 可编辑的文件扩展名集合 */
const EDITABLE_EXTENSIONS = new Set([
  '.py', '.js', '.jsx', '.ts', '.tsx', '.json', '.yaml', '.yml', '.toml',
  '.xml', '.html', '.htm', '.css', '.scss', '.less', '.vue', '.svelte',
  '.rs', '.go', '.java', '.kt', '.c', '.cpp', '.h', '.hpp', '.cs',
  '.rb', '.php', '.swift', '.sh', '.bash', '.bat', '.ps1', '.sql',
  '.r', '.lua', '.pl', '.dart', '.zig', '.ini', '.cfg', '.conf',
  '.env', '.properties', '.log', '.csv', '.tsv', '.graphql', '.gql',
  '.proto', '.cmake', '.gradle', '.txt', '.md', '.markdown',
])

/** 文件扩展名 → Prism 语言映射 */
const EXTENSION_TO_LANGUAGE: Record<string, string> = {
  '.py': 'python',
  '.js': 'javascript',
  '.jsx': 'jsx',
  '.ts': 'typescript',
  '.tsx': 'tsx',
  '.json': 'json',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.xml': 'xml',
  '.html': 'html',
  '.htm': 'html',
  '.css': 'css',
  '.scss': 'scss',
  '.less': 'less',
  '.vue': 'html',
  '.svelte': 'html',
  '.rs': 'rust',
  '.go': 'go',
  '.java': 'java',
  '.kt': 'kotlin',
  '.c': 'c',
  '.cpp': 'cpp',
  '.h': 'c',
  '.hpp': 'cpp',
  '.cs': 'csharp',
  '.rb': 'ruby',
  '.php': 'php',
  '.swift': 'swift',
  '.sh': 'bash',
  '.bash': 'bash',
  '.bat': 'batch',
  '.ps1': 'powershell',
  '.sql': 'sql',
  '.r': 'r',
  '.lua': 'lua',
  '.pl': 'perl',
  '.dart': 'dart',
  '.zig': 'zig',
  '.ini': 'ini',
  '.cfg': 'ini',
  '.conf': 'ini',
  '.env': 'bash',
  '.properties': 'properties',
  '.log': 'text',
  '.csv': 'text',
  '.tsv': 'text',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.proto': 'protobuf',
  '.cmake': 'cmake',
  '.gradle': 'groovy',
  '.txt': 'text',
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.svg': 'xml',
  '.gitignore': 'text',
  '.dockerignore': 'text',
  '.editorconfig': 'ini',
  '.eslintrc': 'json',
  '.prettierrc': 'json',
  '.dockerfile': 'docker',
  '.makefile': 'makefile',
  '.map': 'json',
  '.lock': 'text',
}

/** 自动刷新配置 */
export interface AutoRefreshConfig {
  /** 是否启用自动刷新 */
  enabled: boolean
  /** 刷新间隔（毫秒，默认 3000） */
  interval?: number
}

/** CodeEditor 组件属性 */
export interface CodeEditorProps {
  /** 文件路径（如 src/main.py） */
  filePath: string
  /** 文件内容 */
  content: string
  /** 文件大小（字节） */
  size?: number
  /** 保存回调 */
  onSave: (content: string) => Promise<boolean>
  /** 是否只读模式 */
  readOnly?: boolean
  /** 自定义类名 */
  className?: string
  /** 可选的 Tab ID，用于接收实时刷新事件 */
  tabId?: string
  /** 自动刷新配置 */
  autoRefresh?: AutoRefreshConfig
}

// ────────────────────────────────────────────
// 选中引用浮动按钮：辅助工具
// ────────────────────────────────────────────

/** 浮动「引用」按钮样式（一次性注入到 document.head） */
let _floatingQuoteStyleInjected = false
function injectFloatingQuoteStyles(): void {
  if (_floatingQuoteStyleInjected || typeof document === 'undefined') return
  _floatingQuoteStyleInjected = true
  const style = document.createElement('style')
  style.textContent = `@keyframes floatingQuoteIn{from{opacity:0;transform:translate(-50%,-100%) scale(0.95)}to{opacity:1;transform:translate(-50%,-100%) scale(1)}}`
  document.head.appendChild(style)
}

/** 从代码内容中检测选中文字所在的函数名 从选中起始行向上逐行扫描，匹配常见函数/类定义模式（JS/TS/Python/Go/Rust 等）。 */
function detectFunctionName(code: string, targetLine: number): string | null {
  const lines = code.split('\n')
  const startLine = Math.max(0, Math.min(targetLine - 1, lines.length - 1))
  const patterns: RegExp[] = [
    /(?:export\s+)?(?:async\s+)?function\s+(\w+)/,
    /(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\))?\s*=>/,
    /(?:export\s+)?class\s+(\w+)/,
    /(?:public|private|protected)\s+(?:async\s+)?(?:static\s+)?(\w+)\s*\(/,
    /def\s+(\w+)/,
    /func\s+(\w+)/,
    /fn\s+(\w+)/,
  ]
  for (let i = startLine; i >= 0; i--) {
    const line = lines[i]
    for (const pattern of patterns) {
      const match = line.match(pattern)
      if (match) return match[1]
    }
  }
  return null
}

/** 浮动按钮状态 */
interface FloatingQuoteState {
  visible: boolean
  selectedText: string
  lineRange: { start: number; end: number } | null
  position: { x: number; y: number }
}

const FLOATING_QUOTE_INITIAL: FloatingQuoteState = {
  visible: false,
  selectedText: '',
  lineRange: null,
  position: { x: 0, y: 0 },
}

/** 浮动「引用」按钮（Notion / Google Docs 风格） 选中文字时弹出，点击「引用」把内容塞到 Chat 输入框；按 Esc 关闭。 */
function FloatingQuoteButton({
  position,
  onQuote,
  onClose,
}: {
  position: { x: number; y: number }
  onQuote: () => void
  onClose: () => void
}) {
  return (
    <div
      className="pointer-events-auto absolute z-50"
      style={{
        left: position.x,
        top: position.y,
        transform: 'translate(-50%, -100%)',
        animation: 'floatingQuoteIn 150ms ease-out',
      }}
    >
      <div
        className="flex items-center gap-1 rounded-lg border border-[var(--floating-quote-border,rgba(255,255,255,0.1))] bg-[var(--floating-quote-bg,#2a2a2a)] px-1.5 py-1 shadow-[var(--floating-quote-shadow,0_4px_12px_rgba(0,0,0,0.3))]"
      >
        <button
          onClick={onQuote}
          className="flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium text-[var(--floating-quote-text,#fff)] transition-colors hover:bg-[var(--floating-quote-hover-bg,rgba(255,255,255,0.1))]"
          title="引用到对话 (Enter)"
        >
          <Quote className="h-3.5 w-3.5" />
          <span>引用</span>
        </button>
        <button
          onClick={onClose}
          className="flex h-5 w-5 items-center justify-center rounded text-xs text-[var(--floating-quote-text,#fff)] opacity-60 transition-opacity hover:opacity-100"
          title="关闭 (Esc)"
          aria-label="关闭"
        >
          ×
        </button>
      </div>
    </div>
  )
}

/** 从文件名提取扩展名 */
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

/** 根据文件名获取 Prism 语言标识 */
function getLanguage(fileName: string): string {
  const ext = extractExtension(fileName)
  return EXTENSION_TO_LANGUAGE[ext] ?? 'text'
}

/** 判断文件是否可编辑 */
function isEditable(fileName: string): boolean {
  const ext = extractExtension(fileName)
  return EDITABLE_EXTENSIONS.has(ext)
}

/** 代码编辑器组件 功能： */
export function CodeEditor({
  filePath,
  content: initialContent,
  size,
  onSave,
  readOnly = false,
  className,
  tabId,
  autoRefresh,
}: CodeEditorProps) {
  const [localContent, setLocalContent] = useState(initialContent)
  const [isDirty, setIsDirty] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  /** 预览/编辑模式切换，默认预览模式 */
  const [isPreview, setIsPreview] = useState(true)
  /** 外部变更提示状态 */
  const [externalChange, setExternalChange] = useState<{ content: string; size?: number } | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const preRef = useRef<HTMLPreElement>(null)
  const isDirtyRef = useRef(isDirty)

  /** 同步 isDirty 到 ref，供事件监听使用 */
  useEffect(() => {
    isDirtyRef.current = isDirty
  }, [isDirty])

  const fileName = useMemo(() => {
    const lastSlash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
    return filePath.substring(lastSlash + 1)
  }, [filePath])

  const language = useMemo(() => getLanguage(filePath), [filePath])
  const editable = useMemo(() => !readOnly && isEditable(filePath), [readOnly, filePath])
  const isMarkdownFile = useMemo(() => {
    const ext = extractExtension(filePath)
    return MARKDOWN_EXTENSIONS.has(ext)
  }, [filePath])

  const isLargeFile = useMemo(
    () => (size ?? initialContent.length) > LARGE_FILE_THRESHOLD,
    [size, initialContent.length],
  )

  /** 内容容器的 ref，用于保存和恢复滚动位置 */
  const contentContainerRef = useRef<HTMLDivElement>(null)

  // ────────────────────────────────────────────
  // 选中引用浮动按钮
  // ────────────────────────────────────────────
  // 用户在预览模式 / 只读模式下选中文字时，弹出「引用」按钮；
  // 点击后把带行号和函数名的格式化文本塞进 Chat 输入框（chatInputStore.requestInsert）。
  // 注意：编辑模式（textarea）使用浏览器原生选择/复制，不挂浮动按钮，避免遮挡编辑光标。
  const requestInsert = useChatInputStore((s) => s.requestInsert)
  const [floatingQuote, setFloatingQuote] = useState<FloatingQuoteState>(FLOATING_QUOTE_INITIAL)
  const justSelectedRef = useRef(false)

  useEffect(() => {
    injectFloatingQuoteStyles()
  }, [])

  /** 处理预览区文字选中，计算行号范围并弹出浮动按钮 */
  const handlePreviewMouseUp = useCallback(() => {
    const selection = window.getSelection()
    if (!selection || selection.isCollapsed) return
    const selectedText = selection.toString().trim()
    if (!selectedText) return
    const container = contentContainerRef.current
    if (!container) return
    const range = selection.getRangeAt(0)
    const rect = range.getBoundingClientRect()
    const containerRect = container.getBoundingClientRect()

    // 1) 优先用 DOM 选区计算行号（适用于 SyntaxHighlighter 多 span 的结构）
    let lineRange: { start: number; end: number } | null = null
    try {
      const preRange = document.createRange()
      preRange.selectNodeContents(container)
      preRange.setEnd(range.startContainer, range.startOffset)
      const textBefore = preRange.toString()
      const startLine = (textBefore.match(/\n/g) || []).length + 1
      const fullText = textBefore + selectedText
      const endLine = (fullText.match(/\n/g) || []).length + 1
      lineRange = { start: startLine, end: endLine }
    } catch {
      /* DOM 异常，fallback 到文本匹配 */
    }

    // 2) DOM 选区失败时，用 indexOf 在 localContent 中定位
    if (!lineRange) {
      const sourceIndex = localContent.indexOf(selectedText)
      if (sourceIndex >= 0) {
        const before = localContent.substring(0, sourceIndex)
        const startLine = (before.match(/\n/g) || []).length + 1
        const including = localContent.substring(0, sourceIndex + selectedText.length)
        const endLine = (including.match(/\n/g) || []).length + 1
        lineRange = { start: startLine, end: endLine }
      }
    }

    justSelectedRef.current = true
    setFloatingQuote({
      visible: true,
      selectedText,
      lineRange,
      position: {
        x: rect.left - containerRect.left + rect.width / 2 + container.scrollLeft,
        y: rect.top - containerRect.top - 8 + container.scrollTop,
      },
    })
  }, [localContent])

  /** 点击预览区空白处关闭浮动按钮（但跳过刚刚的选中事件，避免选中后立即被关闭） */
  const handlePreviewClick = useCallback(() => {
    if (justSelectedRef.current) {
      justSelectedRef.current = false
      return
    }
    if (floatingQuote.visible) {
      setFloatingQuote((prev) => ({ ...prev, visible: false }))
    }
  }, [floatingQuote.visible])

  /** 引用选中文字到 Chat 输入框 */
  const handleQuote = useCallback(() => {
    if (!floatingQuote.selectedText) return
    const lineRange = floatingQuote.lineRange
    const funcName = lineRange ? detectFunctionName(localContent, lineRange.start) : null
    const lineInfo = lineRange
      ? lineRange.start === lineRange.end
        ? `L${lineRange.start}`
        : `L${lineRange.start}-${lineRange.end}`
      : ''

    let quotedFileInfo: string
    if (funcName && lineInfo) {
      quotedFileInfo = `${filePath}:${funcName}(${lineInfo})`
    } else if (funcName) {
      quotedFileInfo = `${filePath}:${funcName}`
    } else if (lineInfo) {
      quotedFileInfo = `${filePath}:${lineInfo}`
    } else {
      quotedFileInfo = filePath
    }

    let formattedQuotedText = floatingQuote.selectedText
    if (lineRange) {
      formattedQuotedText = floatingQuote.selectedText
        .split('\n')
        .map((line, i) => `L${lineRange.start + i}: ${line}`)
        .join('\n')
    }

    // 引用文本格式：「${quotedFileInfo}:\n${quotedText}」
    requestInsert(`「${quotedFileInfo}:\n${formattedQuotedText}」`)
    setFloatingQuote((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()
  }, [floatingQuote.selectedText, floatingQuote.lineRange, filePath, localContent, requestInsert])

  /** Esc 关闭浮动按钮 */
  useEffect(() => {
    if (!floatingQuote.visible) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setFloatingQuote((prev) => ({ ...prev, visible: false }))
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [floatingQuote.visible])

  /** 当切换文件时重置浮动按钮 */
  useEffect(() => {
    setFloatingQuote(FLOATING_QUOTE_INITIAL)
  }, [filePath])

  /** 当外部 content 变化时同步（如文件重新加载） */
  useEffect(() => {
    setLocalContent(initialContent)
    setIsDirty(false)
    setSaveError(null)
    setExternalChange(null)
  }, [initialContent])

  /** 保存当前滚动位置 */
  const saveScrollPosition = useCallback(() => {
    const container = contentContainerRef.current
    if (!container) return null
    return {
      scrollTop: container.scrollTop,
      scrollLeft: container.scrollLeft,
    }
  }, [])

  /** 恢复滚动位置 */
  const restoreScrollPosition = useCallback((pos: { scrollTop: number; scrollLeft: number } | null) => {
    if (!pos) return
    const container = contentContainerRef.current
    if (!container) return
    requestAnimationFrame(() => {
      container.scrollTop = pos.scrollTop
      container.scrollLeft = pos.scrollLeft
    })
  }, [])

  /** 订阅文件外部变更事件 当文件被外部修改时，如果当前没有未保存的修改，自动同步新内容； */
  useEffect(() => {
    if (!tabId) return

    const handleFileChange = (newContent: string, newSize?: number) => {
      if (isDirtyRef.current) {
        // 有未保存修改，显示覆盖提示
        setExternalChange({ content: newContent, size: newSize })
      } else {
        // 无未保存修改，直接同步，保持滚动位置
        const scrollPos = saveScrollPosition()
        setLocalContent(newContent)
        setIsDirty(false)
        setSaveError(null)
        restoreScrollPosition(scrollPos)
      }
    }

    subscribeFileChange(tabId, handleFileChange)
    return () => unsubscribeFileChange(tabId, handleFileChange)
  }, [tabId, saveScrollPosition, restoreScrollPosition])

  /** 处理外部变更覆盖 用户确认用外部修改覆盖当前未保存的内容。 */
  const handleAcceptExternalChange = useCallback(() => {
    if (externalChange) {
      const scrollPos = saveScrollPosition()
      setLocalContent(externalChange.content)
      setIsDirty(false)
      setSaveError(null)
      setExternalChange(null)
      restoreScrollPosition(scrollPos)
    }
  }, [externalChange, saveScrollPosition, restoreScrollPosition])

  /** 忽略外部变更 用户选择保留当前修改，忽略外部变更。 */
  const handleIgnoreExternalChange = useCallback(() => {
    setExternalChange(null)
  }, [])

  /** 处理保存 */
  const handleSave = useCallback(async () => {
    if (!isDirty || isSaving) return
    setIsSaving(true)
    setSaveError(null)
    try {
      const success = await onSave(localContent)
      if (success) {
        setIsDirty(false)
      } else {
        setSaveError('保存失败')
      }
    } catch {
      setSaveError('保存失败，请重试')
    } finally {
      setIsSaving(false)
    }
  }, [isDirty, isSaving, localContent, onSave])

  /** Ctrl+S 快捷键 */
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        if (editable && isDirty && !isSaving) {
          handleSave()
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [editable, isDirty, isSaving, handleSave])

  /** 同步 textarea 滚动位置到 pre 高亮层 */
  const handleScroll = useCallback(() => {
    const textarea = textareaRef.current
    const pre = preRef.current
    if (textarea && pre) {
      pre.scrollTop = textarea.scrollTop
      pre.scrollLeft = textarea.scrollLeft
    }
  }, [])

  /** 处理文本变更 */
  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      if (!editable) return
      const newContent = e.target.value
      setLocalContent(newContent)
      setIsDirty(newContent !== initialContent)
      setSaveError(null)
    },
    [editable, initialContent],
  )

  // 大文件提示
  if (isLargeFile && editable) {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
          <FileText className="text-muted-foreground h-4 w-4" />
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <span className="text-muted-foreground ml-2 text-xs">
            ({((size ?? initialContent.length) / 1024).toFixed(1)} KB)
          </span>
        </div>
        <div className="flex flex-1 items-center justify-center p-8">
          <div className="text-center">
            <AlertTriangle className="mx-auto mb-3 h-10 w-10 text-amber-500" />
            <p className="text-foreground mb-1 text-sm font-medium">文件过大，无法编辑</p>
            <p className="text-muted-foreground text-xs">
              文件大小超过 1MB（当前 {(size ?? initialContent.length / 1024).toFixed(1)} KB），
              为保证编辑性能，请使用本地编辑器修改。
            </p>
          </div>
        </div>
      </div>
    )
  }

  // 不可编辑文件：始终使用只读预览模式，不显示切换按钮
  if (!editable) {
    return (
      <div className={cn('flex h-full flex-col', className)}>
        <div className="border-border bg-muted/30 flex items-center gap-2 border-b px-4 py-2">
          <FileText className="text-muted-foreground h-4 w-4" />
          <span className="text-foreground text-sm font-medium">{fileName}</span>
          <span className="text-muted-foreground ml-2 text-xs">（只读预览）</span>
        </div>
        <div
          ref={contentContainerRef}
          className="relative min-h-0 flex-1 overflow-auto"
          onMouseUp={handlePreviewMouseUp}
          onClick={handlePreviewClick}
        >
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
            {localContent}
          </SyntaxHighlighter>
          {floatingQuote.visible && (
            <FloatingQuoteButton
              position={floatingQuote.position}
              onQuote={handleQuote}
              onClose={() => setFloatingQuote((prev) => ({ ...prev, visible: false }))}
            />
          )}
        </div>
      </div>
    )
  }

  // 可编辑文件：支持预览/编辑模式切换
  return (
    <div className={cn('flex h-full flex-col', className)}>
      {/* 外部变更提示条 */}
      {externalChange && (
        <div className="flex items-center justify-between border-b border-amber-500/30 bg-amber-500/10 px-4 py-2">
          <div className="flex items-center gap-2">
            <RefreshCw className="h-3.5 w-3.5 text-amber-600" />
            <span className="text-xs text-amber-700">
              文件已被外部修改
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleAcceptExternalChange}
              className="flex items-center gap-1 rounded-md bg-amber-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-amber-700"
              title="用外部修改覆盖当前内容"
            >
              <RefreshCw className="h-3 w-3" />
              覆盖
            </button>
            <button
              onClick={handleIgnoreExternalChange}
              className="flex items-center gap-1 rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              title="保留当前修改"
            >
              忽略
            </button>
          </div>
        </div>
      )}

      {/* 工具栏 */}
      <div className="border-border bg-muted/30 flex items-center justify-between border-b px-4 py-2">
        <div className="flex items-center gap-2">
          <FileText className="text-muted-foreground h-4 w-4" />
          <span className="text-foreground text-sm font-medium">
            {fileName}
            {!isPreview && isDirty && <span className="text-amber-500 ml-0.5">*</span>}
          </span>
          <span className="text-muted-foreground text-xs lowercase">{language}</span>
        </div>
        <div className="flex items-center gap-2">
          {saveError && (
            <span className="text-destructive text-xs">{saveError}</span>
          )}
          {/* 编辑模式下显示保存按钮 */}
          {!isPreview && (
            <button
              onClick={handleSave}
              disabled={!isDirty || isSaving}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs transition-colors',
                isDirty && !isSaving
                  ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                  : 'bg-muted text-muted-foreground cursor-not-allowed',
              )}
              title="保存 (Ctrl+S)"
            >
              <Save className="h-3.5 w-3.5" />
              {isSaving ? '保存中...' : '保存'}
            </button>
          )}
          {/* 预览/编辑切换按钮 */}
          <button
            onClick={() => setIsPreview((prev) => !prev)}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
              !isPreview
                ? 'bg-blue-600 text-white hover:bg-blue-700'
                : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
            title={isPreview ? '切换到编辑模式' : '切换到预览模式'}
          >
            {!isPreview ? (
              <>
                <Eye className="h-3.5 w-3.5" />
                <span>查看</span>
              </>
            ) : (
              <>
                <Pencil className="h-3.5 w-3.5" />
                <span>编辑</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* 内容区域：根据模式切换渲染 */}
      {isPreview ? (
        /* 预览模式：Markdown 文件使用 LobeChatMarkdown 渲染，其他使用 SyntaxHighlighter */
        isMarkdownFile ? (
          <div
            ref={contentContainerRef}
            className="prose prose-sm dark:prose-invert max-w-none min-h-0 flex-1 overflow-auto p-4 relative"
            onMouseUp={handlePreviewMouseUp}
            onClick={handlePreviewClick}
          >
            <LobeChatMarkdown content={localContent} />
            {floatingQuote.visible && (
              <FloatingQuoteButton
                position={floatingQuote.position}
                onQuote={handleQuote}
                onClose={() => setFloatingQuote((prev) => ({ ...prev, visible: false }))}
              />
            )}
          </div>
        ) : (
          <div
            ref={contentContainerRef}
            className="relative min-h-0 flex-1 overflow-auto"
            onMouseUp={handlePreviewMouseUp}
            onClick={handlePreviewClick}
          >
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
              {localContent}
            </SyntaxHighlighter>
            {floatingQuote.visible && (
              <FloatingQuoteButton
                position={floatingQuote.position}
                onQuote={handleQuote}
                onClose={() => setFloatingQuote((prev) => ({ ...prev, visible: false }))}
              />
            )}
          </div>
        )
      ) : (
        /* 编辑模式：textarea + 语法高亮背景 */
        <div ref={contentContainerRef} className="relative min-h-0 flex-1 overflow-hidden">
          {/* 语法高亮底层（用于视觉参考，实际编辑在 textarea 上层） */}
          <pre
            ref={preRef}
            className="pointer-events-none absolute inset-0 scrollbar-transparent p-4 text-sm"
            style={{
              background: 'var(--code-bg, #1e1e1e)',
              color: 'var(--code-text, #d4d4d4)',
              fontFamily:
                'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
              fontSize: '0.8125rem',
              lineHeight: '1.6',
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordWrap: 'break-word',
              overflow: 'auto',
              tabSize: 4,
            }}
            aria-hidden="true"
          >
            {localContent}
          </pre>

          {/* 文本编辑区域 */}
          <textarea
            ref={textareaRef}
            value={localContent}
            onChange={handleChange}
            onScroll={handleScroll}
            className="absolute inset-0 h-full w-full resize-none p-4 text-sm"
            style={{
              background: 'transparent',
              color: 'transparent',
              caretColor: 'var(--code-text, #d4d4d4)',
              fontFamily:
                'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
              fontSize: '0.8125rem',
              lineHeight: '1.6',
              border: 'none',
              outline: 'none',
              whiteSpace: 'pre-wrap',
              wordWrap: 'break-word',
              overflow: 'auto',
              margin: 0,
              tabSize: 4,
            }}
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
          />
        </div>
      )}
    </div>
  )
}
