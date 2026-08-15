/*
 * AgentOS 适配层（task_dsh_plugin_adapter 任务 3）：anser 依赖的内联替代。
 *
 * DSH 的 ansi.ts 经 anser 的 ansiToJson() 把 SGR 渐变切分成带色 run；为避免
 * 为一个函数引入 npm 依赖（安装污染风险），此处按 anser 的 JSON 契约
 * （{content, fg, bg, decorations}，fg/bg 为 "r, g, b" 三元组或 null）实现
 * SGR 解析：0-9 属性、30-37/90-97 前景、40-47/100-107 背景、38/48 的 5;n
 * 256 色板与 2;r;g;b 真彩、reverse 前景背景交换、39/49 与 21-29 属性复位。
 * 非 SGR 的 CSI 序列（残余光标控制等）按 anser 语义丢弃。
 */

/** anser JSON chunk 契约的本地实现（见 ansi.ts 的 AnsiChunk）。 */
export interface AnsiJsonChunk {
  content: string
  fg: string | null
  bg: string | null
  decorations: readonly string[]
}

/** 基本 16 色（anser 的 xterm 值，ansi.ts 的 TOKEN_BY_BASIC_RGB 按这些三元组查表）。 */
const BASIC_COLORS: readonly string[] = [
  '0,0,0', '187,0,0', '0,187,0', '187,187,0', '0,0,187', '187,0,187', '0,187,187', '255,255,255',
  '85,85,85', '255,85,85', '0,255,0', '255,255,85', '85,85,255', '255,85,255', '85,255,255', '255,255,255',
]

/** 256 色板：16 基本色的扩展（6x6x6 立方体 + 灰阶）。 */
function palette256(n: number): string {
  if (n < 16) return BASIC_COLORS[n]
  if (n < 232) {
    const steps = [0, 95, 135, 175, 215, 255]
    const i = n - 16
    const r = steps[Math.floor(i / 36)]
    const g = steps[Math.floor((i % 36) / 6)]
    const b = steps[i % 6]
    return `${r},${g},${b}`
  }
  const v = 8 + (n - 232) * 10
  return `${v},${v},${v}`
}

/** SGR 解析中的活动样式状态。 */
interface SgrState {
  fg: string | null
  bg: string | null
  decorations: string[]
}

const DECORATION_NAMES: Record<number, string> = {
  1: 'bold', 2: 'dim', 3: 'italic', 4: 'underline', 8: 'hidden', 9: 'strikethrough',
}

function applySgr(state: SgrState, params: readonly number[]): void {
  let i = 0
  while (i < params.length) {
    const p = params[i]
    if (p === 0) {
      state.fg = null
      state.bg = null
      state.decorations = []
    } else if (DECORATION_NAMES[p] !== undefined) {
      const name = DECORATION_NAMES[p]
      if (!state.decorations.includes(name)) state.decorations = [...state.decorations, name]
    } else if (p === 7) {
      // reverse：anser 以交换前景/背景消费它
      const fg = state.fg
      state.fg = state.bg
      state.bg = fg
    } else if (p === 21 || p === 22) {
      state.decorations = state.decorations.filter(d => d !== 'bold' && d !== 'dim')
    } else if (p === 23) {
      state.decorations = state.decorations.filter(d => d !== 'italic')
    } else if (p === 24) {
      state.decorations = state.decorations.filter(d => d !== 'underline')
    } else if (p === 27) {
      const fg = state.fg
      state.fg = state.bg
      state.bg = fg
    } else if (p === 28) {
      state.decorations = state.decorations.filter(d => d !== 'hidden')
    } else if (p === 29) {
      state.decorations = state.decorations.filter(d => d !== 'strikethrough')
    } else if (p >= 30 && p <= 37) {
      state.fg = BASIC_COLORS[p - 30]
    } else if (p === 39) {
      state.fg = null
    } else if (p >= 40 && p <= 47) {
      state.bg = BASIC_COLORS[p - 40]
    } else if (p === 49) {
      state.bg = null
    } else if (p >= 90 && p <= 97) {
      state.fg = BASIC_COLORS[p - 90 + 8]
    } else if (p >= 100 && p <= 107) {
      state.bg = BASIC_COLORS[p - 100 + 8]
    } else if ((p === 38 || p === 48) && i + 1 < params.length) {
      const mode = params[i + 1]
      if (mode === 5 && i + 2 < params.length) {
        const rgb = palette256(Math.max(0, Math.min(255, params[i + 2])))
        if (p === 38) state.fg = rgb
        else state.bg = rgb
        i += 2
      } else if (mode === 2 && i + 4 < params.length) {
        const rgb = `${params[i + 2]},${params[i + 3]},${params[i + 4]}`
        if (p === 38) state.fg = rgb
        else state.bg = rgb
        i += 4
      }
    }
    i += 1
  }
}

/**
 * 把（已 sanitize 的）终端输出切成 SGR run。等价于
 * `Anser.ansiToJson(text, { json: true, remove_empty: true })`。
 * @param text - 已移除非 CSI 转义与惰性控制符的输出文本。
 * @returns 按 SGR 状态切换切分的 chunk 序列（空内容 run 被丢弃）。
 */
export function ansiToJson(text: string): readonly AnsiJsonChunk[] {
  const chunks: AnsiJsonChunk[] = []
  const state: SgrState = { fg: null, bg: null, decorations: [] }
  let buffer = ''
  let bufferedState: SgrState | null = null

  const flush = () => {
    if (buffer !== '') {
      const snapshot = bufferedState ?? state
      chunks.push({
        content: buffer,
        fg: snapshot.fg,
        bg: snapshot.bg,
        decorations: snapshot.decorations,
      })
    }
    buffer = ''
    bufferedState = null
  }

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (ch === '\u001b' && text[i + 1] === '[') {
      // CSI：读参数区（0x30-0x3F）+ 中间字节（0x20-0x2F）到最终字节（0x40-0x7E）。
      let j = i + 2
      while (j < text.length && text.charCodeAt(j) >= 0x20 && text.charCodeAt(j) <= 0x3f) j += 1
      const finalByte = j < text.length ? text[j] : ''
      if (finalByte === 'm') {
        flush()
        const paramStr = text.slice(i + 2, j)
        const params = (paramStr === '' ? ['0'] : paramStr.split(';')).map(s => (s === '' ? 0 : Number.parseInt(s, 10)))
        applySgr(state, params)
      }
      // 非 SGR 的 CSI 整体丢弃（anser 语义）
      i = j
      continue
    }
    if (ch === '\u001b') continue // 孤立 ESC，丢弃
    if (bufferedState === null && buffer === '') bufferedState = { ...state, decorations: [...state.decorations] }
    buffer += ch
  }
  flush()
  return chunks
}
