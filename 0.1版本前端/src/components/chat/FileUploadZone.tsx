/**
 * FileUploadZone - 增强版文件上传组件
 *
 * 功能：
 * - 拖拽上传区域
 * - 点击选择文件
 * - 粘贴上传
 * - 多文件批量上传
 * - 上传进度显示
 * - 文件格式预览（图片缩略图、文件类型图标）
 * - Agent 可读取上传文件（通过 API 返回 file_id）
 * - 支持多种文件格式（图片、文档、音频、代码文件）
 * - 文件验证（类型、大小）
 */

import {
  AlertCircle,
  File as FileIcon,
  FileText,
  Headphones,
  Image as ImageIcon,
  Loader2,
  Music,
  Paperclip,
  Trash2,
  Upload,
  Video,
  X,
  FileCode,
  FileSpreadsheet,
  FileArchive,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { uploadFile, validateFile } from '@/services/api/files'
import type { Attachment } from './types'

/** 文件类型分类 */
type FileCategory = 'image' | 'document' | 'audio' | 'video' | 'code' | 'archive' | 'unknown'

/** 上传文件状态 */
export interface UploadableFile {
  /** 本地 ID */
  localId: string
  /** 原始 File 对象 */
  file: File
  /** 文件分类 */
  category: FileCategory
  /** 上传状态 */
  status: 'pending' | 'uploading' | 'success' | 'error'
  /** 上传进度 (0-100) */
  progress: number
  /** 缩略图 URL（图片类型） */
  thumbnailUrl?: string
  /** 上传结果 */
  result?: {
    file_id: string
    filename: string
    mime_type: string
    file_type: string
    base64_data?: string
  }
  /** 错误信息 */
  error?: string
}

export interface FileUploadZoneProps {
  /** 是否启用 */
  enabled?: boolean
  /** 最大文件数量 */
  maxFiles?: number
  /** 自定义文件类型过滤 */
  accept?: string
  /** 上传完成的回调 */
  onFilesChange?: (files: Attachment[]) => void
  /** 所有文件上传完成时的回调 */
  onAllUploaded?: (attachments: Attachment[]) => void
  /** 模型名称（用于上传 API） */
  modelName?: string
  /** 是否紧凑模式 */
  compact?: boolean
  /** 自定义类名 */
  className?: string
}

/** 获取文件分类 */
function getFileCategory(file: File): FileCategory {
  const type = file.type
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''

  if (type.startsWith('image/')) return 'image'
  if (type.startsWith('audio/')) return 'audio'
  if (type.startsWith('video/')) return 'video'

  // 文档类型
  const docTypes = ['application/pdf', 'text/plain', 'text/markdown', 'text/csv', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']
  if (docTypes.includes(type)) return 'document'

  // 代码文件
  const codeExts = ['js', 'jsx', 'ts', 'tsx', 'py', 'java', 'cpp', 'c', 'h', 'go', 'rs', 'rb', 'php', 'css', 'scss', 'html', 'xml', 'json', 'yaml', 'yml', 'toml', 'sh', 'bash', 'sql', 'md']
  if (codeExts.includes(ext)) return 'code'

  // 压缩包
  const archiveExts = ['zip', 'rar', '7z', 'tar', 'gz', 'bz2']
  if (archiveExts.includes(ext)) return 'archive'

  return 'unknown'
}

/** 获取文件类型图标 */
function FileCategoryIcon({ category, className }: { category: FileCategory; className?: string }) {
  switch (category) {
    case 'image':
      return <ImageIcon className={cn('text-blue-500', className)} />
    case 'document':
      return <FileText className={cn('text-orange-500', className)} />
    case 'audio':
      return <Headphones className={cn('text-purple-500', className)} />
    case 'video':
      return <Video className={cn('text-red-500', className)} />
    case 'code':
      return <FileCode className={cn('text-green-500', className)} />
    case 'archive':
      return <FileArchive className={cn('text-yellow-600', className)} />
    default:
      return <FileIcon className={cn('text-gray-500', className)} />
  }
}

/** 格式化文件大小 */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** 单个文件预览卡片 */
function FilePreviewCard({
  item,
  onRemove,
}: {
  item: UploadableFile
  onRemove: () => void
}) {
  return (
    <div
      className={cn(
        'group relative flex items-center gap-2 rounded-lg border p-2 transition-all',
        item.status === 'error'
          ? 'border-destructive/50 bg-destructive/5'
          : 'border-border/50 bg-muted/30 hover:border-border',
      )}
    >
      {/* 缩略图或图标 */}
      {item.thumbnailUrl ? (
        <img
          src={item.thumbnailUrl}
          alt={item.file.name}
          className="h-10 w-10 rounded-md object-cover flex-shrink-0"
        />
      ) : (
        <div className="flex h-10 w-10 items-center justify-center rounded-md bg-background flex-shrink-0">
          <FileCategoryIcon category={item.category} className="h-5 w-5" />
        </div>
      )}

      {/* 文件信息 */}
      <div className="flex-1 min-w-0">
        <div className="truncate text-xs font-medium">{item.file.name}</div>
        <div className="text-[10px] text-muted-foreground">
          {formatFileSize(item.file.size)}
        </div>

        {/* 上传进度条 */}
        {item.status === 'uploading' && (
          <div className="mt-1 h-1 rounded-full bg-muted overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-all duration-300"
              style={{ width: `${item.progress}%` }}
            />
          </div>
        )}
      </div>

      {/* 状态图标 */}
      {item.status === 'uploading' && (
        <Loader2 className="h-4 w-4 animate-spin text-primary flex-shrink-0" />
      )}
      {item.status === 'error' && (
        <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0" />
      )}

      {/* 删除按钮 */}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0 rounded-full opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity flex-shrink-0"
        onClick={onRemove}
        disabled={item.status === 'uploading'}
        aria-label={`移除 ${item.file.name}`}
      >
        <X className="h-3 w-3" />
      </Button>
    </div>
  )
}

/**
 * FileUploadZone 主组件
 */
export function FileUploadZone({
  enabled = true,
  maxFiles = 10,
  accept,
  onFilesChange,
  onAllUploaded,
  modelName,
  compact = false,
  className,
}: FileUploadZoneProps) {
  const [files, setFiles] = useState<UploadableFile[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [globalError, setGlobalError] = useState<string | null>(null)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const dragCounterRef = useRef(0)

  /** 上传单个文件 */
  const uploadSingleFile = useCallback(
    async (item: UploadableFile): Promise<UploadableFile> => {
      try {
        // 更新状态为上传中
        setFiles((prev) =>
          prev.map((f) =>
            f.localId === item.localId ? { ...f, status: 'uploading' as const, progress: 10 } : f,
          ),
        )

        // 模拟进度推进（实际上传由 API 处理）
        const progressInterval = setInterval(() => {
          setFiles((prev) =>
            prev.map((f) => {
              if (f.localId !== item.localId || f.status !== 'uploading') return f
              const newProgress = Math.min(f.progress + Math.random() * 20, 90)
              return { ...f, progress: newProgress }
            }),
          )
        }, 300)

        const result = await uploadFile(item.file, modelName)

        clearInterval(progressInterval)

        const updated: UploadableFile = {
          ...item,
          status: 'success',
          progress: 100,
          result: {
            file_id: result.file_id,
            filename: result.filename,
            mime_type: result.mime_type,
            file_type: result.file_type,
            base64_data: result.base64_data,
          },
        }

        setFiles((prev) =>
          prev.map((f) => (f.localId === item.localId ? updated : f)),
        )

        return updated
      } catch (error: any) {
        const errorMessage = error?.message ?? '上传失败'
        const updated: UploadableFile = {
          ...item,
          status: 'error',
          error: errorMessage,
        }

        setFiles((prev) =>
          prev.map((f) => (f.localId === item.localId ? updated : f)),
        )

        return updated
      }
    },
    [modelName],
  )

  /** 处理文件选择 */
  const handleFileSelect = useCallback(
    async (fileList: FileList | File[]) => {
      if (!enabled) return

      setGlobalError(null)

      const fileArray = Array.from(fileList)
      const remaining = maxFiles - files.length

      if (fileArray.length > remaining) {
        setGlobalError(`最多上传 ${maxFiles} 个文件，还能添加 ${remaining} 个`)
        fileArray.splice(remaining)
      }

      // 验证并创建 UploadableFile
      const newItems: UploadableFile[] = []
      for (const file of fileArray) {
        const validation = validateFile(file)
        if (!validation.valid) {
          setGlobalError(validation.error ?? '文件验证失败')
          continue
        }

        const localId = `file-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
        const category = getFileCategory(file)

        newItems.push({
          localId,
          file,
          category,
          status: 'pending',
          progress: 0,
          thumbnailUrl:
            category === 'image' ? URL.createObjectURL(file) : undefined,
        })
      }

      if (newItems.length === 0) return

      setFiles((prev) => [...prev, ...newItems])

      // 异步上传所有文件
      const uploadedItems = await Promise.all(newItems.map(uploadSingleFile))

      // 通知父组件
      const successfulUploads = uploadedItems.filter((u) => u.status === 'success')
      if (successfulUploads.length > 0) {
        const attachments: Attachment[] = successfulUploads.map((u) => ({
          id: u.result!.file_id,
          name: u.result!.filename,
          type: u.result!.mime_type,
          size: u.file.size,
          url: u.result!.file_id,
          status: 'completed' as const,
        }))

        // 通知当前所有已完成文件
        const allCurrentAttachments = [
          ...files
            .filter((f) => f.status === 'success' && f.result)
            .map((f) => ({
              id: f.result!.file_id,
              name: f.result!.filename,
              type: f.result!.mime_type,
              size: f.file.size,
              url: f.result!.file_id,
              status: 'completed' as const,
            })),
          ...attachments,
        ]

        onFilesChange?.(allCurrentAttachments)
        onAllUploaded?.(allCurrentAttachments)
      }
    },
    [enabled, maxFiles, files, uploadSingleFile, onFilesChange, onAllUploaded],
  )

  /** 移除文件 */
  const handleRemove = useCallback(
    (localId: string) => {
      setFiles((prev) => {
        const item = prev.find((f) => f.localId === localId)
        if (item?.thumbnailUrl) {
          URL.revokeObjectURL(item.thumbnailUrl)
        }
        return prev.filter((f) => f.localId !== localId)
      })

      // 通知更新
      setFiles((prev) => {
        const attachments: Attachment[] = prev
          .filter((f) => f.status === 'success' && f.result && f.localId !== localId)
          .map((f) => ({
            id: f.result!.file_id,
            name: f.result!.filename,
            type: f.result!.mime_type,
            size: f.file.size,
            url: f.result!.file_id,
            status: 'completed' as const,
          }))
        onFilesChange?.(attachments)
        return prev
      })
    },
    [onFilesChange],
  )

  /** 清空所有文件 */
  const handleClearAll = useCallback(() => {
    files.forEach((f) => {
      if (f.thumbnailUrl) URL.revokeObjectURL(f.thumbnailUrl)
    })
    setFiles([])
    onFilesChange?.([])
  }, [files, onFilesChange])

  /** 触发文件选择对话框 */
  const triggerSelect = useCallback(() => {
    fileInputRef.current?.click()
  }, [])

  // 拖拽事件
  const handleDragEnter = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      dragCounterRef.current++
      if (enabled) setIsDragging(true)
    },
    [enabled],
  )

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current--
    if (dragCounterRef.current === 0) setIsDragging(false)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      dragCounterRef.current = 0
      setIsDragging(false)
      if (enabled && e.dataTransfer.files.length > 0) {
        handleFileSelect(e.dataTransfer.files)
      }
    },
    [enabled, handleFileSelect],
  )

  // 粘贴上传
  useEffect(() => {
    const handlePaste = (e: ClipboardEvent) => {
      if (!enabled) return
      const items = e.clipboardData?.items
      if (!items) return

      const files: File[] = []
      for (const item of items) {
        if (item.kind === 'file') {
          const file = item.getAsFile()
          if (file) files.push(file)
        }
      }
      if (files.length > 0) {
        handleFileSelect(files)
      }
    }

    document.addEventListener('paste', handlePaste)
    return () => document.removeEventListener('paste', handlePaste)
  }, [enabled, handleFileSelect])

  // 清理缩略图 URL
  useEffect(() => {
    return () => {
      files.forEach((f) => {
        if (f.thumbnailUrl) URL.revokeObjectURL(f.thumbnailUrl)
      })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const hasFiles = files.length > 0
  const allUploaded = files.length > 0 && files.every((f) => f.status === 'success')
  const hasErrors = files.some((f) => f.status === 'error')
  const isUploading = files.some((f) => f.status === 'uploading')

  if (!enabled) return null

  return (
    <div
      className={cn('relative', className)}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* 隐藏的文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        onChange={(e) => {
          if (e.target.files) handleFileSelect(e.target.files)
          e.target.value = ''
        }}
      />

      {/* 全局错误提示 */}
      {globalError && (
        <div className="flex items-center gap-2 text-xs text-destructive bg-destructive/10 rounded-lg px-3 py-1.5 mb-2">
          <AlertCircle className="h-3 w-3 flex-shrink-0" />
          <span>{globalError}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-4 w-4 p-0 ml-auto"
            onClick={() => setGlobalError(null)}
          >
            <X className="h-3 w-3" />
          </Button>
        </div>
      )}

      {/* 拖拽覆盖层 */}
      {isDragging && (
        <div className="absolute inset-0 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-primary bg-primary/5">
          <div className="flex flex-col items-center gap-2 text-primary">
            <Upload className="h-8 w-8" />
            <span className="text-sm font-medium">松开鼠标上传文件</span>
            <span className="text-xs text-muted-foreground">
              支持图片、文档、音频、代码文件
            </span>
          </div>
        </div>
      )}

      {/* 文件列表 */}
      {hasFiles && (
        <div className="space-y-1.5 mb-2">
          {files.map((item) => (
            <FilePreviewCard
              key={item.localId}
              item={item}
              onRemove={() => handleRemove(item.localId)}
            />
          ))}

          {/* 操作栏 */}
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              className="text-xs h-6"
              onClick={triggerSelect}
              disabled={isUploading || files.length >= maxFiles}
            >
              <Paperclip className="h-3 w-3 mr-1" />
              继续添加
            </Button>
            {hasFiles && (
              <Button
                variant="ghost"
                size="sm"
                className="text-xs h-6 text-destructive hover:text-destructive"
                onClick={handleClearAll}
                disabled={isUploading}
              >
                <Trash2 className="h-3 w-3 mr-1" />
                清空全部
              </Button>
            )}
          </div>
        </div>
      )}

      {/* 上传按钮（无文件时显示） */}
      {!compact && !hasFiles && (
        <button
          className="flex items-center gap-2 text-xs text-muted-foreground hover:text-primary transition-colors py-1"
          onClick={triggerSelect}
        >
          <Upload className="h-3.5 w-3.5" />
          <span>点击或拖拽文件到此处上传</span>
          <span className="text-[10px]">（支持图片、文档、音频、代码）</span>
        </button>
      )}

      {/* 紧凑模式按钮 */}
      {compact && !hasFiles && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={triggerSelect}
          aria-label="上传文件"
        >
          <Paperclip className="h-4 w-4" />
        </Button>
      )}
    </div>
  )
}
