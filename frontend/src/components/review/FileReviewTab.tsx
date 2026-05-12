/**
 * FileReviewTab - 文件审批 Tab 组件
 *
 * 全屏文件查看/编辑器，支持：
 *   - 根据扩展名自动选择 Markdown / 代码高亮 / 纯文本渲染
 *   - 查看/编辑模式切换
 *   - 文字选中后弹出浮动按钮「引用到对话」，将选中文字注入到 Chat 面板
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
  }, [floatingButton.visible])

  /**
   * 引用选中文字到 Chat 输入框（含文件路径和行号范围）
   */
  const handleQuote = useCallback(() => {
    if (!floatingButton.selectedText) return
    const quotedText = floatingButton.selectedText
    const quotedFile = activeFilePath
    const lineRange = floatingButton.selectedLineRange
    setFloatingButton((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()
    const lineInfo = lineRange
      ? (lineRange.start === lineRange.end
          ? `:${lineRange.start}`
          : `:${lineRange.start}-${lineRange.end}`)
      : ''
    onSendMessage('', quotedText, `${quotedFile}${lineInfo}`)
  }, [floatingButton.selectedText, floatingButton.selectedLineRange, activeFilePath, onSendMessage])

  const handleAnnotate = useCallback(() => {
    if (!floatingButton.selectedText) return
    const quotedText = floatingButton.selectedText
    const quotedFile = activeFilePath
    const lineRange = floatingButton.selectedLineRange
    setFloatingButton((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()
    const lineInfo = lineRange
      ? (lineRange.start === lineRange.end
          ? `:${lineRange.start}`
          : `:${lineRange.start}-${lineRange.end}`)
      : ''
    const annotation = `[${quotedFile}${lineInfo}] ${quotedText}`
    if (mode === 'choice') {
      setFeedbackText((prev) => prev ? `${prev}\n${annotation}` : annotation)
    } else {
      onSendMessage(annotation)
    }
  }, [floatingButton.selectedText, floatingButton.selectedLineRange, activeFilePath, mode, onSendMessage])

  const handleCloseFloating = useCallback(() => {
    setFloatingButton((prev) => ({ ...prev, visible: false }))
  }, [])

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

  // Escape 关闭浮动按钮
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && floatingButton.visible) handleCloseFloating()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [floatingButton.visible, handleCloseFloating])

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
          <FloatingQuoteButton position={floatingButton.position} onQuote={handleQuote} onAnnotate={handleAnnotate} onClose={handleCloseFloating} />
        )}
      </div>

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
