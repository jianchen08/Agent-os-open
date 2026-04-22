/**
 * 模型多模态能力类型定义
 *
 * 用于前端动态控制输入组件的显示和行为
 */

/**
 * 模型多模态能力（后端返回的完整能力信息）
 */
export interface ModelCapabilities {
  /** 模型名称 */
  modelName: string

  // 图片能力
  /** 是否支持图片 */
  supportsImage: boolean
  /** 支持的图片 MIME 类型列表 */
  supportedImageTypes: string[]
  /** 最大图片大小（字节） */
  maxImageSize: number

  // 音频能力
  /** 是否支持音频 */
  supportsAudio: boolean
  /** 支持的音频 MIME 类型列表 */
  supportedAudioTypes: string[]
  /** 最大音频大小（字节） */
  maxAudioSize: number

  // 视频能力
  /** 是否支持视频 */
  supportsVideo: boolean
  /** 支持的视频 MIME 类型列表 */
  supportedVideoTypes: string[]
  /** 最大视频大小（字节） */
  maxVideoSize: number

  // 文档能力
  /** 是否支持文档 */
  supportsDocument: boolean
  /** 支持的文档 MIME 类型列表 */
  supportedDocumentTypes: string[]
  /** 最大文档大小（字节） */
  maxDocumentSize: number

  // 代码文件能力
  /** 是否支持代码文件 */
  supportsCode: boolean
  /** 支持的代码文件 MIME 类型列表 */
  supportedCodeTypes: string[]
  /** 最大代码文件大小（字节） */
  maxCodeSize: number

  // 便捷属性
  /** 是否为多模态模型 */
  isMultimodal: boolean
}

/**
 * 输入能力配置（用于控制输入组件的显示）
 */
export interface InputCapabilities {
  /** 是否显示附件按钮 */
  showAttachmentButton: boolean
  /** 是否显示图片上传 */
  showImageUpload: boolean
  /** 是否显示音频上传 */
  showAudioUpload: boolean
  /** 是否显示视频上传 */
  showVideoUpload: boolean
  /** 是否显示文档上传 */
  showDocumentUpload: boolean
  /** 是否支持粘贴图片 */
  canPasteImage: boolean
  /** 是否支持拖拽上传 */
  canDragDrop: boolean
  /** 支持的文件类型（用于 file input 的 accept 属性） */
  acceptedFileTypes: string
  /** 能力标签（用于 UI 显示） */
  capabilityTags: string[]
}

/**
 * 默认输入能力配置（无多模态能力）
 */
export const DEFAULT_INPUT_CAPABILITIES: InputCapabilities = {
  showAttachmentButton: false,
  showImageUpload: false,
  showAudioUpload: false,
  showVideoUpload: false,
  showDocumentUpload: false,
  canPasteImage: false,
  canDragDrop: false,
  acceptedFileTypes: '',
  capabilityTags: [],
}
