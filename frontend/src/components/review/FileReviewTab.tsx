/**
 * FileReviewTab - 文件审批 Tab 组件
 *
 * 全屏文件查看/编辑器，支持：
 *   - 根据扩展名自动选择 Markdown / 代码高亮 / 纯文本渲染
 *   - 查看/编辑模式切换
 *   - 文字选中后弹出浮动按钮「引用」「批注」
 *   - 引用时自动检测函数名，提供更精确的上下文（文件路径:函数名(行号)）
 *   - 批注流程：选中 → 输入建议 → 保存到列表 → 批量提交
 *   - Choice 模式下底部显示审批通过/驳回按钮
 *   - Conversation 模式下全屏展示文件，对话在 Chat 面板进行
 */

import { MessageSquare, CheckCircle2, XCircle, Quote, X, FileText, Code2, File, Pencil, Eye, Save, Trash2, Send } from 'lucide-react'
import React, { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { cn } from '@/lib/utils'
import { LobeChatMarkdown } from '../chat/LobeChatMarkdown'
import { CodeBlock } from '../chat/markdown/CodeBlock'
import { AnnotationBubble } from './AnnotationBubble'

export interface FileReviewTabProps {
  /** 文件内容映射 {文件路径: 文件内容} */
  fileContents: Record<string, string>
  /** 交互请求 ID */
  requestId: string
  /** 交互模式 */
  mode: 'choice' | 'conversation'
  /** 交互标题 */
  title: string
  /** 管道 ID（用于发送消息） */
  pipelineId: string
  /** 选项列表（choice 模式） */
  options?: Array<{ id: string; label: string }>
  /** 回调：发送对话消息（引用文字到 Chat 面板） */
  onSendMessage: (message: string, quotedText?: string, quotedFile?: string) => void
  /** 回调：提交审批结果 */
  onSubmitReview: (requestId: string, response: 'approved' | 'denied', feedback?: string) => void
  /** 回调：文件内容被编辑 */
  onFileContentChange?: (filePath: string, newContent: string) => void
  /** 回调：保存文件内容到后端 */
  onSaveFile?: (filePath: string, content: string) => Promise<boolean>
}

// ────────────────────────────────────────────
// 文件类型 → 语言映射
// ────────────────────────────────────────────

type FileRenderType = 'markdown' | 'code' | 'text'

/** 文件扩展名 → Prism 语言标识映射（覆盖 20+ 常见语言） */
const EXTENSION_LANGUAGE_MAP: Record<string, string> = {
  '.ts': 'typescript', '.tsx': 'tsx', '.js': 'javascript', '.jsx': 'jsx',
  '.vue': 'javascript', '.svelte': 'javascript', '.html': 'html', '.css': 'css',
  '.scss': 'scss', '.less': 'less', '.py': 'python', '.rb': 'ruby', '.php': 'php',
  '.pl': 'perl', '.lua': 'lua', '.r': 'r', '.go': 'go', '.rs': 'rust', '.java': 'java',
  '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp', '.cs': 'csharp', '.swift': 'swift',
  '.kt': 'kotlin', '.scala': 'scala', '.dart': 'dart', '.json': 'json', '.yaml': 'yaml',
  '.yml': 'yaml', '.toml': 'toml', '.xml': 'xml', '.ini': 'ini', '.env': 'bash',
  '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.fish': 'bash', '.ps1': 'powershell',
  '.bat': 'batch', '.cmd': 'batch', '.sql': 'sql', '.md': 'markdown', '.markdown': 'markdown',
  '.txt': 'text', '.log': 'text', '.dockerfile': 'docker', '.makefile': 'makefile',
}

const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown'])

/**
 * 根据文件扩展名判断渲染类型
 */
function getFileTypeInfo(filePath: string): { renderType: FileRenderType; language: string } {
  const lowerPath = filePath.toLowerCase()
  const basename = lowerPath.split('/').pop() ?? ''
  if (basename === 'dockerfile') return { renderType: 'code', language: 'docker' }
  if (basename === 'makefile') return { renderType: 'code', language: 'makefile' }
  if (basename === '.gitignore' || basename === '.env') return { renderType: 'code', language: 'bash' }
  const dotIndex = basename.lastIndexOf('.')
  if (dotIndex === -1) return { renderType: 'text', language: 'text' }
  const ext = basename.slice(dotIndex)
  if (MARKDOWN_EXTENSIONS.has(ext)) return { renderType: 'markdown', language: 'markdown' }
  const language = EXTENSION_LANGUAGE_MAP[ext]
  if (language) return { renderType: 'code', language }
  return { renderType: 'text', language: 'text' }
}

function getFileName(filePath: string): string {
  return filePath.split('/').pop() ?? filePath
}

// ────────────────────────────────────────────
// 子组件：浮动引用按钮
// ────────────────────────────────────────────

let _floatingStyleInjected = false
function injectFloatingQuoteStyles() {
  if (_floatingStyleInjected || typeof document === 'undefined') return
  _floatingStyleInjected = true
  const style = document.createElement('style')
  style.textContent = `@keyframes floatingQuoteIn{from{opacity:0;transform:translate(-50%,-100%) scale(0.95)}to{opacity:1;transform:translate(-50%,-100%) scale(1)}}`
  document.head.appendChild(style)
}

interface FloatingQuoteButtonProps {
  position: { x: number; y: number }
  onQuote: () => void
  onAnnotate?: () => void
  onClose: () => void
}

/**
 * 浮动引用按钮（Notion / Google Docs 风格）
 */
function FloatingQuoteButton({ position, onQuote, onAnnotate, onClose }: FloatingQuoteButtonProps) {
  useEffect(() => { injectFloatingQuoteStyles() }, [])
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Enter') { e.preventDefault(); onQuote() }
      else if (e.key === 'Escape') { onClose() }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onQuote, onClose])

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
      <div className="flex items-center gap-1 rounded-lg bg-[var(--floating-quote-bg)] px-1.5 py-1 shadow-[var(--floating-quote-shadow)] border border-[var(--floating-quote-border)]">
        <button
          onClick={onQuote}
          className="flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium text-[var(--floating-quote-text)] transition-colors hover:bg-[var(--floating-quote-hover-bg)]"
          title="引用到对话 (Enter)"
        >
          <Quote className="h-3.5 w-3.5" />
          <span>引用</span>
        </button>
        {onAnnotate && (
          <>
            <div className="h-4 w-px bg-border" />
            <button
              onClick={onAnnotate}
              className="flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium text-[var(--floating-quote-text)] transition-colors hover:bg-[var(--floating-quote-hover-bg)]"
              title="添加批注"
            >
              <MessageSquare className="h-3.5 w-3.5" />
              <span>批注</span>
            </button>
          </>
        )}
      </div>
    </div>
  )
}

// ────────────────────────────────────────────
// 批注数据类型
// ────────────────────────────────────────────

/** 已保存的批注数据结构 */
interface SavedAnnotation {
  /** 唯一标识 */
  id: string
  /** 选中的原文 */
  selectedText: string
  /** 用户输入的修改建议 */
  suggestion: string
  /** 批注所属文件路径 */
  filePath: string
  /** 选中文字在文件中的行号范围 */
  lineRange: { start: number; end: number } | null
}

/**
 * 从代码内容中检测选中文字所在的函数名
 * 从选中行向上逐行扫描，匹配常见函数/类定义模式
 *
 * @param code - 完整文件内容
 * @param targetLine - 选中文字起始行号（1-based）
 * @returns 检测到的函数名，未找到返回 null
 */
function detectFunctionName(code: string, targetLine: number): string | null {
  const lines = code.split('\n')
  const startLine = Math.max(0, Math.min(targetLine - 1, lines.length - 1))

  // 函数定义正则模式（按优先级从高到低排序）
  const patterns: RegExp[] = [
    /(?:export\s+)?(?:async\s+)?function\s+(\w+)/,                                    // function xxx / async function xxx
    /(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\))?\s*=>/,  // const xxx = () =>
    /(?:export\s+)?class\s+(\w+)/,                                                     // class xxx
    /(?:public|private|protected)\s+(?:async\s+)?(?:static\s+)?(\w+)\s*\(/,             // public xxx(
    /def\s+(\w+)/,                                                                      // def xxx (Python)
    /func\s+(\w+)/,                                                                     // func xxx (Go/Swift)
    /fn\s+(\w+)/,                                                                       // fn xxx (Rust)
  ]

  // 从选中行向上扫描，返回最近的函数名
  for (let i = startLine; i >= 0; i--) {
    const line = lines[i]
    for (const pattern of patterns) {
      const match = line.match(pattern)
      if (match) return match[1]
    }
  }

  return null
}

// ────────────────────────────────────────────
// 主组件
// ────────────────────────────────────────────

/**
 * FileReviewTab - 全屏文件审批查看器
 *
 * Conversation 模式：全屏展示文件，选中文字引用到 Chat 面板
 * Choice 模式：全屏展示文件 + 底部审批按钮
 */
export function FileReviewTab({
  fileContents,
  requestId,
  mode,
  title,
  options,
  onSendMessage,
  onSubmitReview,
  onFileContentChange,
  onSaveFile,
}: FileReviewTabProps) {
  const filePaths = useMemo(() => Object.keys(fileContents), [fileContents])
  const [activeFileIndex, setActiveFileIndex] = useState(0)
  const activeFilePath = filePaths[activeFileIndex] ?? ''
  const activeFileContent = fileContents[activeFilePath] ?? ''
  const activeFileInfo = useMemo(() => getFileTypeInfo(activeFilePath), [activeFilePath])

  const [isEditing, setIsEditing] = useState(false)
  const [editableContents, setEditableContents] = useState<Record<string, string>>({})
  const [isSaving, setIsSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)

  const displayContent = useMemo(() => {
    return editableContents[activeFilePath] ?? activeFileContent
  }, [editableContents, activeFilePath, activeFileContent])

  const editableContent = useMemo(() => {
    return editableContents[activeFilePath] ?? activeFileContent
  }, [editableContents, activeFilePath, activeFileContent])

  // 浮动引用按钮
  const contentRef = useRef<HTMLDivElement>(null)
  const [floatingButton, setFloatingButton] = useState<{
    visible: boolean
    selectedText: string
    selectedLineRange: { start: number; end: number } | null
    position: { x: number; y: number }
  }>({ visible: false, selectedText: '', selectedLineRange: null, position: { x: 0, y: 0 } })

  // 批注气泡状态（用于 AnnotationBubble 的显示/隐藏）
  const [annotationBubble, setAnnotationBubble] = useState<{
    visible: boolean
    selectedText: string
    position: { x: number; y: number }
    lineRange: { start: number; end: number } | null
  }>({ visible: false, selectedText: '', position: { x: 0, y: 0 }, lineRange: null })

  // 已保存的批注列表
  const [annotations, setAnnotations] = useState<SavedAnnotation[]>([])

  // Choice 模式审批
  const [feedbackText, setFeedbackText] = useState('')

  // 文字选中处理
  const justSelectedRef = useRef(false)

  const handleMouseUp = useCallback(() => {
    const selection = window.getSelection()
    if (!selection || selection.isCollapsed || !selection.toString().trim()) return
    const selectedText = selection.toString().trim()
    if (!selectedText) return
    const range = selection.getRangeAt(0)
    const rect = range.getBoundingClientRect()
    const containerRect = contentRef.current?.getBoundingClientRect()
    if (!containerRect) return

    let lineRange: { start: number; end: number } | null = null
    try {
      const preRange = document.createRange()
      preRange.selectNodeContents(contentRef.current!)
      preRange.setEnd(range.startContainer, range.startOffset)
      const textBefore = preRange.toString()
      const startLine = (textBefore.match(/\n/g) || []).length + 1
      const fullText = textBefore + selectedText
      const endLine = (fullText.match(/\n/g) || []).length + 1
      lineRange = { start: startLine, end: endLine }
    } catch { /* ignore */ }

    justSelectedRef.current = true
    setFloatingButton({
      visible: true,
      selectedText,
      selectedLineRange: lineRange,
      position: {
        x: rect.left - containerRect.left + rect.width / 2 + (contentRef.current?.scrollLeft ?? 0),
        y: rect.top - containerRect.top - 8 + (contentRef.current?.scrollTop ?? 0),
      },
    })
  }, [])

  const handleContentClick = useCallback(() => {
    if (justSelectedRef.current) {
      justSelectedRef.current = false
      return
    }
    if (floatingButton.visible) {
      setFloatingButton((prev) => ({ ...prev, visible: false }))
    }
    if (annotationBubble.visible) {
      setAnnotationBubble((prev) => ({ ...prev, visible: false }))
    }
  }, [floatingButton.visible, annotationBubble.visible])

  /**
   * 引用选中文字到 Chat 输入框
   * 增强版：自动检测选中文字所在的函数名，附带上下文信息
   * 格式：文件路径:函数名(行号范围) 或 文件路径:行号范围
   */
  const handleQuote = useCallback(() => {
    if (!floatingButton.selectedText) return
    const quotedText = floatingButton.selectedText
    const quotedFile = activeFilePath
    const lineRange = floatingButton.selectedLineRange
    setFloatingButton((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()

    // 尝试检测选中文字所在的函数名
    const funcName = lineRange
      ? detectFunctionName(displayContent, lineRange.start)
      : null

    // 格式化行号信息
    const lineInfo = lineRange
      ? (lineRange.start === lineRange.end
          ? `${lineRange.start}`
          : `${lineRange.start}-${lineRange.end}`)
      : ''

    // 组装引用路径：有函数名时附带函数名(行号)，否则只附行号
    let quotedFileInfo: string
    if (funcName && lineInfo) {
      quotedFileInfo = `${quotedFile}:${funcName}(${lineInfo})`
    } else if (funcName) {
      quotedFileInfo = `${quotedFile}:${funcName}`
    } else if (lineInfo) {
      quotedFileInfo = `${quotedFile}:${lineInfo}`
    } else {
      quotedFileInfo = quotedFile
    }

    onSendMessage('', quotedText, quotedFileInfo)
  }, [floatingButton.selectedText, floatingButton.selectedLineRange, activeFilePath, displayContent, onSendMessage])

  /**
   * 点击"批注"按钮处理
   * 改为弹出 AnnotationBubble 让用户输入批注内容，而非直接发送
   */
  const handleAnnotate = useCallback(() => {
    if (!floatingButton.selectedText) return
    // 将浮动按钮的位置和选中文字传递给 AnnotationBubble
    setAnnotationBubble({
      visible: true,
      selectedText: floatingButton.selectedText,
      position: floatingButton.position,
      lineRange: floatingButton.selectedLineRange,
    })
    // 隐藏浮动按钮
    setFloatingButton((prev) => ({ ...prev, visible: false }))
  }, [floatingButton])

  const handleCloseFloating = useCallback(() => {
    setFloatingButton((prev) => ({ ...prev, visible: false }))
  }, [])

  /**
   * 批注气泡提交处理
   * 将用户输入的批注保存到 annotations 列表中
   */
  const handleAnnotationSubmit = useCallback((suggestion: string) => {
    const newAnnotation: SavedAnnotation = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2),
      selectedText: annotationBubble.selectedText,
      suggestion,
      filePath: activeFilePath,
      lineRange: annotationBubble.lineRange,
    }
    setAnnotations((prev) => [...prev, newAnnotation])
    setAnnotationBubble((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()
  }, [annotationBubble, activeFilePath])

  /**
   * 批注气泡取消处理
   */
  const handleAnnotationCancel = useCallback(() => {
    setAnnotationBubble((prev) => ({ ...prev, visible: false }))
  }, [])

  /**
   * 删除单条批注
   */
  const handleDeleteAnnotation = useCallback((id: string) => {
    setAnnotations((prev) => prev.filter((ann) => ann.id !== id))
  }, [])

  /**
   * 提交所有批注
   * 将所有批注格式化后通过 onSendMessage 发送，conversation 和 choice 模式均支持
   */
  const handleSubmitAllAnnotations = useCallback(() => {
    if (annotations.length === 0) return
    const formattedAnnotations = annotations.map((ann) => {
      const lineInfo = ann.lineRange
        ? (ann.lineRange.start === ann.lineRange.end
            ? `:${ann.lineRange.start}`
            : `:${ann.lineRange.start}-${ann.lineRange.end}`)
        : ''
      return `[${ann.filePath}${lineInfo}] "${ann.selectedText}" → ${ann.suggestion}`
    }).join('\n')
    onSendMessage(formattedAnnotations)
    setAnnotations([])
  }, [annotations, onSendMessage])

  /**
   * 编辑内容变更处理
   */
  const handleEditContentChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const newContent = e.target.value
      setEditableContents((prev) => ({ ...prev, [activeFilePath]: newContent }))
      onFileContentChange?.(activeFilePath, newContent)
    },
    [activeFilePath, onFileContentChange],
  )

  const hasUnsavedChanges = useMemo(() => {
    const edited = editableContents[activeFilePath]
    if (edited === undefined) return false
    return edited !== activeFileContent
  }, [editableContents, activeFilePath, activeFileContent])

  /**
   * 保存当前文件内容到后端
   */
  const handleSave = useCallback(async () => {
    const content = editableContents[activeFilePath]
    if (content === undefined || !onSaveFile) return
    setIsSaving(true)
    setSaveMessage(null)
    try {
      const success = await onSaveFile(activeFilePath, content)
      setSaveMessage(success ? '已保存' : '保存失败')
      if (success) {
        setEditableContents((prev) => {
          const next = { ...prev }
          delete next[activeFilePath]
          return next
        })
      }
    } catch {
      setSaveMessage('保存失败')
    } finally {
      setIsSaving(false)
      setTimeout(() => setSaveMessage(null), 2000)
    }
  }, [activeFilePath, editableContents, onSaveFile])

  const handleApprove = useCallback(() => {
    onSubmitReview(requestId, 'approved', feedbackText.trim() || undefined)
  }, [requestId, feedbackText, onSubmitReview])

  const handleDeny = useCallback(() => {
    onSubmitReview(requestId, 'denied', feedbackText.trim() || undefined)
  }, [requestId, feedbackText, onSubmitReview])

  // Escape 关闭浮动按钮和批注气泡
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (annotationBubble.visible) {
          setAnnotationBubble((prev) => ({ ...prev, visible: false }))
        } else if (floatingButton.visible) {
          handleCloseFloating()
        }
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [floatingButton.visible, annotationBubble.visible, handleCloseFloating])

  // Ctrl+S 保存文件
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        if (isEditing && hasUnsavedChanges && onSaveFile) {
          e.preventDefault()
          handleSave()
        }
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isEditing, hasUnsavedChanges, onSaveFile, handleSave])

  // 渲染文件内容
  const renderedContent = useMemo(() => {
    switch (activeFileInfo.renderType) {
      case 'markdown':
        return (
          <div className="prose prose-sm dark:prose-invert max-w-none p-4">
            <LobeChatMarkdown content={displayContent} />
          </div>
        )
      case 'code':
        return (
          <div className="p-2">
            <CodeBlock code={displayContent} language={activeFileInfo.language} showLineNumbers={true} isStreaming={false} />
          </div>
        )
      case 'text':
      default:
        return (
          <pre className="whitespace-pre-wrap break-words p-4 font-mono text-sm leading-relaxed text-foreground">
            {displayContent}
          </pre>
        )
    }
  }, [displayContent, activeFileInfo])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* ── 顶部工具栏 ── */}
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-muted/20 px-4 py-2">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium text-foreground truncate max-w-[200px]" title={title}>{title}</h3>
          {mode === 'conversation' && (
            <span className="flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
              <MessageSquare className="h-3 w-3" />
              对话模式
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{activeFilePath}</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{activeFileInfo.language}</span>
          {saveMessage && (
            <span className={cn(
              'text-xs font-medium',
              saveMessage === '已保存' ? 'text-green-600' : 'text-red-500',
            )}>{saveMessage}</span>
          )}
          {isEditing && hasUnsavedChanges && onSaveFile && (
            <button
              onClick={handleSave}
              disabled={isSaving}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
                isSaving
                  ? 'bg-muted text-muted-foreground cursor-not-allowed'
                  : 'bg-green-600 text-white hover:bg-green-700',
              )}
              title="保存文件 (Ctrl+S)"
            >
              <Save className="h-3.5 w-3.5" />
              <span>{isSaving ? '保存中...' : '保存'}</span>
            </button>
          )}
          <button
            onClick={() => setIsEditing((prev) => !prev)}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
              isEditing ? 'bg-blue-600 text-white hover:bg-blue-700' : 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
            title={isEditing ? '切换到查看模式' : '切换到编辑模式'}
          >
            {isEditing ? <><Eye className="h-3.5 w-3.5" /><span>查看</span></> : <><Pencil className="h-3.5 w-3.5" /><span>编辑</span></>}
          </button>
        </div>
      </div>

      {/* ── 多文件 Tab 切换 ── */}
      {filePaths.length > 1 && (
        <div className="flex shrink-0 items-center gap-0.5 overflow-x-auto border-b border-border bg-muted/30 px-2 py-1">
          {filePaths.map((path, index) => {
            const info = getFileTypeInfo(path)
            const isActive = index === activeFileIndex
            const IconComponent = info.renderType === 'markdown' ? FileText : info.renderType === 'code' ? Code2 : File
            return (
              <button
                key={path}
                onClick={() => setActiveFileIndex(index)}
                className={cn(
                  'flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-xs transition-colors',
                  isActive ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:bg-background/50 hover:text-foreground',
                )}
                title={path}
              >
                <IconComponent className="h-3.5 w-3.5" />
                <span className="max-w-[120px] truncate">{getFileName(path)}</span>
              </button>
            )
          })}
        </div>
      )}

      {/* ── 文件内容区域（全屏） ── */}
      <div
        ref={contentRef}
        className={cn('relative min-h-0 flex-1 select-text overflow-y-auto', isEditing && 'flex')}
        onMouseUp={!isEditing ? handleMouseUp : undefined}
        onClick={!isEditing ? handleContentClick : undefined}
      >
        {isEditing ? (
          <textarea
            value={editableContent}
            onChange={handleEditContentChange}
            className="font-mono text-sm leading-relaxed bg-[#1e1e2e] text-[#cdd6f4] p-4 w-full h-full resize-none border-none outline-none flex-1"
            spellCheck={false}
          />
        ) : (
          renderedContent
        )}

        {!isEditing && floatingButton.visible && (
          <FloatingQuoteButton position={floatingButton.position} onQuote={handleQuote} onAnnotate={requestId ? handleAnnotate : undefined} onClose={handleCloseFloating} />
        )}

        {/* 批注气泡：用户输入批注内容 */}
        {!isEditing && annotationBubble.visible && (
          <AnnotationBubble
            selectedText={annotationBubble.selectedText}
            position={annotationBubble.position}
            onSubmit={handleAnnotationSubmit}
            onCancel={handleAnnotationCancel}
          />
        )}
      </div>

      {/* ── 批注列表区域 ── */}
      {annotations.length > 0 && (
        <div className="shrink-0 border-t border-border bg-muted/10">
          {/* 批注列表标题栏 */}
          <div className="flex items-center justify-between border-b border-border px-4 py-2">
            <div className="flex items-center gap-2">
              <MessageSquare className="h-3.5 w-3.5 text-yellow-600" />
              <span className="text-xs font-medium text-foreground">
                批注 ({annotations.length})
              </span>
            </div>
            <button
              onClick={handleSubmitAllAnnotations}
              className="flex items-center gap-1.5 rounded-md bg-yellow-600 px-3 py-1 text-xs font-medium text-white transition-colors hover:bg-yellow-700"
            >
              <Send className="h-3 w-3" />
              提交所有批注
            </button>
          </div>
          {/* 批注列表内容 */}
          <div className="max-h-40 overflow-y-auto px-4 py-1">
            {annotations.map((ann) => {
              const lineInfo = ann.lineRange
                ? (ann.lineRange.start === ann.lineRange.end
                    ? `:${ann.lineRange.start}`
                    : `:${ann.lineRange.start}-${ann.lineRange.end}`)
                : ''
              return (
                <div
                  key={ann.id}
                  className="flex items-start gap-2 border-b border-border/50 py-2 last:border-b-0"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground mb-1">
                      <span className="truncate max-w-[200px]" title={ann.filePath}>{getFileName(ann.filePath)}</span>
                      {lineInfo && <span>{lineInfo}</span>}
                    </div>
                    <div className="rounded bg-yellow-100 px-2 py-0.5 text-xs text-yellow-900 dark:bg-yellow-900/30 dark:text-yellow-200 mb-1">
                      &ldquo;{ann.selectedText.length > 40 ? ann.selectedText.slice(0, 40) + '...' : ann.selectedText}&rdquo;
                    </div>
                    <div className="text-xs text-foreground">
                      {ann.suggestion}
                    </div>
                  </div>
                  <button
                    onClick={() => handleDeleteAnnotation(ann.id)}
                    className="shrink-0 flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-destructive/10 hover:text-destructive transition-colors"
                    title="删除批注"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ── 底部：Choice 模式审批操作栏 ── */}
      {mode === 'choice' && (
        <div className="relative z-10 flex shrink-0 flex-col gap-2 border-t border-border bg-muted/20 px-4 py-3">
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="审批备注（可选）..."
            rows={2}
            className="w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm outline-none placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-ring"
          />
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {options?.map((option) => (
              <button
                key={option.id}
                onClick={() => onSendMessage(option.label)}
                className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-accent"
              >
                {option.label}
              </button>
            ))}
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={handleApprove}
                className="flex items-center gap-1.5 rounded-md bg-green-600 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-green-700"
              >
                <CheckCircle2 className="h-4 w-4" />
                通过
              </button>
              <button
                onClick={handleDeny}
                className="flex items-center gap-1.5 rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-red-700"
              >
                <XCircle className="h-4 w-4" />
                驳回
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
