/**
 * 代码编辑器组件
 *
 * 在工作区面板中提供带语法高亮的代码编辑功能。
 * 根据文件扩展名自动识别语言，支持保存、修改标记和大文件检测。
 *
 * @module components/workspace/CodeEditor
 */

import { Save, AlertTriangle, FileText, Eye, Pencil } from 'lucide-react'
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { LobeChatMarkdown } from '../chat/LobeChatMarkdown'
import { cn } from '@/lib/utils'

/** Markdown 扩展名集合 */
const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown'])

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
}

/**
 * 从文件名提取扩展名
 *
 * @param fileName - 文件名或文件路径
 * @returns 小写扩展名（如 ".py"），无扩展名返回整个文件名的小写
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
 * 根据文件名获取 Prism 语言标识
 *
 * @param fileName - 文件名或文件路径
 * @returns Prism 语言标识（如 "python"），未知时返回 "text"
 */
function getLanguage(fileName: string): string {
  const ext = extractExtension(fileName)
  return EXTENSION_TO_LANGUAGE[ext] ?? 'text'
}

/**
 * 判断文件是否可编辑
 *
 * @param fileName - 文件名或文件路径
 * @returns 是否可编辑
 */
function isEditable(fileName: string): boolean {
  const ext = extractExtension(fileName)
  return EDITABLE_EXTENSIONS.has(ext)
}

/**
 * 代码编辑器组件
 *
 * 功能：
 * - 根据文件扩展名自动识别语言并应用语法高亮
 * - 支持基础文本编辑（输入、选择、复制粘贴）
 * - Ctrl+S 快捷键保存 + 保存按钮
 * - 未保存修改时显示星号标记
 * - 大文件（超过 1MB）提示无法编辑
 * - 只读模式下以语法高亮显示代码
 */
export function CodeEditor({
  filePath,
  content: initialContent,
  size,
  onSave,
  readOnly = false,
  className,
}: CodeEditorProps) {
  const [localContent, setLocalContent] = useState(initialContent)
  const [isDirty, setIsDirty] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  /** 预览/编辑模式切换，默认预览模式 */
  const [isPreview, setIsPreview] = useState(true)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const preRef = useRef<HTMLPreElement>(null)

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

  /** 当外部 content 变化时同步（如文件重新加载） */
  useEffect(() => {
    setLocalContent(initialContent)
    setIsDirty(false)
    setSaveError(null)
  }, [initialContent])

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
        <div className="min-h-0 flex-1 overflow-auto">
          <SyntaxHighlighter
            language={language}
            style={oneDark}
            showLineNumbers={true}
            wrapLongLines={true}
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
        </div>
      </div>
    )
  }

  // 可编辑文件：支持预览/编辑模式切换
  return (
    <div className={cn('flex h-full flex-col', className)}>
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
          <div className="prose prose-sm dark:prose-invert max-w-none min-h-0 flex-1 overflow-auto p-4">
            <LobeChatMarkdown content={localContent} />
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto">
            <SyntaxHighlighter
              language={language}
              style={oneDark}
              showLineNumbers={true}
              wrapLongLines={true}
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
          </div>
        )
      ) : (
        /* 编辑模式：textarea + 语法高亮背景 */
        <div className="relative min-h-0 flex-1 overflow-hidden">
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
