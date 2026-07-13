/**
 * 模型能力 Hook
 *
 * 根据当前模型获取多模态能力，并计算输入组件的显示配置
 */

import { useState, useEffect, useMemo, useRef } from 'react'
import { getModelCapabilities } from '@/services/api/files'
import { DEFAULT_INPUT_CAPABILITIES } from '@/types/capabilities'
import type { ModelCapabilities, InputCapabilities } from '@/types/capabilities'

/** 能力缓存，避免重复请求 */
const capabilitiesCache = new Map<string, ModelCapabilities>()

/**
 * 从后端响应转换为前端 ModelCapabilities
 */
function transformCapabilities(data: Record<string, unknown>): ModelCapabilities {
  return {
    modelName: data.model_name as string,
    supportsImage: data.supports_image as boolean,
    supportedImageTypes: (data.supported_image_types as string[]) || [],
    maxImageSize: (data.max_image_size as number) || 0,
    supportsAudio: data.supports_audio as boolean,
    supportedAudioTypes: (data.supported_audio_types as string[]) || [],
    maxAudioSize: (data.max_audio_size as number) || 0,
    supportsVideo: data.supports_video as boolean,
    supportedVideoTypes: (data.supported_video_types as string[]) || [],
    maxVideoSize: (data.max_video_size as number) || 0,
    isMultimodal: data.is_multimodal as boolean,
  }
}

/**
 * 计算 InputCapabilities
 */
function computeInputCapabilities(capabilities: ModelCapabilities | null): InputCapabilities {
  if (!capabilities) {
    return DEFAULT_INPUT_CAPABILITIES
  }

  const {
    supportsImage,
    supportsAudio,
    supportsVideo,
    supportedImageTypes,
    supportedAudioTypes,
    supportedVideoTypes,
  } = capabilities

  // 文本/文档/代码附件始终可上传（任何模型都能接收文本），
  // 因此附件按钮始终显示；图片/音频/视频按多模态能力控制。

  // accept 仅含图片/音频/视频的多模态类型；文本类不进 accept
  // （accept 无法表达"任意文本"，且会限制用户选择文本文件）
  const acceptedTypes: string[] = []
  if (supportsImage) acceptedTypes.push(...supportedImageTypes)
  if (supportsAudio) acceptedTypes.push(...supportedAudioTypes)
  if (supportsVideo) acceptedTypes.push(...supportedVideoTypes)

  // 能力标签
  const capabilityTags: string[] = []
  if (supportsImage) capabilityTags.push('图片')
  if (supportsAudio) capabilityTags.push('音频')
  if (supportsVideo) capabilityTags.push('视频')

  return {
    showAttachmentButton: true,
    showImageUpload: supportsImage,
    showAudioUpload: supportsAudio,
    showVideoUpload: supportsVideo,
    canPasteImage: supportsImage,
    canDragDrop: true,
    acceptedFileTypes: acceptedTypes.join(','),
    capabilityTags,
  }
}

/**
 * 模型能力 Hook
 *
 * @param modelName - 当前模型名称
 * @returns 模型能力和输入能力配置
 */
export function useModelCapabilities(modelName: string | undefined) {
  const [capabilities, setCapabilities] = useState<ModelCapabilities | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  // 跟踪上一次请求的模型名称，避免重复请求
  const lastModelNameRef = useRef<string | undefined>(undefined)

  useEffect(() => {
    // 无效模型名称时清空能力
    if (!modelName || modelName === 'unknown') {
      setCapabilities(null)
      setLoading(false)
      setError(null)
      lastModelNameRef.current = modelName
      return
    }

    // 相同模型名称不重复请求
    if (modelName === lastModelNameRef.current) {
      return
    }

    lastModelNameRef.current = modelName

    // 检查缓存
    const cached = capabilitiesCache.get(modelName)
    if (cached) {
      setCapabilities(cached)
      setLoading(false)
      setError(null)
      return
    }

    // 发起请求
    setLoading(true)
    setError(null)

    getModelCapabilities(modelName)
      .then((response) => {
        const transformed = transformCapabilities(response as unknown as Record<string, unknown>)
        // 更新缓存
        capabilitiesCache.set(modelName, transformed)
        setCapabilities(transformed)
      })
      .catch(() => {
        // Silently fall back to default capabilities - endpoint may not exist
        setCapabilities(null)
      })
      .finally(() => {
        setLoading(false)
      })
  }, [modelName])

  // 计算输入能力配置
  const inputCapabilities: InputCapabilities = useMemo(() => {
    return computeInputCapabilities(capabilities)
  }, [capabilities])

  return {
    /** 模型能力（原始数据） */
    capabilities,
    /** 输入能力配置（用于控制输入组件） */
    inputCapabilities,
    /** 是否正在加载 */
    loading,
    /** 错误信息 */
    error,
  }
}

/**
 * 清除能力缓存（用于测试或强制刷新）
 */
export function clearCapabilitiesCache() {
  capabilitiesCache.clear()
}
