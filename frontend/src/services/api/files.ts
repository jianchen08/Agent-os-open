/**
 * 文件上传 API 服务
 *
 * 提供文件上传和模型能力查询功能
 *
 * 暴露接口：
 * - uploadFile(file, modelName): FileUploadResponse - 上传文件
 * - getModelCapabilities(modelName): FileCapabilityResponse - 获取模型文件能力
 * - getSupportedTypes(): SupportedTypesResponse - 获取支持的文件类型
 * - validateFile(file, capabilities): 验证结果 - 验证文件是否可上传
 * - getFileCategory(mimeType): 文件分类 - 获取文件类型分类
 * - FileUploadResponse - 文件上传响应类型
 * - FileCapabilityResponse - 模型文件能力响应
 * - SupportedTypesResponse - 支持的文件类型响应
 */

import apiClient from '@/services/api/client'

/**
 * 文件上传响应
 */
export interface FileUploadResponse {
  /** 文件唯一标识 */
  file_id: string
  /** 原始文件名 */
  filename: string
  /** MIME类型 */
  mime_type: string
  /** 文件大小（字节） */
  size: number
  /** 文件类型（image/document） */
  file_type: 'image' | 'document'
  /** Base64编码的文件内容 */
  base64_data: string
  /** 上传时间 */
  uploaded_at: string
}

/**
 * 模型文件能力响应（扩展版）
 */
export interface FileCapabilityResponse {
  /** 模型名称 */
  model_name: string
  /** 是否支持图片 */
  supports_image: boolean
  /** 是否支持文档 */
  supports_document: boolean
  /** 支持的图片类型 */
  supported_image_types: string[]
  /** 支持的文档类型 */
  supported_document_types: string[]
  /** 最大图片大小（字节） */
  max_image_size: number
  /** 最大文档大小（字节） */
  max_document_size: number
  // 扩展字段（音频、视频、代码）
  /** 是否支持音频 */
  supports_audio?: boolean
  /** 是否支持视频 */
  supports_video?: boolean
  /** 是否支持代码文件 */
  supports_code?: boolean
  /** 支持的音频类型 */
  supported_audio_types?: string[]
  /** 支持的视频类型 */
  supported_video_types?: string[]
  /** 支持的代码文件类型 */
  supported_code_types?: string[]
  /** 最大音频大小（字节） */
  max_audio_size?: number
  /** 最大视频大小（字节） */
  max_video_size?: number
  /** 最大代码文件大小（字节） */
  max_code_size?: number
  /** 是否为多模态模型 */
  is_multimodal?: boolean
}

/**
 * 支持的文件类型响应
 */
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

/**
 * 上传文件
 */
export async function uploadFile(
  file: File,
  modelName?: string
): Promise<FileUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  if (modelName) {
    formData.append('model_name', modelName)
  }

  const response = await apiClient.post<FileUploadResponse>(
    '/api/v1/files/upload',
    formData,
    {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 60000,
    }
  )

  return response.data
}

/**
 * 获取模型文件能力
 */
export async function getModelCapabilities(
  modelName: string
): Promise<FileCapabilityResponse> {
  const response = await apiClient.get<FileCapabilityResponse>(
    `/api/v1/files/capabilities`,
    { params: { model_name: modelName } }
  )
  return response.data
}

/**
 * 获取支持的文件类型
 */
export async function getSupportedTypes(): Promise<SupportedTypesResponse> {
  const response = await apiClient.get<SupportedTypesResponse>(
    '/files/supported-types'
  )
  return response.data
}

/**
 * 验证文件是否可上传
 */
export function validateFile(
  file: File,
  capabilities?: FileCapabilityResponse
): { valid: boolean; error?: string } {
  const imageTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
  const documentTypes = [
    'application/pdf',
    'text/plain',
    'text/markdown',
    'text/csv',
  ]
  const audioTypes = [
    'audio/webm',
    'audio/webm;codecs=opus',
    'audio/mp4',
    'audio/mpeg',
    'audio/wav',
    'audio/ogg',
  ]

  const isImage = imageTypes.includes(file.type)
  const isDocument = documentTypes.includes(file.type)
  const isAudio = audioTypes.some(type => file.type === type || file.type.startsWith('audio/'))

  if (!isImage && !isDocument && !isAudio) {
    return {
      valid: false,
      error: `不支持的文件类型: ${file.type || '未知'}`,
    }
  }

  const maxImageSize = 20 * 1024 * 1024
  const maxDocumentSize = 10 * 1024 * 1024
  const maxAudioSize = 25 * 1024 * 1024 // 音频文件最大 25MB
  const maxSize = isImage ? maxImageSize : isAudio ? maxAudioSize : maxDocumentSize

  if (file.size > maxSize) {
    const maxSizeMB = maxSize / (1024 * 1024)
    return {
      valid: false,
      error: `文件大小超过限制（最大 ${maxSizeMB}MB）`,
    }
  }

  if (capabilities) {
    if (isImage && !capabilities.supports_image) {
      return { valid: false, error: `当前模型不支持图片输入` }
    }
    if (isDocument && !capabilities.supports_document) {
      return { valid: false, error: `当前模型不支持文档输入` }
    }
    if (isAudio && !capabilities.supports_audio) {
      return { valid: false, error: `当前模型不支持音频输入` }
    }
  }

  return { valid: true }
}

/**
 * 获取文件类型分类
 */
export function getFileCategory(
  mimeType: string
): 'image' | 'document' | 'audio' | 'unknown' {
  const imageTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
  const documentTypes = [
    'application/pdf',
    'text/plain',
    'text/markdown',
    'text/csv',
  ]
  const audioTypes = [
    'audio/webm',
    'audio/mp4',
    'audio/mpeg',
    'audio/wav',
    'audio/ogg',
  ]

  if (imageTypes.includes(mimeType)) return 'image'
  if (documentTypes.includes(mimeType)) return 'document'
  if (audioTypes.some(type => mimeType === type || mimeType.startsWith('audio/'))) return 'audio'
  return 'unknown'
}
