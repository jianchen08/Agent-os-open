/** 文件上传 API 服务 提供文件上传和模型能力查询功能 */

import apiClient from '@/services/api/client'
import type { ModelCapabilities } from '@/types/capabilities'

/** 文件上传响应 */
export interface FileUploadResponse {
  /** 文件唯一标识 */
  file_id: string
  /** 原始文件名 */
  filename: string
  /** MIME类型 */
  mime_type: string
  /** 媒体类型（image/document/audio/video） */
  media_type: string
  /** 文件大小（字节） */
  size: number
  /** 文件访问 URL */
  url: string
}

/** 模型文件能力响应（扩展版） */
export interface FileCapabilityResponse {
  /** 模型名称 */
  model_name: string
  /** 是否支持图片 */
  supports_image: boolean
  /** 支持的图片类型 */
  supported_image_types: string[]
  /** 最大图片大小（字节） */
  max_image_size: number
  // 音频、视频能力（真正的多模态能力）
  /** 是否支持音频 */
  supports_audio?: boolean
  /** 是否支持视频 */
  supports_video?: boolean
  /** 支持的音频类型 */
  supported_audio_types?: string[]
  /** 支持的视频类型 */
  supported_video_types?: string[]
  /** 最大音频大小（字节） */
  max_audio_size?: number
  /** 最大视频大小（字节） */
  max_video_size?: number
  /** 是否为多模态模型 */
  is_multimodal?: boolean
}

/** 支持的文件类型响应 */
export interface SupportedTypesResponse {
  /** 支持的图片类型 */
  image_types: Record<string, string[]>
  /** 支持的文档类型 */
  document_types: Record<string, string[]>
  /** 最大图片大小（字节） */
  max_image_size: number
  /** 最大文档大小（字节） */
  max_document_size: number
}

/** 上传文件 */
export async function uploadFile(file: File, modelName?: string): Promise<FileUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  if (modelName) {
    formData.append('model_name', modelName)
  }

  // 后端实际端点是 /api/v1/artifacts/upload
  const response = await apiClient.post<FileUploadResponse>('/api/v1/artifacts/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    timeout: 60000,
  })

  return response.data
}

/** 获取模型文件能力 静默处理 404，避免控制台报错 */
export async function getModelCapabilities(modelName: string): Promise<FileCapabilityResponse> {
  try {
    const response = await apiClient.get<FileCapabilityResponse>(`/api/v1/files/capabilities`, {
      params: { model_name: modelName },
    })
    return response.data
  } catch (error: unknown) {
    if (
      typeof error === 'object' &&
      error !== null &&
      'response' in error &&
      (error as { response?: { status?: number } }).response?.status === 404
    ) {
      // 端点不存在：返回全 False 的合法能力（无多模态，但仍可发文本附件）
      return {
        model_name: modelName,
        supports_image: false,
        supported_image_types: [],
        max_image_size: 0,
        supports_audio: false,
        supports_video: false,
        is_multimodal: false,
      }
    }
    throw error
  }
}

/** 获取支持的文件类型 */
export async function getSupportedTypes(): Promise<SupportedTypesResponse> {
  const response = await apiClient.get<SupportedTypesResponse>('/files/supported-types')
  return response.data
}

/**
 * 纯文本类 MIME 前缀/集合：可直接 UTF-8 解码的文件。
 * 任何模型都能接收文本，前端宽规则放行，无需多模态能力声明。
 */
const PLAIN_TEXT_MIME_PREFIX = 'text/'
const PLAIN_TEXT_EXTRA_MIMES = new Set([
  'application/json',
  'application/xml',
  'application/javascript',
  'application/x-yaml',
  'application/x-sh',
  // 非标准但实际会出现的文本 MIME
  'application/text',
  'application/x-tex',
  'application/x-httpd-php',
])

/**
 * markitdown 可转换的文档扩展名（pdf/docx/xlsx/pptx 等）。
 * 后端经 markitdown 提取文本后拼进用户消息，前端同样按文本放行。
 */
const MARKITDOWN_DOCUMENT_EXTENSIONS = new Set([
  '.pdf',
  '.docx',
  '.doc',
  '.xlsx',
  '.xls',
  '.csv',
  '.pptx',
  '.ppt',
])

/**
 * 常见文本/代码文件扩展名（MIME 不准或未知时按扩展名兜底）。
 * 与后端 get_file_category "非二进制即 text" 的宽松策略对齐。
 */
const TEXT_FILE_EXTENSIONS = new Set([
  '.txt', '.md', '.markdown', '.log', '.csv', '.tsv',
  '.json', '.yaml', '.yml', '.toml', '.xml', '.ini', '.cfg', '.conf',
  '.properties', '.env', '.svg',
  // 代码文件
  '.py', '.js', '.jsx', '.ts', '.tsx', '.java', '.kt', '.go', '.rs',
  '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.dart',
  '.sh', '.bash', '.bat', '.ps1', '.sql', '.r', '.lua', '.pl',
  '.vue', '.svelte', '.scss', '.less', '.css', '.html', '.htm',
  '.graphql', '.gql', '.proto', '.zig',
])

/** 文本类附件大小上限（与后端 markitdown 的 10MB 对齐） */
const MAX_TEXT_FILE_SIZE = 10 * 1024 * 1024

/** 提取文件扩展名（小写，含点；无扩展名返回空串） */
function extractExt(fileName: string): string {
  const dotIdx = fileName.lastIndexOf('.')
  return dotIdx >= 0 ? fileName.slice(dotIdx).toLowerCase() : ''
}

/** 判断 MIME 是否为纯文本类（可直接解码） */
function isPlainTextMime(mimeType: string): boolean {
  return mimeType.startsWith(PLAIN_TEXT_MIME_PREFIX) || PLAIN_TEXT_EXTRA_MIMES.has(mimeType)
}

/** 判断文件是否为 markitdown 可转的二进制文档（按扩展名） */
function isMarkitdownDocument(file: File): boolean {
  const ext = extractExt(file.name)
  return ext !== '' && MARKITDOWN_DOCUMENT_EXTENSIONS.has(ext)
}

/** 判断文件是否为常见文本/代码文件（按扩展名兜底，应对 MIME 不准或未知） */
function isTextFileByExtension(file: File): boolean {
  const ext = extractExt(file.name)
  return ext !== '' && TEXT_FILE_EXTENSIONS.has(ext)
}

/** 判断文件是否为文本类附件（纯文本或 markitdown 文档或文本扩展名） */
export function isTextLikeFile(file: File): boolean {
  return isPlainTextMime(file.type) || isMarkitdownDocument(file) || isTextFileByExtension(file)
}

/** 验证文件是否可上传 */
export function validateFile(
  file: File,
  capabilities?: ModelCapabilities | null,
): { valid: boolean; error?: string } {
  // 1. 文本/文档/代码类：宽规则放行（任何模型都能接收文本）
  if (isTextLikeFile(file)) {
    if (file.size > MAX_TEXT_FILE_SIZE) {
      const maxSizeMB = MAX_TEXT_FILE_SIZE / (1024 * 1024)
      return {
        valid: false,
        error: `文件大小超过限制（最大 ${maxSizeMB}MB）`,
      }
    }
    return { valid: true }
  }

  // 2. 图片/音频/视频：按模型多模态能力校验
  const isImage = file.type.startsWith('image/')
  const isAudio = file.type.startsWith('audio/')
  const isVideo = file.type.startsWith('video/')

  if (!isImage && !isAudio && !isVideo) {
    return {
      valid: false,
      error: `不支持的文件类型: ${file.type || '未知'}`,
    }
  }

  // 多模态类型必须在 capabilities 中被声明支持
  if (capabilities) {
    if (isImage && !capabilities.supportsImage) {
      return { valid: false, error: '当前模型不支持图片输入' }
    }
    if (isAudio && !capabilities.supportsAudio) {
      return { valid: false, error: '当前模型不支持音频输入' }
    }
    if (isVideo && !capabilities.supportsVideo) {
      return { valid: false, error: '当前模型不支持视频输入' }
    }

    // MIME 类型必须在能力声明的 supported_*_types 列表内
    if (isImage && !capabilities.supportedImageTypes.includes(file.type)) {
      return { valid: false, error: `不支持的图片类型: ${file.type}` }
    }
    if (isAudio && !capabilities.supportedAudioTypes.includes(file.type)) {
      return { valid: false, error: `不支持的音频类型: ${file.type}` }
    }
    if (isVideo && !capabilities.supportedVideoTypes.includes(file.type)) {
      return { valid: false, error: `不支持的视频类型: ${file.type}` }
    }

    // 大小限制
    const maxSize = isImage
      ? capabilities.maxImageSize
      : isAudio
        ? capabilities.maxAudioSize
        : capabilities.maxVideoSize
    if (maxSize > 0 && file.size > maxSize) {
      const maxSizeMB = maxSize / (1024 * 1024)
      return {
        valid: false,
        error: `文件大小超过限制（最大 ${maxSizeMB}MB）`,
      }
    }
  }

  return { valid: true }
}

/** 获取文件类型分类 */
export function getFileCategory(mimeType: string): 'image' | 'document' | 'audio' | 'video' | 'text' | 'unknown' {
  if (mimeType.startsWith('image/')) return 'image'
  if (mimeType.startsWith('audio/')) return 'audio'
  if (mimeType.startsWith('video/')) return 'video'
  if (isPlainTextMime(mimeType)) return 'text'
  // 二进制文档（pdf/docx 等）经 markitdown 转 text，归类为 document
  return 'unknown'
}
