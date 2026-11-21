/**
 * 录音转写格式归一：MediaRecorder 产物（audio/webm）→ 16-bit PCM WAV。
 *
 * 现状契约：服务端 ASR（智谱 GLM-ASR /audio/transcriptions）不收 webm 容器
 * （status=400 code=1214「transcriptions不支持当前文件格式」），而浏览器
 * MediaRecorder 的音频产出只能是 webm/opus——上传前统一转 WAV。
 * 解码不可用（无 AudioContext）或解码失败由调用方回退原 Blob，不阻断上传。
 */

/** 将各声道 Float32 采样编码为 WAV（16-bit PCM 交错） */
export function encodeWav(channels: Float32Array[], sampleRate: number): Blob {
  const numChannels = channels.length
  const numFrames = channels[0]?.length ?? 0
  const bytesPerSample = 2
  const blockAlign = numChannels * bytesPerSample
  const dataSize = numFrames * blockAlign
  const buffer = new ArrayBuffer(44 + dataSize)
  const view = new DataView(buffer)

  const writeString = (offset: number, value: string): void => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i))
  }

  writeString(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true) // fmt 块长度
  view.setUint16(20, 1, true) // 编码：1 = PCM
  view.setUint16(22, numChannels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * blockAlign, true) // 字节率
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, bytesPerSample * 8, true)
  writeString(36, 'data')
  view.setUint32(40, dataSize, true)

  let offset = 44
  for (let frame = 0; frame < numFrames; frame++) {
    for (let ch = 0; ch < numChannels; ch++) {
      const sample = Math.max(-1, Math.min(1, channels[ch][frame]))
      view.setInt16(offset, Math.round(sample < 0 ? sample * 0x8000 : sample * 0x7fff), true)
      offset += 2
    }
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

/**
 * 解码任意浏览器可解码的音频 Blob 并转单声道 WAV。
 *
 * 服务端 ASR 只收单声道（智谱 code=1214「transcriptions文件只支持单声道」），
 * 而 WebAudio 输出与部分设备麦克风为多声道——多声道按均值下混；解码上下文
 * 按 16kHz 构造（ASR 模型输入采样率），宿主不支持构造参数时回退默认采样率。
 *
 * @throws AudioContext 不可用或音频解码失败时抛错，由调用方回退原格式
 */
export async function convertBlobToWav(blob: Blob): Promise<Blob> {
  const Ctx =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctx) throw new Error('AudioContext 不可用')
  const ctx = createAudioContext(Ctx)
  try {
    const audioBuffer = await ctx.decodeAudioData(await blob.arrayBuffer())
    return encodeWav([downmixToMono(audioBuffer)], audioBuffer.sampleRate)
  } finally {
    void ctx.close()
  }
}

/** 以 16kHz 采样率构造 AudioContext；宿主不支持构造参数时回退默认采样率 */
function createAudioContext(Ctx: typeof AudioContext): AudioContext {
  try {
    return new Ctx({ sampleRate: 16000 })
  } catch {
    return new Ctx()
  }
}

/** 多声道按均值下混为单声道；单声道原样返回 */
function downmixToMono(audioBuffer: AudioBuffer): Float32Array {
  const numChannels = audioBuffer.numberOfChannels
  if (numChannels <= 1) return audioBuffer.getChannelData(0)
  const mixed = new Float32Array(audioBuffer.length)
  for (let ch = 0; ch < numChannels; ch++) {
    const data = audioBuffer.getChannelData(ch)
    for (let i = 0; i < data.length; i++) {
      mixed[i] += data[i] / numChannels
    }
  }
  return mixed
}
