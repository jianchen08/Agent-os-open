// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * audioWav 单元测试：WAV 头结构 / 16-bit PCM 采样 / 交错 / 削波 / Blob 转码
 *
 * 行为契约（服务端 ASR 只收 WAV，不收 MediaRecorder 的 webm 容器）：
 * - encodeWav 产出标准 44 字节头 + 16-bit PCM 的交错采样
 * - convertBlobToWav 解码失败或 AudioContext 缺失时抛错（调用方回退原格式）
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { convertBlobToWav, encodeWav } from '@/utils/audioWav'

/** 读取 Blob 并解析 WAV 头（返回关键字段 + DataView 供采样断言） */
async function parseWav(blob: Blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  const view = new DataView(bytes.buffer)
  const tag = (offset: number) => String.fromCharCode(...bytes.slice(offset, offset + 4))
  return {
    bytes,
    view,
    riff: tag(0),
    riffSize: view.getUint32(4, true),
    wave: tag(8),
    fmt: tag(12),
    fmtSize: view.getUint32(16, true),
    audioFormat: view.getUint16(20, true),
    channels: view.getUint16(22, true),
    sampleRate: view.getUint32(24, true),
    byteRate: view.getUint32(28, true),
    blockAlign: view.getUint16(32, true),
    bitsPerSample: view.getUint16(34, true),
    dataTag: tag(36),
    dataSize: view.getUint32(40, true),
    sample: (index: number) => view.getInt16(44 + index * 2, true),
  }
}

describe('audioWav.encodeWav', () => {
  it('输出标准 44 字节 WAV 头（单声道 16-bit）', async () => {
    const wav = encodeWav([new Float32Array(4)], 16000)
    expect(wav.type).toBe('audio/wav')
    const parsed = await parseWav(wav)
    expect(parsed.riff).toBe('RIFF')
    expect(parsed.wave).toBe('WAVE')
    expect(parsed.fmt).toBe('fmt ')
    expect(parsed.dataTag).toBe('data')
    expect(parsed.fmtSize).toBe(16)
    expect(parsed.audioFormat).toBe(1)
    expect(parsed.channels).toBe(1)
    expect(parsed.sampleRate).toBe(16000)
    expect(parsed.bitsPerSample).toBe(16)
    expect(parsed.blockAlign).toBe(2)
    expect(parsed.byteRate).toBe(16000 * 2)
    expect(parsed.dataSize).toBe(4 * 2)
    expect(parsed.riffSize).toBe(36 + parsed.dataSize)
    expect(parsed.bytes.length).toBe(44 + parsed.dataSize)
  })

  it('双声道按帧交错写入，采样按 16-bit 量化', async () => {
    const left = new Float32Array([0.5, -0.5])
    const right = new Float32Array([0.25, 0])
    const parsed = await parseWav(encodeWav([left, right], 48000))
    expect(parsed.channels).toBe(2)
    expect(parsed.blockAlign).toBe(4)
    expect(parsed.dataSize).toBe(2 * 4)
    expect(parsed.sample(0)).toBe(16384) // L0: round(0.5*32767)
    expect(parsed.sample(1)).toBe(8192) // R0: round(0.25*32767)
    expect(parsed.sample(2)).toBe(-16384) // L1: round(-0.5*32768)
    expect(parsed.sample(3)).toBe(0) // R1
  })

  it('超界采样削波到 [-32768, 32767]', async () => {
    const parsed = await parseWav(encodeWav([new Float32Array([2, -2])], 8000))
    expect(parsed.sample(0)).toBe(32767)
    expect(parsed.sample(1)).toBe(-32768)
  })
})

describe('audioWav.convertBlobToWav', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('以 16kHz 采样率构造 AudioContext 并转 WAV（采样率沿用解码结果）', async () => {
    const channel = new Float32Array([0.1, 0.2, 0.3])
    const ctorArgs: Array<AudioContextOptions | undefined> = []
    class FakeAudioContext {
      constructor(options?: AudioContextOptions) {
        ctorArgs.push(options)
      }
      decodeAudioData = vi.fn(async () => ({
        sampleRate: 16000,
        numberOfChannels: 1,
        getChannelData: () => channel,
      }))
      close = vi.fn(async () => {})
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const wav = await convertBlobToWav(new Blob(['webm'], { type: 'audio/webm' }))
    expect(wav.type).toBe('audio/wav')
    expect(ctorArgs).toEqual([{ sampleRate: 16000 }])
    const parsed = await parseWav(wav)
    expect(parsed.sampleRate).toBe(16000)
    expect(parsed.dataSize).toBe(3 * 2)
  })

  it('宿主不支持 sampleRate 构造参数时回退默认构造（采样率取解码结果）', async () => {
    const ctorArgs: Array<AudioContextOptions | undefined> = []
    class FakeAudioContext {
      constructor(options?: AudioContextOptions) {
        ctorArgs.push(options)
        if (options) throw new TypeError('sampleRate not supported')
      }
      decodeAudioData = vi.fn(async () => ({
        sampleRate: 48000,
        numberOfChannels: 1,
        getChannelData: () => new Float32Array([0.5]),
      }))
      close = vi.fn(async () => {})
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const parsed = await parseWav(await convertBlobToWav(new Blob(['webm'])))
    expect(ctorArgs).toEqual([{ sampleRate: 16000 }, undefined])
    expect(parsed.sampleRate).toBe(48000)
  })

  it('多声道输入下混为单声道（服务端 ASR 只收单声道）', async () => {
    const left = new Float32Array([1, 0])
    const right = new Float32Array([0, 1])
    class FakeAudioContext {
      constructor(_options?: AudioContextOptions) {}
      decodeAudioData = vi.fn(async () => ({
        sampleRate: 8000,
        numberOfChannels: 2,
        length: 2,
        getChannelData: (ch: number) => (ch === 0 ? left : right),
      }))
      close = vi.fn(async () => {})
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const parsed = await parseWav(await convertBlobToWav(new Blob(['webm'])))
    expect(parsed.channels).toBe(1)
    expect(parsed.dataSize).toBe(2 * 2)
    expect(parsed.sample(0)).toBe(16384) // (1+0)/2 = 0.5
    expect(parsed.sample(1)).toBe(16384) // (0+1)/2 = 0.5
  })

  it('AudioContext 不可用 → 抛错（调用方回退原格式）', async () => {
    // jsdom 默认无 AudioContext；显式清空避免宿主注入
    vi.stubGlobal('AudioContext', undefined)
    await expect(convertBlobToWav(new Blob(['x']))).rejects.toThrow('AudioContext 不可用')
  })
})
