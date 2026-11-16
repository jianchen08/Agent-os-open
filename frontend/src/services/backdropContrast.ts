/**
 * 实际渲染背景对比度统一强制（全主题 × 全 UI 同一条数据链）。
 *
 * 静态门禁（presets.contrast.test / translator）的背景基准是声明色，管不住
 * "真实渲染面"：背景图（立绘×overlay）、渐变、半透明面。本模块在主题应用后
 * 从实际渲染的背景栈解析出真实底色像素，对 WCAG 亮度包络强制全部接入主题
 * 控制的文字/图标令牌 ≥4.5:1——根级正文/次要（body 背景）、三个区域
 * （侧栏/聊天/工作区各自的面）、语义表面对（card/popover/secondary/primary/
 * accent 各对各自的 face）、双气泡（含玻璃面）；图片背景包络对黑白端点均
 * 不可达时按最小必要 alpha 加强 --bg-overlay（封顶 0.75）。所有主题经
 * themeStore 的 applyTheme 总装线走同一入口，结果按内容指纹记忆进主题数据，
 * 输入不变只算一次。
 *
 * 发射格式随消费面：`--foreground`/`--muted-foreground`/`--region-*-muted-fg`/
 * 语义 `--*-foreground` 走 `hsl(var(--x))` 包装链 → 三元组 "h s% l%"；
 * `--region-*-fg`/`--bubble-*-text`/`--ds-text-*` 走原始 color 位 → #rrggbb。
 *
 * fail-open：任何输入不可静态解析或图片加载失败 → 不发射，由 store 侧还原
 * 本轮写入者值（applyThemeToDOM 幂等全量重写），回退既有静态层。
 */
import { colorToRgb, relativeLuminance } from './themeValuePolicy'

/** WCAG 强制目标对比度（正文级 AA，与 translator _enforce_contrast 同一口径） */
const MIN_RATIO = 4.5
/** scrim 加强 alpha 封顶（再往上立绘细节损失过重，宁可端点兜底） */
const OVERLAY_ALPHA_CAP = 0.75

/** 栅格化缓冲宽高：垂直向 128 行保渐变精度，横向 32 列足够包络统计 */
const BUF_W = 32
const BUF_H = 128

export interface Rgb {
  r: number
  g: number
  b: number
}

export interface Rgba extends Rgb {
  a: number
}

/** 主题渲染上下文：全部来自声明值/内联变量（确定性可测，不读 computed） */
export interface RenderedBackdropContext {
  /** body 主背景声明值（纯色或渐变，body.style.background 的写入源） */
  mainBg: string
  /** 背景图层 url（内联 --bg-image 的 url() 实参；null=无图主题） */
  imageUrl: string | null
  /** 背景遮罩声明值（内联 --bg-overlay） */
  overlay: string | null
  /** 遮罩附加不透明度（内联 --bg-overlay-opacity，缺省 1） */
  overlayOpacity: number
  /** 聊天区区域面层序（外→内：--region-chat-bg → --chat-bg-color → --chat-bg-image） */
  chatLayers: Array<string | null>
  /** 侧栏区域面层序（外→内：--region-sidebar-bg → --ds-bg-panel → --sidebar-bg → --card） */
  sidebarLayers: Array<string | null>
  /** 工作区区域面层序（外→内：--region-workspace-bg → --ds-bg-elevated → --card） */
  workspaceLayers: Array<string | null>
  /** 正文色种子（--foreground 三元组或任意颜色串） */
  textSeed: string
  /** AI 气泡面声明（--bubble-ai-bg；可为 color-mix 玻璃面） */
  aiBubbleFace: string | null
  /** 用户气泡面声明（--bubble-user-bg；通常不透明品牌面） */
  userBubbleFace: string | null
  /** 用户气泡声明文字（--bubble-user-text，半透明面时的强制种子） */
  userBubbleText: string | null
  /** 卡面色（--card，flat 平铺气泡与区域链缺省底的合成基） */
  cardFallback: string | null
  /** AI 气泡平铺态（flat 缺省面 = 卡面 80% 玻璃） */
  flatBubble: boolean
  /** 语义表面对：face 为该面声明值，fg 为声明前景种子（消费面 hsl(var(--x)) → 发三元组） */
  surfacePairs: Array<{ token: string; face: string; fg: string }>
  /** 皮肤 --ds-text-* 声明值（原始 color 位消费 → 发 hex；缺省跳过） */
  dsTextTokens: { primary: string | null; secondary: string | null; muted: string | null }
}

export interface RegionTokens {
  /** 原始 color 位消费 → #rrggbb */
  fg: string
  /** 经 --muted-foreground 重绑进 hsl(var()) 包装 → 三元组 */
  mutedFg: string
}

/** 对比度强制结果（回写主题数据的最小持久形状） */
export interface BackdropTokens {
  /** 上下文内容指纹：输入不变 → 指纹不变 → 直接复用本结果 */
  fingerprint: string
  /** 根级正文/次要（body 背景包络；三元组，消费面 hsl(var(--foreground))） */
  foreground: string
  mutedForeground: string
  region: {
    sidebar: RegionTokens
    chat: RegionTokens
    workspace: RegionTokens
  }
  /** 气泡配对文字（仅半透明/flat 缺省面时非 null；不透明面保留声明成对值） */
  bubbleUserText: string | null
  bubbleAiText: string | null
  /** 语义表面对覆盖：'--card-foreground' → 三元组（仅解析成功的对） */
  surfaceOverrides: Record<string, string>
  /** --ds-text-* 覆盖：'--ds-text-primary' → #rrggbb */
  dsTextOverrides: Record<string, string>
  /** 加强后的遮罩 rgba()（null=无需加强，--bg-overlay 维持原发射值） */
  overlayOverride: string | null
}

// ── 颜色解析（声明值 → RGBA）──

/** 解析任意声明颜色：hex / rgb() / rgba() / 'transparent' / white/black / hsl() / HSL 三元组 */
export function parseColorValue(value: string): Rgba | null {
  const v = value.trim()
  if (!v) return null
  if (v === 'transparent' || v === 'none') return { r: 0, g: 0, b: 0, a: 0 }
  // white/black：DSH 翻译器 color-mix 派生面的第二色用命名色（--ds-bg-elevated
  // = canvas 82% + white）；其余命名色不收（fail-open 走 null）
  if (v === 'white') return { r: 255, g: 255, b: 255, a: 1 }
  if (v === 'black') return { r: 0, g: 0, b: 0, a: 1 }
  if (v.startsWith('#')) {
    const rgb = colorToRgb(v)
    return rgb ? { ...rgb, a: 1 } : null
  }
  const rgba = v.match(
    /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/i,
  )
  if (rgba) {
    return {
      r: parseInt(rgba[1], 10),
      g: parseInt(rgba[2], 10),
      b: parseInt(rgba[3], 10),
      a: rgba[4] !== undefined ? parseFloat(rgba[4]) : 1,
    }
  }
  return parseHslColor(v)
}

/** hsl()/hsla()（空格或逗号语法）或 shadcn 三元组（"h s% l%"）→ RGBA */
export function parseHslColor(value: string): Rgba | null {
  let v = value.trim()
  const wrapped = v.match(/^hsla?\((.*)\)$/i)
  if (wrapped) v = wrapped[1].replace(/,/g, ' ')
  const m = v.match(
    /^([\d.]+)(?:deg)?\s+([\d.]+)%\s+([\d.]+)%(?:\s*\/\s*([\d.]+))?$/,
  )
  if (!m) return null
  return hslToRgb(
    parseFloat(m[1]),
    parseFloat(m[2]),
    parseFloat(m[3]),
    m[4] !== undefined ? parseFloat(m[4]) : 1,
  )
}

function hslToRgb(h: number, s: number, l: number, a: number): Rgba {
  const c = (1 - Math.abs((2 * l) / 100 - 1)) * (s / 100)
  const hp = ((((h % 360) + 360) % 360) / 60)
  const x = c * (1 - Math.abs((hp % 2) - 1))
  const seg: Array<[number, number, number]> = [
    [c, x, 0],
    [x, c, 0],
    [0, c, x],
    [0, x, c],
    [x, 0, c],
    [c, 0, x],
  ]
  const [r1, g1, b1] = seg[Math.min(5, Math.floor(hp))]
  const m = l / 100 - c / 2
  return {
    r: Math.round((r1 + m) * 255),
    g: Math.round((g1 + m) * 255),
    b: Math.round((b1 + m) * 255),
    a,
  }
}

/** RGB → shadcn 三元组串（"h s% l%"，发射进 hsl(var(--x)) 包装链用） */
export function hslTriple(c: Rgb): string {
  const r = c.r / 255
  const g = c.g / 255
  const b = c.b / 255
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  let h = 0
  let s = 0
  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6
    else if (max === g) h = ((b - r) / d + 2) / 6
    else h = ((r - g) / d + 4) / 6
  }
  return `${Math.round(h * 360)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%`
}

/** 顶层逗号切分（忽略括号内逗号） */
function splitTopLevel(input: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of input) {
    if (ch === '(') depth++
    if (ch === ')') depth--
    if (ch === ',' && depth === 0) {
      out.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  if (cur.trim()) out.push(cur)
  return out
}

/** color-mix(in srgb, A p%, B) → RGBA（第二色任意；预乘 alpha 混合=CSS 语义） */
export function parseColorMix(value: string): Rgba | null {
  const inner = value.slice(value.indexOf('(') + 1, value.lastIndexOf(')'))
  const parts = splitTopLevel(inner)
  if (parts.length !== 3 || !/^\s*in\s+srgb\s*$/i.test(parts[0])) return null
  const pm = parts[1].match(/\s([\d.]+)%\s*$/)
  if (!pm) return null
  const a = parseColorValue(parts[1].slice(0, pm.index))
  const b = parseColorValue(parts[2])
  if (!a || !b) return null
  const t = parseFloat(pm[1]) / 100
  const outA = a.a * t + b.a * (1 - t)
  if (outA <= 0) return { r: 0, g: 0, b: 0, a: 0 }
  // 预乘 alpha：与透明混合保留通道只降 alpha（与浏览器 color-mix 一致）
  const ch = (x: number, y: number) => Math.round((x * a.a * t + y * b.a * (1 - t)) / outA)
  return { r: ch(a.r, b.r), g: ch(a.g, b.g), b: ch(a.b, b.b), a: outA }
}

/** rgba → hex 令牌：不透明发 6 位，带 alpha 发 8 位（#rrggbbaa，Chromium 62+） */
function rgbaToHexToken(c: Rgba): string {
  const h = (v: number) => Math.round(v).toString(16).padStart(2, '0')
  const base = `#${h(c.r)}${h(c.g)}${h(c.b)}`
  return c.a >= 0.999 ? base : `${base}${h(c.a * 255)}`
}

/**
 * color-mix(in srgb, …) 皮肤令牌静态等价转换（WebView 110 兼容注入面）。
 *
 * DSH 翻译器发射的皮肤令牌含 color-mix 派生面（--ds-bg-hover/--ds-bg-elevated/
 * --ds-text-secondary/--ds-text-muted/--ds-border-subtle/--bubble-ai-bg），
 * Chromium <111 不支持 color-mix → 消费位 IACVT（custom property 有值、var()
 * 展开后的消费声明整体失效，声明级 fallback 不救）。本函数在注入前把可静态
 * 解析的 srgb 混色转为等价 hex 令牌：混色走 parseColorMix（预乘 alpha 插值，
 * 与 111+ 浏览器 color-mix 同式，量化差 ≤1 LSB 视觉等价）；非 srgb 空间/
 * var() 引用/缺首百分比/不可解析色等形态原值透传（fail-open：110 维持现状，
 * 111+ 渲染零变化）。非 color-mix 值原样返回（幂等）。
 */
export function normalizeColorMix(value: string): string {
  if (!/^color-mix\(/i.test(value.trim())) return value
  const rgba = parseColorMix(value)
  return rgba ? rgbaToHexToken(rgba) : value
}

// ── 渐变解析（主题作者格式：linear-gradient([角度,] 色标…)）──

export interface GradientStop {
  pos: number
  color: Rgba
}

/** 解析 authored linear-gradient 色标序列；radial 等不支持形态返回 null */
export function parseGradientStops(value: string): GradientStop[] | null {
  const v = value.trim()
  if (!/^linear-gradient\(/i.test(v)) return null
  const inner = v.slice(v.indexOf('(') + 1, v.lastIndexOf(')'))
  const parts = splitTopLevel(inner)
  if (parts.length < 2) return null
  const stops: GradientStop[] = []
  for (const part of parts) {
    const seg = part.trim()
    if (/^-?[\d.]+deg$/.test(seg) || /^to\s+/i.test(seg)) continue
    const pm = seg.match(/\s([\d.]+)%\s*$/)
    const color = parseColorValue(pm ? seg.slice(0, pm.index) : seg)
    if (!color) return null
    stops.push({ pos: pm ? parseFloat(pm[1]) / 100 : -1, color })
  }
  if (stops.length < 2) return null
  // 缺省位次：首尾强制 0/1，中间未知段在相邻已知位次间均分（CSS 规则）
  if (stops[0].pos < 0) stops[0].pos = 0
  if (stops[stops.length - 1].pos < 0) stops[stops.length - 1].pos = 1
  for (let i = 1; i < stops.length - 1; i++) {
    if (stops[i].pos >= 0) continue
    let j = i
    while (j < stops.length - 1 && stops[j].pos < 0) j++
    for (let k = i; k < j; k++) {
      stops[k].pos =
        stops[i - 1].pos + ((stops[j].pos - stops[i - 1].pos) * (k - i + 1)) / (j - i + 1)
    }
  }
  return stops.sort((a, b) => a.pos - b.pos)
}

// ── 像素合成管线 ──

/** alpha-over 合成：top 叠加在 bottom 之上 → 新缓冲（等长缓冲，异长返回 bottom 兜底） */
export function compositeOver(
  bottom: Uint8ClampedArray,
  top: Uint8ClampedArray,
): Uint8ClampedArray {
  if (bottom.length !== top.length) return bottom
  const out = new Uint8ClampedArray(bottom.length)
  for (let i = 0; i < out.length; i += 4) {
    const a = top[i + 3] / 255
    out[i] = top[i] * a + bottom[i] * (1 - a)
    out[i + 1] = top[i + 1] * a + bottom[i + 1] * (1 - a)
    out[i + 2] = top[i + 2] * a + bottom[i + 2] * (1 - a)
    out[i + 3] = 255
  }
  return out
}

/** 与目标缓冲等长的单色半透明缓冲（overlay/玻璃面合成用） */
export function fillSolid(color: Rgba, byteLength: number): Uint8ClampedArray {
  const buf = new Uint8ClampedArray(byteLength)
  for (let i = 0; i < buf.length; i += 4) {
    buf[i] = color.r
    buf[i + 1] = color.g
    buf[i + 2] = color.b
    buf[i + 3] = Math.round(Math.max(0, Math.min(1, color.a)) * 255)
  }
  return buf
}

/** 纯色/渐变/color-mix 声明值 → 像素缓冲（垂直插值；不支持形态返回 null） */
export function rasterizeColorValue(value: string): Uint8ClampedArray | null {
  const stops = parseGradientStops(value)
  const buf = new Uint8ClampedArray(BUF_W * BUF_H * 4)
  if (stops) {
    for (let y = 0; y < BUF_H; y++) {
      const c = sampleStops(stops, y / (BUF_H - 1))
      fillRow(buf, y, c)
    }
    return buf
  }
  const solid = parseColorMix(value) ?? parseColorValue(value)
  if (!solid || solid.a <= 0) return null
  for (let y = 0; y < BUF_H; y++) fillRow(buf, y, solid)
  return buf
}

function sampleStops(stops: GradientStop[], t: number): Rgba {
  if (t <= stops[0].pos) return stops[0].color
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i].pos) {
      const p0 = stops[i - 1]
      const p1 = stops[i]
      const k = (t - p0.pos) / (p1.pos - p0.pos || 1)
      const ch = (a: number, b: number) => a + (b - a) * k
      return {
        r: Math.round(ch(p0.color.r, p1.color.r)),
        g: Math.round(ch(p0.color.g, p1.color.g)),
        b: Math.round(ch(p0.color.b, p1.color.b)),
        a: ch(p0.color.a, p1.color.a),
      }
    }
  }
  // t 超出末停位次（authored 渐变末停 <100%）：CSS 语义末色延展到末端
  return stops[stops.length - 1].color
}

function fillRow(buf: Uint8ClampedArray, y: number, c: Rgba): void {
  for (let x = 0; x < BUF_W; x++) {
    const i = (y * BUF_W + x) * 4
    buf[i] = c.r
    buf[i + 1] = c.g
    buf[i + 2] = c.b
    buf[i + 3] = Math.round(c.a * 255)
  }
}

/**
 * WCAG 亮度包络：按相对亮度排序取 p5/p95 位次像素的实色。
 * 图片主题走全图包络（⊇ cover 裁剪可视区，保守方向安全）。
 */
export function backdropEnvelope(data: Uint8ClampedArray): { low: Rgb; high: Rgb } {
  const n = data.length / 4
  const lums = new Float64Array(n)
  for (let i = 0; i < n; i++) {
    lums[i] = relativeLuminance({ r: data[i * 4], g: data[i * 4 + 1], b: data[i * 4 + 2] })
  }
  const idx = Array.from({ length: n }, (_, i) => i)
  idx.sort((a, b) => lums[a] - lums[b])
  const pick = (i: number): Rgb => ({
    r: data[i * 4],
    g: data[i * 4 + 1],
    b: data[i * 4 + 2],
  })
  return { low: pick(idx[Math.floor(0.05 * (n - 1))]), high: pick(idx[Math.ceil(0.95 * (n - 1))]) }
}

/** 前景向白/黑双向 22% 步进混色，取对全部背景最小对比度更高者（translator 同构） */
export function enforceStepwise(
  fg: Rgb,
  bgs: Rgb[],
  minRatio = MIN_RATIO,
  maxIter = 10,
): { color: Rgb; ratio: number } {
  const worst = (c: Rgb): number => Math.min(...bgs.map((b) => wcagRatio(c, b)))
  const fgRatio = worst(fg)
  if (fgRatio >= minRatio) return { color: fg, ratio: fgRatio }
  const run = (seed: Rgb, target: Rgb): { color: Rgb; ratio: number } => {
    let cur = seed
    let best = cur
    let bestR = worst(cur)
    for (let i = 0; i < maxIter; i++) {
      if (bestR >= minRatio) break
      cur = mixRgb(cur, target, 0.22)
      const r = worst(cur)
      if (r > bestR) {
        best = cur
        bestR = r
      }
    }
    // 步进网格停在端点前时以纯端点兜底（中间调面网格够不到 4.5）
    const tR = worst(target)
    if (bestR < minRatio && tR > bestR) {
      best = target
      bestR = tR
    }
    return { color: best, ratio: bestR }
  }
  // 均衡灰锚点（L≈0.179，对任何实底黑白两侧比值相等的唯一亮度）：中间调
  // 种子对"全跨包络"（两端横跨该亮度）时 22% 网格会跳过均衡点，从此锚点
  // 出发补齐机械保证
  const EQ_GRAY: Rgb = { r: 117, g: 117, b: 117 }
  const white = { r: 255, g: 255, b: 255 }
  const black = { r: 0, g: 0, b: 0 }
  return [run(fg, white), run(fg, black), run(EQ_GRAY, white), run(EQ_GRAY, black)].reduce(
    (a, b) => (b.ratio > a.ratio ? b : a),
  )
}

export function mixRgb(a: Rgb, b: Rgb, t: number): Rgb {
  return {
    r: Math.round(a.r * (1 - t) + b.r * t),
    g: Math.round(a.g * (1 - t) + b.g * t),
    b: Math.round(a.b * (1 - t) + b.b * t),
  }
}

export function wcagRatio(a: Rgb, b: Rgb): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

function rgbToHex(c: Rgb): string {
  const h = (v: number) =>
    Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')
  return `#${h(c.r)}${h(c.g)}${h(c.b)}`
}

/**
 * scrim 自适应：包络对黑白端点各绑定端均 < MIN_RATIO 时，二分最小遮罩有效
 * alpha（色相不变）使包络可满足；封顶仍不可满足返回封顶值（端点兜底）。
 * 基础 alpha 已可满足返回 null（无需加强）。三位小数向上量化：发射值只强不弱。
 */
export function solveOverlayAlpha(
  rawImage: Uint8ClampedArray,
  overlay: Rgba,
  baseEffectiveAlpha: number,
  minRatio = MIN_RATIO,
  cap = OVERLAY_ALPHA_CAP,
): number | null {
  const white = { r: 255, g: 255, b: 255 }
  const black = { r: 0, g: 0, b: 0 }
  const satisfiable = (alpha: number): boolean => {
    const mixed = compositeOver(rawImage, fillSolid({ ...overlay, a: alpha }, rawImage.length))
    const { low, high } = backdropEnvelope(mixed)
    // 包络 low≤high（按亮度排序）：白字的绑定端是 high、黑字的绑定端是 low
    return Math.max(wcagRatio(high, white), wcagRatio(low, black)) >= minRatio
  }
  if (satisfiable(baseEffectiveAlpha)) return null
  if (!satisfiable(cap)) return cap
  let lo = baseEffectiveAlpha
  let hi = cap
  for (let i = 0; i < 12; i++) {
    const mid = (lo + hi) / 2
    if (satisfiable(mid)) hi = mid
    else lo = mid
  }
  return Math.ceil(hi * 1000) / 1000
}

/** 气泡面解析：声明值 → 玻璃 RGBA；flat 缺省面 = 卡面 80% 玻璃 */
export function resolveBubbleFace(
  face: string | null,
  cardFallback: string | null,
  flatBubble: boolean,
): Rgba | null {
  if (face) {
    return parseColorMix(face) ?? parseColorValue(face)
  }
  if (flatBubble && cardFallback) {
    const card = parseHslColor(cardFallback) ?? parseColorValue(cardFallback)
    return card ? { ...card, a: card.a * 0.8 } : null
  }
  return null
}

// ── 背景栈构建（图片只解码一次，scrim 加强复用同 buffer）──

function parseOverlay(ctx: RenderedBackdropContext): Rgba | null {
  if (!ctx.overlay) return null
  return parseColorValue(ctx.overlay) ?? parseColorMix(ctx.overlay)
}

/**
 * body 底色栈：背景图×overlay（有图时覆盖主背景层），无图则主背景
 * （纯色/渐变）。image 为 null 且主背景不可解析 → null（fail-open）。
 */
export function buildBodyPixels(
  ctx: RenderedBackdropContext,
  image: Uint8ClampedArray | null,
  overlayAlphaOverride?: number,
): Uint8ClampedArray | null {
  let base: Uint8ClampedArray | null = null
  if (image) {
    base = image
    const overlayRgba = parseOverlay(ctx)
    if (overlayRgba && overlayRgba.a > 0) {
      const alpha =
        overlayAlphaOverride ?? Math.min(1, overlayRgba.a * ctx.overlayOpacity)
      base = compositeOver(base, fillSolid({ ...overlayRgba, a: alpha }, base.length))
    }
  } else {
    base = rasterizeColorValue(ctx.mainBg)
  }
  return base
}

/** 区域底色栈：body 底色之上按序叠加区域面层（透明/none 层跳过） */
export function buildRegionPixels(
  bodyPixels: Uint8ClampedArray,
  layers: Array<string | null>,
): Uint8ClampedArray {
  let base = bodyPixels
  for (const layer of layers) {
    if (!layer || layer === 'none' || layer === 'transparent') continue
    const top = rasterizeColorValue(layer)
    if (top) base = compositeOver(base, top)
  }
  return base
}

// ── 令牌计算（纯管线）──

/** 半透明面色压在实底上 → 实色 */
function blendSolid(bg: Rgb, face: Rgba): Rgb {
  const a = Math.max(0, Math.min(1, face.a))
  return {
    r: Math.round(face.r * a + bg.r * (1 - a)),
    g: Math.round(face.g * a + bg.g * (1 - a)),
    b: Math.round(face.b * a + bg.b * (1 - a)),
  }
}

function parseSeed(value: string | null | undefined): Rgb {
  return (value ? parseColorMix(value) ?? parseColorValue(value) : null) ?? {
    r: 255,
    g: 255,
    b: 255,
  }
}

/** 单一底色包络 → 区域令牌（fg hex 走原始 color 位；mutedFg 三元组走 hsl 包装链） */
function enforceRegion(pixels: Uint8ClampedArray, seed: Rgb): RegionTokens {
  const { low, high } = backdropEnvelope(pixels)
  const fg = enforceStepwise(seed, [low, high])
  const mutedSeed = mixRgb(seed, mixRgb(low, high, 0.5), 0.3)
  const muted = enforceStepwise(mutedSeed, [low, high])
  return { fg: rgbToHex(fg.color), mutedFg: hslTriple(muted.color) }
}

/** 从 body 底色栈计算全部令牌（根级 + 三区域 + 语义表面对 + 双气泡 + 皮肤 ds 文字） */
export function computeAllTokens(
  ctx: RenderedBackdropContext,
  bodyPixels: Uint8ClampedArray,
  fingerprint: string,
): BackdropTokens {
  const bodyEnv = backdropEnvelope(bodyPixels)
  const seed = parseSeed(ctx.textSeed)
  const fg = enforceStepwise(seed, [bodyEnv.low, bodyEnv.high])
  const mutedSeed = mixRgb(seed, mixRgb(bodyEnv.low, bodyEnv.high, 0.5), 0.3)
  const muted = enforceStepwise(mutedSeed, [bodyEnv.low, bodyEnv.high])

  // 语义表面对：face 静态栅格（半透明面压在 body 底色上=玻璃卡真实观感）
  const surfaceOverrides: Record<string, string> = {}
  for (const pair of ctx.surfacePairs) {
    const faceRgba = parseColorMix(pair.face) ?? parseColorValue(pair.face)
    if (!faceRgba || faceRgba.a <= 0) continue
    const facePx = rasterizeColorValue(pair.face)
    if (!facePx) continue
    const realPx =
      faceRgba.a < 0.99 ? compositeOver(bodyPixels, facePx) : facePx
    const env = backdropEnvelope(realPx)
    const enforced = enforceStepwise(parseSeed(pair.fg), [env.low, env.high])
    surfaceOverrides[pair.token] = hslTriple(enforced.color)
  }

  // 皮肤 --ds-text-*：对 body 包络强制（原始 color 位消费 → hex）
  const dsTextOverrides: Record<string, string> = {}
  const dsEntries: Array<[string, string | null]> = [
    ['--ds-text-primary', ctx.dsTextTokens.primary],
    ['--ds-text-secondary', ctx.dsTextTokens.secondary],
    ['--ds-text-muted', ctx.dsTextTokens.muted],
  ]
  for (const [token, declared] of dsEntries) {
    if (!declared) continue
    const enforced = enforceStepwise(parseSeed(declared), [bodyEnv.low, bodyEnv.high])
    dsTextOverrides[token] = rgbToHex(enforced.color)
  }

  // 气泡：面实色（半透明面压包络两端）；不透明面保留声明成对值
  let bubbleUserText: string | null = null
  const userFace = resolveBubbleFace(ctx.userBubbleFace, null, false)
  if (userFace && userFace.a < 0.99) {
    // 强制种子 = 声明文字色；缺省时取面色在包络两端的合成中点（均衡锚点兜底）
    const userSeed = ctx.userBubbleText
      ? parseSeed(ctx.userBubbleText)
      : mixRgb(blendSolid(bodyEnv.low, userFace), blendSolid(bodyEnv.high, userFace), 0.5)
    bubbleUserText = rgbToHex(
      enforceStepwise(userSeed, [
        blendSolid(bodyEnv.low, userFace),
        blendSolid(bodyEnv.high, userFace),
      ]).color,
    )
  }
  let bubbleAiText: string | null = null
  const aiFace = resolveBubbleFace(ctx.aiBubbleFace, ctx.cardFallback, ctx.flatBubble)
  if (aiFace && aiFace.a < 0.99) {
    bubbleAiText = rgbToHex(
      enforceStepwise(seed, [blendSolid(bodyEnv.low, aiFace), blendSolid(bodyEnv.high, aiFace)]).color,
    )
  }

  return {
    fingerprint,
    foreground: hslTriple(fg.color),
    mutedForeground: hslTriple(muted.color),
    region: {
      sidebar: enforceRegion(buildRegionPixels(bodyPixels, ctx.sidebarLayers), seed),
      chat: enforceRegion(buildRegionPixels(bodyPixels, ctx.chatLayers), seed),
      workspace: enforceRegion(buildRegionPixels(bodyPixels, ctx.workspaceLayers), seed),
    },
    bubbleUserText,
    bubbleAiText,
    surfaceOverrides,
    dsTextOverrides,
    overlayOverride: null,
  }
}

// ── 指纹（内容寻址记忆）──

/** 上下文指纹：任一相关输入变化 → 指纹变化 → 重算回写 */
export function fingerprintOf(ctx: RenderedBackdropContext): string {
  const parts = [
    ctx.mainBg,
    ctx.imageUrl ?? '',
    ctx.overlay ?? '',
    String(ctx.overlayOpacity),
    ...ctx.chatLayers.map((v) => v ?? ''),
    ...ctx.sidebarLayers.map((v) => v ?? ''),
    ...ctx.workspaceLayers.map((v) => v ?? ''),
    ctx.textSeed,
    ctx.aiBubbleFace ?? '',
    ctx.userBubbleFace ?? '',
    ctx.userBubbleText ?? '',
    ctx.cardFallback ?? '',
    String(ctx.flatBubble),
    ...ctx.surfacePairs.flatMap((p) => [p.token, p.face, p.fg]),
    ctx.dsTextTokens.primary ?? '',
    ctx.dsTextTokens.secondary ?? '',
    ctx.dsTextTokens.muted ?? '',
  ]
  let hash = 5381
  for (const part of parts) {
    for (let i = 0; i < part.length; i++) {
      hash = ((hash << 5) + hash + part.charCodeAt(i)) | 0
    }
  }
  return (hash >>> 0).toString(36)
}

// ── DOM 侧：上下文组装与图片采样 ──

/** 语义表面对清单（face 变量 → fg 变量；全部走 hsl(var(--x)) 包装链） */
const SURFACE_PAIRS: Array<[string, string]> = [
  ['--card', '--card-foreground'],
  ['--popover', '--popover-foreground'],
  ['--secondary', '--secondary-foreground'],
  ['--primary', '--primary-foreground'],
  ['--accent', '--accent-foreground'],
]

/** 从内联变量与 store 传参组装上下文（applyTheme 总装线在全部变量发射后调用） */
export function gatherBackdropContext(opts: {
  mainBg: string
  flatBubble: boolean
}): RenderedBackdropContext | null {
  if (typeof document === 'undefined') return null
  const root = document.documentElement
  const read = (name: string): string | null => {
    const v = root.style.getPropertyValue(name).trim()
    return v || null
  }
  const card = read('--card')
  // 与 index.css 让位规则对齐：背景图模式下聊天区实底让位（透出立绘），
  // 仅主题显式 --region-chat-bg 仍生效——color 槽不计入实际渲染面
  const hasBgImage = document.body.classList.contains('has-bg-image')
  const chatColorSlot = hasBgImage ? null : read('--chat-bg-color')
  return {
    mainBg: opts.mainBg,
    imageUrl: parseCssUrl(read('--bg-image')),
    overlay: read('--bg-overlay'),
    overlayOpacity: parseFloat(read('--bg-overlay-opacity') ?? '') || 1,
    chatLayers: [read('--region-chat-bg'), chatColorSlot, read('--chat-bg-image')],
    sidebarLayers: [read('--region-sidebar-bg'), read('--ds-bg-panel'), read('--sidebar-bg'), card],
    workspaceLayers: [read('--region-workspace-bg'), read('--ds-bg-elevated'), card],
    textSeed: read('--foreground') || '',
    aiBubbleFace: read('--bubble-ai-bg'),
    userBubbleFace: read('--bubble-user-bg'),
    userBubbleText: read('--bubble-user-text'),
    cardFallback: card,
    flatBubble: opts.flatBubble,
    surfacePairs: SURFACE_PAIRS.flatMap(([faceVar, fgVar]) => {
      const face = read(faceVar)
      const fg = read(fgVar)
      return face && fg ? [{ token: fgVar, face, fg }] : []
    }),
    dsTextTokens: {
      primary: read('--ds-text-primary'),
      secondary: read('--ds-text-secondary'),
      muted: read('--ds-text-muted'),
    },
  }
}

function parseCssUrl(value: string | null): string | null {
  if (!value) return null
  const m = value.match(/^url\((['"]?)(.*)\1\)$/)
  return m ? m[2] : null
}

/** 图片 → 降采样像素（统一 BUF_W×BUF_H 缓冲，与合成面等长）；失败/污染/无 2d 返回 null */
export function loadImagePixels(
  url: string,
  w = BUF_W,
  h = BUF_H,
): Promise<Uint8ClampedArray | null> {
  return new Promise((resolve) => {
    if (typeof window === 'undefined' || typeof Image === 'undefined') {
      resolve(null)
      return
    }
    const img = new Image()
    let settled = false
    const done = (v: Uint8ClampedArray | null): void => {
      if (!settled) {
        settled = true
        resolve(v)
      }
    }
    img.onload = () => {
      try {
        const canvas = document.createElement('canvas')
        canvas.width = w
        canvas.height = h
        const ctx2d = canvas.getContext('2d')
        if (!ctx2d) {
          done(null)
          return
        }
        ctx2d.drawImage(img, 0, 0, w, h)
        done(ctx2d.getImageData(0, 0, w, h).data)
      } catch {
        done(null)
      }
    }
    img.onerror = () => done(null)
    img.src = url
  })
}

// ── 令牌发射与记忆 ──

const STORAGE_PREFIX = 'backdrop-contrast:'

/** 结果发射：根级 + 三区域 + 双气泡 + 语义表面对 + 皮肤 ds 文字 + 加强遮罩 */
export function emitBackdropTokens(tokens: BackdropTokens): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  root.style.setProperty('--foreground', tokens.foreground)
  root.style.setProperty('--muted-foreground', tokens.mutedForeground)
  root.style.setProperty('--region-sidebar-fg', tokens.region.sidebar.fg)
  root.style.setProperty('--region-sidebar-muted-fg', tokens.region.sidebar.mutedFg)
  root.style.setProperty('--region-chat-fg', tokens.region.chat.fg)
  root.style.setProperty('--region-chat-muted-fg', tokens.region.chat.mutedFg)
  root.style.setProperty('--region-workspace-fg', tokens.region.workspace.fg)
  root.style.setProperty('--region-workspace-muted-fg', tokens.region.workspace.mutedFg)
  if (tokens.bubbleUserText) {
    root.style.setProperty('--bubble-user-text', tokens.bubbleUserText)
  }
  if (tokens.bubbleAiText) {
    root.style.setProperty('--bubble-ai-text', tokens.bubbleAiText)
  }
  for (const [token, value] of Object.entries(tokens.surfaceOverrides)) {
    root.style.setProperty(token, value)
  }
  for (const [token, value] of Object.entries(tokens.dsTextOverrides)) {
    root.style.setProperty(token, value)
  }
  if (tokens.overlayOverride) {
    root.style.setProperty('--bg-overlay', tokens.overlayOverride)
    // 加强值已含最终 alpha，附加不透明度归一避免二次折算
    root.style.setProperty('--bg-overlay-opacity', '1')
  }
}

function readCachedTokens(fingerprint: string): BackdropTokens | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + fingerprint)
    if (!raw) return null
    const parsed = JSON.parse(raw) as BackdropTokens
    return parsed?.fingerprint === fingerprint ? parsed : null
  } catch {
    return null
  }
}

function writeCachedTokens(tokens: BackdropTokens): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + tokens.fingerprint, JSON.stringify(tokens))
  } catch {
    // 配额满/隐私模式：记忆降级为会话内主题数据槽，不阻塞发射
  }
}

/** 进行中管线的最新指纹：快速切主题时旧异步结果不再发射（防跨主题串写） */
let latestFingerprint = ''

/**
 * 唯一公开编排入口：图片解码一次 → body 底色栈 → 全量令牌（根级+三区域+
 * 语义对+气泡+ds 文字）→ 图片主题 scrim 自适应加强（必要时按新包络重出）→
 * 发射 + 记忆。
 *
 * @param onResolved - 计算完成回调（主题数据回写槽；null=计算失败，store 侧
 *   还原本轮写入者值 applyThemeToDOM 幂等重写）
 */
export async function applyRenderedBackdropContrast(
  ctx: RenderedBackdropContext | null,
  onResolved: (tokens: BackdropTokens | null) => void,
): Promise<void> {
  if (!ctx) return
  const fingerprint = fingerprintOf(ctx)
  latestFingerprint = fingerprint

  const cached = readCachedTokens(fingerprint)
  if (cached) {
    emitBackdropTokens(cached)
    onResolved(cached)
    return
  }

  const image = ctx.imageUrl ? await loadImagePixels(ctx.imageUrl) : null
  if (ctx.imageUrl && !image) {
    if (latestFingerprint === fingerprint) onResolved(null)
    return
  }
  let bodyPixels = buildBodyPixels(ctx, image)
  if (!bodyPixels || latestFingerprint !== fingerprint) {
    if (latestFingerprint === fingerprint) onResolved(null)
    return
  }

  let tokens = computeAllTokens(ctx, bodyPixels, fingerprint)
  const overlayRgba = parseOverlay(ctx)
  if (image && overlayRgba && overlayRgba.a > 0) {
    const baseEffective = Math.min(1, overlayRgba.a * ctx.overlayOpacity)
    const alpha = solveOverlayAlpha(image, overlayRgba, baseEffective)
    if (alpha !== null) {
      bodyPixels = buildBodyPixels(ctx, image, alpha)
      if (bodyPixels) {
        tokens = {
          ...computeAllTokens(ctx, bodyPixels, fingerprint),
          overlayOverride: rgbaString(overlayRgba, alpha),
        }
      }
    }
  }

  if (latestFingerprint !== fingerprint) return
  emitBackdropTokens(tokens)
  writeCachedTokens(tokens)
  onResolved(tokens)
}

function rgbaString(base: Rgba, alpha: number): string {
  const a = parseFloat(Math.max(0, Math.min(1, alpha)).toFixed(3))
  return `rgba(${Math.round(base.r)}, ${Math.round(base.g)}, ${Math.round(base.b)}, ${a})`
}
