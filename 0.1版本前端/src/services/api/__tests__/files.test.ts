/**
 * 文件校验逻辑测试
 *
 * 验证 validateFile 的两条核心规则：
 * 1. 文本/文档/代码类附件宽规则放行（任何模型都能接收文本）
 * 2. 图片/音频/视频按模型多模态能力校验
 */

/* eslint-disable import-x/order */
import { describe, expect, it } from 'vitest'
import { getFileCategory, isTextLikeFile, validateFile } from '@/services/api/files'
import type { ModelCapabilities } from '@/types/capabilities'

/** 构造 File 对象的辅助函数 */
function makeFile(name: string, type: string, size = 1024): File {
  const file = new File([new ArrayBuffer(size)], name, { type })
  // File.size 由内容决定，但测试需要可控大小，直接覆盖
  Object.defineProperty(file, 'size', { value: size, configurable: true })
  return file
}

/** 模型支持图片的能力 */
const imageCap: ModelCapabilities = {
  modelName: 'glm-5.2',
  supportsImage: true,
  supportedImageTypes: ['image/jpeg', 'image/png'],
  maxImageSize: 20 * 1024 * 1024,
  supportsAudio: false,
  supportedAudioTypes: [],
  maxAudioSize: 0,
  supportsVideo: false,
  supportedVideoTypes: [],
  maxVideoSize: 0,
  isMultimodal: true,
}

describe('isTextLikeFile - 文本类判定', () => {
  it('纯文本 MIME 判定为文本类', () => {
    expect(isTextLikeFile(makeFile('a.txt', 'text/plain'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.md', 'text/markdown'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.py', 'text/x-python'))).toBe(true)
  })

  it('结构化文本 MIME 判定为文本类', () => {
    expect(isTextLikeFile(makeFile('a.json', 'application/json'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.xml', 'application/xml'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.yaml', 'application/x-yaml'))).toBe(true)
  })

  it('非标准文本 MIME（application/text 等）判定为文本类', () => {
    expect(isTextLikeFile(makeFile('a.txt', 'application/text'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.log', 'application/octet-stream'))).toBe(true)
  })

  it('MIME 未知但扩展名为文本/代码时按扩展名兜底放行', () => {
    expect(isTextLikeFile(makeFile('app.py', 'application/octet-stream'))).toBe(true)
    expect(isTextLikeFile(makeFile('conf.cfg', 'application/octet-stream'))).toBe(true)
    expect(isTextLikeFile(makeFile('data.csv', ''))).toBe(true)
  })

  it('markitdown 文档扩展名判定为文本类（即使 MIME 未知）', () => {
    expect(isTextLikeFile(makeFile('a.pdf', 'application/pdf'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.docx', 'application/octet-stream'))).toBe(true)
    expect(isTextLikeFile(makeFile('a.xlsx', 'application/vnd.openxmlformats'))).toBe(true)
  })

  it('图片/音频/视频不判定为文本类', () => {
    expect(isTextLikeFile(makeFile('a.png', 'image/png'))).toBe(false)
    expect(isTextLikeFile(makeFile('a.mp3', 'audio/mpeg'))).toBe(false)
    expect(isTextLikeFile(makeFile('a.mp4', 'video/mp4'))).toBe(false)
  })
})

describe('validateFile - 文本类宽规则放行', () => {
  it('text/plain 放行（无需 capabilities）', () => {
    const result = validateFile(makeFile('notes.txt', 'text/plain'))
    expect(result.valid).toBe(true)
  })

  it('代码文件（text/x-python）放行', () => {
    expect(validateFile(makeFile('app.py', 'text/x-python')).valid).toBe(true)
  })

  it('application/json 放行', () => {
    expect(validateFile(makeFile('data.json', 'application/json')).valid).toBe(true)
  })

  it('PDF 文档放行（markitdown 可转）', () => {
    expect(validateFile(makeFile('doc.pdf', 'application/pdf')).valid).toBe(true)
  })

  it('DOCX 文档放行（markitdown 可转）', () => {
    expect(validateFile(makeFile('doc.docx', 'application/octet-stream')).valid).toBe(true)
  })

  it('文本类超过 10MB 拒绝', () => {
    const big = makeFile('big.txt', 'text/plain', 11 * 1024 * 1024)
    const result = validateFile(big)
    expect(result.valid).toBe(false)
    expect(result.error).toContain('超过限制')
  })

  it('纯文本模型（无多模态能力）也能发文本附件', () => {
    // 这是核心需求：任何模型都能接收文本
    const noCap: ModelCapabilities = {
      modelName: 'text-only',
      supportsImage: false,
      supportedImageTypes: [],
      maxImageSize: 0,
      supportsAudio: false,
      supportedAudioTypes: [],
      maxAudioSize: 0,
      supportsVideo: false,
      supportedVideoTypes: [],
      maxVideoSize: 0,
      isMultimodal: false,
    }
    expect(validateFile(makeFile('a.txt', 'text/plain'), noCap).valid).toBe(true)
    expect(validateFile(makeFile('a.pdf', 'application/pdf'), noCap).valid).toBe(true)
  })
})

describe('validateFile - 多模态按能力校验', () => {
  it('图片在能力范围内放行', () => {
    expect(validateFile(makeFile('a.jpg', 'image/jpeg'), imageCap).valid).toBe(true)
  })

  it('图片 MIME 不在 supported_image_types 内拒绝', () => {
    const result = validateFile(makeFile('a.gif', 'image/gif'), imageCap)
    expect(result.valid).toBe(false)
    expect(result.error).toContain('不支持的图片类型')
  })

  it('模型不支持图片时拒绝', () => {
    const result = validateFile(makeFile('a.jpg', 'image/jpeg'), {
      ...imageCap,
      supportsImage: false,
    })
    expect(result.valid).toBe(false)
    expect(result.error).toContain('不支持图片')
  })

  it('图片超过 max_image_size 拒绝', () => {
    const result = validateFile(makeFile('big.jpg', 'image/jpeg', 25 * 1024 * 1024), imageCap)
    expect(result.valid).toBe(false)
    expect(result.error).toContain('超过限制')
  })

  it('音频在能力范围内放行', () => {
    const audioCap: ModelCapabilities = {
      ...imageCap,
      supportsAudio: true,
      supportedAudioTypes: ['audio/mpeg'],
      maxAudioSize: 5 * 1024 * 1024,
    }
    expect(validateFile(makeFile('a.mp3', 'audio/mpeg'), audioCap).valid).toBe(true)
  })

  it('模型不支持音频时拒绝音频', () => {
    const result = validateFile(makeFile('a.mp3', 'audio/mpeg'), imageCap)
    expect(result.valid).toBe(false)
    expect(result.error).toContain('不支持音频')
  })

  it('不支持的二进制类型（如 zip）拒绝', () => {
    const result = validateFile(makeFile('a.zip', 'application/zip'))
    expect(result.valid).toBe(false)
    expect(result.error).toContain('不支持的文件类型')
  })
})

describe('getFileCategory - 文件分类', () => {
  it('按 MIME 前缀分类', () => {
    expect(getFileCategory('image/png')).toBe('image')
    expect(getFileCategory('audio/mpeg')).toBe('audio')
    expect(getFileCategory('video/mp4')).toBe('video')
  })

  it('纯文本归为 text', () => {
    expect(getFileCategory('text/plain')).toBe('text')
    expect(getFileCategory('application/json')).toBe('text')
  })

  it('未知二进制归为 unknown', () => {
    expect(getFileCategory('application/zip')).toBe('unknown')
    expect(getFileCategory('')).toBe('unknown')
  })
})
