/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * 组件级颜色对门禁：TSX 层级扫描 × 全量预设主题 WCAG 对比度
 *
 * 背景：presets.contrast.test.ts 只校验主题配置内的成对颜色（text ↔ background、
 * bubble_user_text ↔ bubble_user_bg 等）；组件消费层「跨对接线」不在其射程——
 * MessageItem 编辑框 bg-background（foreground/background 对的底）继承了气泡容器
 * 内联 color var(--bubble-user-text)（bubble 对的字），浅色主题下近白字压近白底
 * 不可见（2026-09-30 用户报告，实锤两处：编辑框 + 用户消息附件行）。
 *
 * 本门禁静态解析全部 TSX 的 JSX 层级：收集每个元素 className / 内联 style 显式
 * 声明的 bg/fg 颜色 token，沿祖先链求「有效前景 × 有效背景」配对，在全量预设
 * 主题下计算 WCAG 对比度（半透明 bg 压父底合成、渐变取全部色标候选取最差），
 * min < 3.0（WCAG 可视下限，与 presets 门禁 VISIBLE 同源）判违例。
 * 历史债进 .github/ui-color-pair-baseline.txt：违例集合与基线**精确相等**——
 * 新增违例红（禁止新增），清偿后基线残留行也红（只减不增棘轮）。
 *
 * 检测器边界（v1）：
 * - 只扫 base-state 类（hover:/dark: 等变体前缀不计，也不作前景屏障）；
 * - style={变量} 解析到同文件 `const 变量 = {...}` 初始化器；`变量.prop =` 事后
 *   赋值不追踪（MessageItem flat 模式成对赋值 bg/color 同步置值，初始化器的
 *   三元分支已覆盖其真实配对组合）；
 * - 内联 style 三元分支按位置配对（isUser ? A : B × isUser ? C : D →
 *   (A,C),(B,D)），候选数不等时退化为叉积；
 * - 未知 token（自研 CSS 类、Tailwind 调色板类）视为屏障：其子树停止继承判定；
 * - antd/RJSF 组件内部主题、CSS 文件级联不在扫描面（presets 配置级门禁兜底）。
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'
import { presetThemes } from '@/config/themes'
import {
  colorToRgb,
  contrastPick,
  extractSolidFromGradient,
  hexToRgb,
  wcagRatio,
} from '@/services/themeValuePolicy'
import type { ThemeConfig } from '@/types/theme'

// vitest 恒以 frontend 根为 cwd（CI 与本地一致）；import.meta.url 经 vite 变换
// 后非 file 协议，不可用
const SRC_DIR = resolve(process.cwd(), 'src')
const BASELINE_FILE = resolve(process.cwd(), '..', '.github', 'ui-color-pair-baseline.txt')
/** 可视下限：与 presets.contrast.test.ts 的 VISIBLE 同值（辅助可见 ≥3.0） */
const VISIBLE_RATIO = 3.0

// ---------------------------------------------------------------------------
// 颜色数学（复用 themeValuePolicy；alpha 合成本地实现）
// ---------------------------------------------------------------------------

interface RgbA {
  r: number
  g: number
  b: number
  a: number
}

function parseRgba(value: string): RgbA | null {
  const hex = value.trim().match(/^#([0-9a-f]{6})$/i)
  if (hex) {
    return {
      r: parseInt(hex[1].slice(0, 2), 16),
      g: parseInt(hex[1].slice(2, 4), 16),
      b: parseInt(hex[1].slice(4, 6), 16),
      a: 1,
    }
  }
  const rgba = value
    .trim()
    .match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/)
  if (rgba) {
    return { r: +rgba[1], g: +rgba[2], b: +rgba[3], a: rgba[4] !== undefined ? +rgba[4] : 1 }
  }
  return null
}

function composite(fg: RgbA, bg: RgbA): RgbA {
  return {
    r: Math.round(fg.r * fg.a + bg.r * (1 - fg.a)),
    g: Math.round(fg.g * fg.a + bg.g * (1 - fg.a)),
    b: Math.round(fg.b * fg.a + bg.b * (1 - fg.a)),
    a: 1,
  }
}

/** 渐变/复杂值的实色候选（全部色标，评估取最差） */
function gradientStops(value: string): string[] {
  return value.match(/#[0-9a-f]{6}\b/gi) ?? []
}

// ---------------------------------------------------------------------------
// token → 主题取值映射（口径 = themeService.ts 的 CSS 变量发射规则）
// ---------------------------------------------------------------------------

/** bg token → 主题配置取值（语义表面色） */
const BG_TOKENS: Record<string, (t: ThemeConfig) => string> = {
  background: (t) => t.colors.background.main,
  sidebar: (t) => t.colors.background.sidebar,
  card: (t) => t.colors.background.card,
  popover: (t) => t.colors.background.elevated,
  elevated: (t) => t.colors.background.elevated,
  input: (t) => t.colors.background.input,
  muted: (t) => t.colors.background.input,
  primary: (t) => t.colors.primary,
  secondary: (t) => t.colors.secondary,
  accent: (t) => t.colors.accent,
  destructive: (t) => t.colors.status.error,
  'bubble-user': (t) => t.colors.bubble.user_bg,
  'bubble-ai': (t) => t.colors.bubble.ai_bg,
  'status-success': (t) => t.colors.status.success,
  'status-warning': (t) => t.colors.status.warning,
  'status-error': (t) => t.colors.status.error,
  'status-info': (t) => t.colors.status.info,
  'status-running': (t) => t.colors.status.running,
  'status-pending': (t) => t.colors.status.pending,
  white: () => '#ffffff',
  black: () => '#000000',
}

/** 气泡内链接色：与 themeService pushCoreColorVars 的 --bubble-link 判据一致 */
function bubbleLinkValue(t: ThemeConfig): string {
  const userBg = t.colors.bubble.user_bg
  const face =
    colorToRgb(userBg) ??
    (() => {
      const solid = extractSolidFromGradient(userBg)
      return solid ? hexToRgb(solid) : null
    })()
  if (!face) return t.colors.primary
  const primary = colorToRgb(t.colors.primary)
  return primary && wcagRatio(primary, face) >= 3 ? t.colors.primary : contrastPick(userBg)
}

/** fg token → 主题配置取值（*-foreground 按 themeService 口径黑白择优） */
const FG_TOKENS: Record<string, (t: ThemeConfig) => string> = {
  foreground: (t) => t.colors.text.primary,
  'card-foreground': (t) => t.colors.text.primary,
  'popover-foreground': (t) => t.colors.text.primary,
  'primary-foreground': (t) => contrastPick(t.colors.primary),
  'secondary-foreground': (t) => contrastPick(t.colors.secondary),
  'accent-foreground': (t) => contrastPick(t.colors.accent),
  'destructive-foreground': (t) => contrastPick(t.colors.status.error),
  primary: (t) => t.colors.primary,
  secondary: (t) => t.colors.secondary,
  accent: (t) => t.colors.accent,
  destructive: (t) => t.colors.status.error,
  'muted-foreground': (t) => t.colors.text.secondary,
  'bubble-user-text': (t) => t.colors.bubble.user_text,
  'bubble-ai-text': (t) => t.colors.bubble.ai_text,
  'bubble-link': bubbleLinkValue,
  'status-success': (t) => t.colors.status.success,
  'status-warning': (t) => t.colors.status.warning,
  'status-error': (t) => t.colors.status.error,
  'status-info': (t) => t.colors.status.info,
  'status-running': (t) => t.colors.status.running,
  'status-pending': (t) => t.colors.status.pending,
  'status-success-foreground': (t) => contrastPick(t.colors.status.success),
  'status-warning-foreground': (t) => contrastPick(t.colors.status.warning),
  'status-error-foreground': (t) => contrastPick(t.colors.status.error),
  'status-info-foreground': (t) => contrastPick(t.colors.status.info),
  'status-running-foreground': (t) => contrastPick(t.colors.status.running),
  'status-pending-foreground': (t) => contrastPick(t.colors.status.pending),
  white: () => '#ffffff',
  black: () => '#000000',
}

/** text-* 里的非颜色工具类（字号/对齐），既非颜色也不是屏障 */
const NON_COLOR_TEXT = new Set([
  'xs',
  'sm',
  'base',
  'lg',
  'xl',
  '2xl',
  '3xl',
  '4xl',
  '5xl',
  '6xl',
  'left',
  'center',
  'right',
  'justify',
  'nowrap',
  'wrap',
  'balance',
  'pretty',
  'caption',
  'label',
  'body',
  'title',
  'page-title',
])

/** 内联 style 里 var(--x) 的 slot 化映射（口径 = themeService 发射的变量名） */
const BG_VARS: Record<string, string> = {
  '--background': 'background',
  '--card': 'card',
  '--popover': 'popover',
  '--primary': 'primary',
  '--secondary': 'secondary',
  '--accent': 'accent',
  '--muted': 'muted',
  '--input': 'input',
  '--bubble-user-bg': 'bubble-user',
  '--bubble-ai-bg': 'bubble-ai',
  '--ds-bg-panel': 'card',
  '--ds-bg-elevated': 'elevated',
  '--sidebar-bg': 'sidebar',
  '--region-sidebar-bg': 'sidebar',
  '--region-chat-bg': 'background',
  '--region-workspace-bg': 'card',
  '--chat-bg-color': 'background',
  '--status-success': 'status-success',
  '--status-warning': 'status-warning',
  '--status-error': 'status-error',
  '--status-info': 'status-info',
  '--status-running': 'status-running',
  '--status-pending': 'status-pending',
}

const FG_VARS: Record<string, string> = {
  '--foreground': 'foreground',
  '--card-foreground': 'card-foreground',
  '--popover-foreground': 'popover-foreground',
  '--primary-foreground': 'primary-foreground',
  '--secondary-foreground': 'secondary-foreground',
  '--accent-foreground': 'accent-foreground',
  '--muted-foreground': 'muted-foreground',
  '--primary': 'primary',
  '--secondary': 'secondary',
  '--accent': 'accent',
  '--destructive': 'destructive',
  '--bubble-user-text': 'bubble-user-text',
  '--bubble-ai-text': 'bubble-ai-text',
  '--bubble-link': 'bubble-link',
  '--region-chat-fg': 'foreground',
  '--region-sidebar-fg': 'foreground',
  '--region-workspace-fg': 'foreground',
  '--status-success': 'status-success',
  '--status-warning': 'status-warning',
  '--status-error': 'status-error',
  '--status-info': 'status-info',
  '--status-running': 'status-running',
  '--status-pending': 'status-pending',
}

// ---------------------------------------------------------------------------
// 颜色引用模型
// ---------------------------------------------------------------------------

interface ColorRef {
  kind: 'token' | 'raw' | 'unknown' | 'none'
  slot?: 'bg' | 'fg'
  token?: string
  values?: string[]
  alpha?: number
  /** 报告/基线用的可读名 */
  name: string
}

const DEFAULT_FG: ColorRef = { kind: 'token', slot: 'fg', token: 'foreground', name: 'foreground' }
const DEFAULT_BG: ColorRef = { kind: 'token', slot: 'bg', token: 'background', name: 'background' }

/** 把一个颜色值字符串解析为 slot 化引用 */
function resolveRawValue(value: string, slot: 'bg' | 'fg'): ColorRef {
  const v = value.trim()
  if (v === '' || v === 'transparent' || v === 'none' || v === 'currentColor' || v === 'inherit') {
    return { kind: 'none', name: v || 'empty' }
  }
  // 复合函数先于 var() 判定：color-mix/gradient 的内层 var 只是成分，
  // 抽出来当整值会把 15% 透明混合当纯色（MessageActions 回退确认条实锤）。
  // color-mix 的百分比权重不可静态解析（抽取色标当纯色会过度逼近），一律屏障；
  // gradient 保留色标候选（渐变面的最差色标评估与 presets 门禁同口径）
  if (v.includes('color-mix(')) return { kind: 'unknown', name: v.slice(0, 40) }
  if (v.includes('gradient(')) {
    const stops = gradientStops(v)
    return stops.length
      ? { kind: 'raw', slot, values: stops, name: v.slice(0, 40) }
      : { kind: 'unknown', name: v.slice(0, 40) }
  }
  // var(--name) / var(--name, fallback) / hsl(var(--name)) / hsl(var(--name) / a)
  const varM = v.match(/var\((--[\w-]+)\s*(?:,\s*([^()]+))?\)/)
  if (varM) {
    const table = slot === 'bg' ? BG_VARS : FG_VARS
    const token = table[varM[1]]
    if (token) return { kind: 'token', slot, token, name: token }
    const fallback = varM[2]?.trim()
    if (fallback) return resolveRawValue(fallback, slot)
    return { kind: 'unknown', name: varM[1] }
  }
  if (/^#[0-9a-f]{6}$/i.test(v) || /^rgba?\(/i.test(v)) {
    const parsed = parseRgba(v)
    const alpha = parsed ? parsed.a : 1
    return { kind: 'raw', slot, values: [v], alpha, name: v.toLowerCase() }
  }
  return { kind: 'unknown', name: v.slice(0, 40) }
}

/** 单个 Tailwind 类 → 引用；非颜色类返回 null，变体前缀忽略 */
function classToRef(cls: string, slot: 'bg' | 'fg'): ColorRef | null {
  if (cls.includes(':')) return null
  const m = cls.match(/^(bg|text)-(.+?)(?:\/(\d{1,3}))?$/)
  if (!m || (slot === 'bg') !== (m[1] === 'bg')) return null
  const rawToken = m[2]
  const alpha = m[3] !== undefined ? Number(m[3]) / 100 : 1
  if (slot === 'fg' && NON_COLOR_TEXT.has(rawToken)) return null
  if (rawToken === 'transparent' || rawToken === 'inherit' || rawToken === 'current') {
    return { kind: 'none', name: `${slot}-${rawToken}` }
  }
  if (rawToken.startsWith('gradient-to')) return null
  const table = slot === 'bg' ? BG_TOKENS : FG_TOKENS
  if (table[rawToken]) return { kind: 'token', slot, token: rawToken, alpha, name: rawToken }
  if (rawToken.startsWith('[') && rawToken.endsWith(']')) {
    const inner = rawToken.slice(1, -1).trim()
    const ref = resolveRawValue(inner, slot)
    // alpha 修饰对 token/raw 一律生效（bg-[var(--x)]/10 的 /10 曾被整块丢弃，
    // 半透明 tint 被当纯色判出 1.00 假撞色）
    return ref.kind === 'raw' || ref.kind === 'token'
      ? { ...ref, alpha: refAlpha(ref) * alpha }
      : ref
  }
  return { kind: 'unknown', name: `${slot === 'bg' ? 'bg' : 'text'}-${rawToken}` }
}

// ---------------------------------------------------------------------------
// TSX 扫描
// ---------------------------------------------------------------------------

interface PairViolation {
  file: string
  line: number
  bg: string
  fg: string
  theme: string
  ratio: number
}

/** 收集表达式内全部字符串字面量（style 兜底面：非三元结构的字面量并集） */
function collectStringLiterals(node: ts.Expression | undefined): string[] {
  if (!node) return []
  const out: string[] = []
  function walk(n: ts.Node): void {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) {
      out.push(n.text)
      return
    }
    ts.forEachChild(n, walk)
  }
  walk(node)
  return out
}

/**
 * className 表达式 → 互斥分支集：三元/&& 的每个分支块是互斥状态，与无条件
 * 基类组合成集；集内类同时生效，集间互斥（ReviewDiff 模式切换按钮实证：
 * 叉积会造出 bg-accent × text-muted-foreground 的不可能组合）
 */
function classAltSets(node: ts.Expression | undefined): string[][] {
  if (!node) return []
  const base: string[] = []
  const branchChunks: string[] = []
  function walk(n: ts.Node, inBranch: boolean): void {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) {
      ;(inBranch ? branchChunks : base).push(n.text)
      return
    }
    if (ts.isConditionalExpression(n)) {
      walk(n.whenTrue, true)
      walk(n.whenFalse, true)
      return
    }
    if (ts.isParenthesizedExpression(n) || ts.isAsExpression(n)) {
      walk(n.expression, inBranch)
      return
    }
    if (
      ts.isBinaryExpression(n) &&
      n.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
    ) {
      walk(n.left, inBranch)
      walk(n.right, true)
      return
    }
    ts.forEachChild(n, (child) => walk(child, inBranch))
  }
  walk(node, false)
  if (branchChunks.length === 0) return base.length > 0 ? [base] : []
  return branchChunks.map((chunk) => [...base, chunk])
}

/**
 * 内联 style 值的分支序候选：三元按 true→false 展开，保持跨属性位置配对
 * （isUser ? A : B 与 isUser ? C : D → [A,B] × [C,D] 位置对应）
 */
function literalAlternatives(node: ts.Expression): string[] {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [node.text]
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) {
    return literalAlternatives(node.expression)
  }
  if (ts.isConditionalExpression(node)) {
    return [...literalAlternatives(node.whenTrue), ...literalAlternatives(node.whenFalse)]
  }
  if (
    ts.isBinaryExpression(node) &&
    node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
  ) {
    return literalAlternatives(node.right)
  }
  return collectStringLiterals(node)
}

/** 从 style 对象字面量提取 bg/fg 候选（含 spread 变量解析） */
function extractStyleObject(
  obj: ts.ObjectLiteralExpression,
  styleVars: Map<string, ts.ObjectLiteralExpression>,
): { bgAlts: ColorRef[]; fgAlts: ColorRef[] } {
  const bgAlts: ColorRef[] = []
  const fgAlts: ColorRef[] = []
  function absorb(target: ts.ObjectLiteralExpression): void {
    for (const prop of target.properties) {
      if (ts.isSpreadAssignment(prop)) {
        if (ts.isIdentifier(prop.expression)) {
          const found = styleVars.get(prop.expression.text)
          if (found) absorb(found)
        } else if (ts.isObjectLiteralExpression(prop.expression)) {
          absorb(prop.expression)
        }
        continue
      }
      if (!ts.isPropertyAssignment(prop) || !ts.isIdentifier(prop.name)) continue
      const key = prop.name.text
      if (key !== 'color' && key !== 'background' && key !== 'backgroundColor') continue
      const slot: 'bg' | 'fg' = key === 'color' ? 'fg' : 'bg'
      const refs = literalAlternatives(prop.initializer).map((v) => resolveRawValue(v, slot))
      if (slot === 'fg') fgAlts.push(...refs)
      else bgAlts.push(...refs)
    }
  }
  absorb(obj)
  return { bgAlts, fgAlts }
}

function refColorStrings(ref: ColorRef, theme: ThemeConfig): string[] | null {
  if (ref.kind === 'token' && ref.slot) {
    const table = ref.slot === 'bg' ? BG_TOKENS : FG_TOKENS
    const fn = ref.token ? table[ref.token] : undefined
    return fn ? [fn(theme)] : null
  }
  if (ref.kind === 'raw' && ref.values) return ref.values
  return null
}

function refAlpha(ref: ColorRef): number {
  return ref.alpha ?? 1
}

/**
 * 祖先 bg 栈（候选组栈）→ 有效不透明候选色：自顶向下找最近可解析组（组内候选
 * 全展开），半透明逐层压栈合成；unknown 屏障组返回 null 表示不可判定
 */
function stackBgColors(stack: ColorRef[][], theme: ThemeConfig): RgbA[] | null {
  for (let i = stack.length - 1; i >= 0; i--) {
    const candidates: RgbA[] = []
    let barrier = false
    for (const ref of stack[i]) {
      if (ref.kind === 'unknown') {
        barrier = true
        break
      }
      if (ref.kind !== 'token' && ref.kind !== 'raw') continue
      const vals = refColorStrings(ref, theme)
      if (!vals) {
        barrier = true
        break
      }
      for (const v of vals) {
        const parsed = parseRgba(v)
        if (parsed) candidates.push({ ...parsed, a: parsed.a * refAlpha(ref) })
      }
    }
    if (barrier) return null
    if (candidates.length === 0) continue
    if (candidates.every((c) => c.a >= 1)) return candidates
    const lower = stackBgColors(stack.slice(0, i), theme)
    if (!lower) return null
    const out: RgbA[] = []
    for (const c of candidates) {
      for (const base of lower) out.push(c.a >= 1 ? c : composite(c, base))
    }
    return out
  }
  // 栈底缺省 = 页面主背景（渐变取全部色标；纯色直接解析）
  const stops = gradientStops(theme.colors.background.main)
  const defaultVals = stops.length > 0 ? stops : [theme.colors.background.main]
  return defaultVals.map((v) => parseRgba(v)).filter((c): c is RgbA => c !== null)
}

/** 祖先栈上最近的有效候选组；unknown 屏障 → 哨兵；栈空 → null（用缺省） */
const BARRIER_GROUP: ColorRef[] = [{ kind: 'unknown', name: 'barrier' }]
function nearestGroup(stack: ColorRef[][]): ColorRef[] | null {
  for (let i = stack.length - 1; i >= 0; i--) {
    if (stack[i].some((r) => r.kind === 'unknown')) return BARRIER_GROUP
    const real = stack[i].filter((r) => r.kind === 'token' || r.kind === 'raw')
    if (real.length > 0) return real
  }
  return null
}

/** 表单控件自闭合仍有内容（值/占位文本），不可按装饰跳过 */
const CONTENT_BEARING_DOM = new Set(['input', 'textarea', 'select', 'option'])

/** 无内容自闭合 DOM（装饰条/spacer/分隔线）不承载文字图标，跳过配对评估 */
function isContentlessSelfClosing(node: ts.JsxSelfClosingElement): boolean {
  if (!ts.isIdentifier(node.tagName)) return false
  const name = node.tagName.text
  if (name.length > 0 && /[A-Z]/.test(name[0])) return false
  return !CONTENT_BEARING_DOM.has(name)
}

/** 一对 (fg, bg) 在一个主题下的最差对比度；不可判定返回 null */
function pairMinRatio(
  fgRef: ColorRef,
  bgRef: ColorRef,
  bgStackBelow: ColorRef[][],
  theme: ThemeConfig,
): number | null {
  // 自身 bg 候选（半透明压父底合成）
  const vals = refColorStrings(bgRef, theme)
  if (!vals) return null
  let bgCandidates = vals
    .map((v) => parseRgba(v))
    .filter((c): c is RgbA => c !== null)
    .map((c) => ({ ...c, a: c.a * refAlpha(bgRef) }))
  if (bgCandidates.length === 0) return null
  if (bgCandidates.some((c) => c.a < 1)) {
    const base = stackBgColors(bgStackBelow, theme)
    if (!base) return null
    bgCandidates = bgCandidates.flatMap((c) => (c.a >= 1 ? [c] : base.map((b) => composite(c, b))))
  }
  const fgVals = refColorStrings(fgRef, theme)
  if (!fgVals) return null
  const fgParsed = fgVals
    .map((v) => parseRgba(v))
    .filter((c): c is RgbA => c !== null)
    .map((c) => ({ ...c, a: c.a * refAlpha(fgRef) }))
  if (fgParsed.length === 0) return null
  let min = Infinity
  for (const fg of fgParsed) {
    for (const bg of bgCandidates) {
      min = Math.min(min, wcagRatio(fg.a >= 1 ? fg : composite(fg, bg), bg))
    }
  }
  return Number.isFinite(min) ? min : null
}

/** 扫描单份 TSX 源码，产出违例（file 由调用方传入，相对 src/） */
export function scanTsxSource(
  file: string,
  source: string,
  themes: Record<string, ThemeConfig>,
): PairViolation[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  // 预收集：style={变量} 引用目标（同文件 const 变量 = {...} 初始化器）
  const styleVars = new Map<string, ts.ObjectLiteralExpression>()
  function collectStyleVars(n: ts.Node): void {
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && n.initializer) {
      const init = ts.isParenthesizedExpression(n.initializer)
        ? n.initializer.expression
        : n.initializer
      if (ts.isObjectLiteralExpression(init)) styleVars.set(n.name.text, init)
    }
    ts.forEachChild(n, collectStyleVars)
  }
  collectStyleVars(sf)

  const violations: PairViolation[] = []
  // 祖先栈：每个元素压一个「候选组」——同一属性的条件分支（isUser ? A : B）是
  // 一组互斥候选，继承方评估时全组展开（任一分支成立即为真实状态）
  const bgStack: ColorRef[][] = []
  const fgStack: ColorRef[][] = []
  const themeEntries = Object.entries(themes)

  function lineOf(node: ts.Node): number {
    return sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
  }

  /** 元素内 (bg, fg) 配对：style 三元候选数相等时按位置 zip（同条件分支），
   * 否则叉积；重复对按名去重 */
  function pairRefs(bgGroup: ColorRef[], fgGroup: ColorRef[]): Array<[ColorRef, ColorRef]> {
    const out: Array<[ColorRef, ColorRef]> = []
    const push = (bgRef: ColorRef, fgRef: ColorRef) => {
      if (!out.some(([b, f]) => b.name === bgRef.name && f.name === fgRef.name)) {
        out.push([bgRef, fgRef])
      }
    }
    const realBg = bgGroup.filter((r) => r.kind === 'token' || r.kind === 'raw')
    const realFg = fgGroup.filter((r) => r.kind === 'token' || r.kind === 'raw')
    if (
      realBg.length > 1 &&
      realFg.length > 1 &&
      realBg.length === realFg.length &&
      realBg.every((r) => r.kind === 'token' && r.slot === 'bg') &&
      realFg.every((r) => r.kind === 'token' && r.slot === 'fg')
    ) {
      // 内联 style 三元位置配对：避免 (user-bg × ai-text) 类不可能组合误报
      for (let i = 0; i < realBg.length; i++) push(realBg[i], realFg[i])
    } else {
      for (const bgRef of realBg) for (const fgRef of realFg) push(bgRef, fgRef)
    }
    return out
  }

  function evalElement(
    line: number,
    ownBgGroup: ColorRef[],
    ownFgGroup: ColorRef[],
    inheritedFg: ColorRef[] | null,
    inheritedBg: ColorRef[] | null,
  ): void {
    const evalPair = (bgRef: ColorRef, fgRef: ColorRef) => {
      for (const [themeId, theme] of themeEntries) {
        const ratio = pairMinRatio(fgRef, bgRef, bgStack, theme)
        if (ratio !== null && ratio < VISIBLE_RATIO) {
          violations.push({ file, line, bg: bgRef.name, fg: fgRef.name, theme: themeId, ratio })
        }
      }
    }
    if (ownBgGroup.length > 0) {
      const fgGroup =
        ownFgGroup.length > 0
          ? ownFgGroup
          : inheritedFg === BARRIER_GROUP
            ? []
            : (inheritedFg ?? [DEFAULT_FG])
      for (const bgRef of ownBgGroup) {
        if (bgRef.kind !== 'token' && bgRef.kind !== 'raw') continue
        for (const fgRef of fgGroup) evalPair(bgRef, fgRef)
      }
    } else if (ownFgGroup.length > 0) {
      if (inheritedBg === BARRIER_GROUP) return
      for (const bgRef of inheritedBg ?? [DEFAULT_BG]) {
        for (const fgRef of ownFgGroup) evalPair(bgRef, fgRef)
      }
    }
  }

  function visit(node: ts.Node): void {
    if (ts.isJsxSelfClosingElement(node) && isContentlessSelfClosing(node)) return
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      const attrs: ts.JsxAttributes = ts.isJsxElement(node)
        ? node.openingElement.attributes
        : node.attributes
      const styleBg: ColorRef[] = []
      const styleFg: ColorRef[] = []
      const classSets: Array<{ bg: ColorRef[]; fg: ColorRef[] }> = []
      for (const prop of attrs.properties) {
        if (!ts.isJsxAttribute(prop) || !prop.initializer) continue
        if (prop.name.text === 'className') {
          for (const set of classAltSets(prop.initializer as ts.Expression)) {
            const setBg: ColorRef[] = []
            const setFg: ColorRef[] = []
            for (const literal of set) {
              for (const cls of literal.split(/\s+/)) {
                const bgRef = classToRef(cls, 'bg')
                if (bgRef) setBg.push(bgRef)
                const fgRef = classToRef(cls, 'fg')
                if (fgRef) setFg.push(fgRef)
              }
            }
            classSets.push({ bg: setBg, fg: setFg })
          }
        } else if (prop.name.text === 'style') {
          const init = prop.initializer as ts.Expression
          const objs: ts.ObjectLiteralExpression[] = []
          if (ts.isObjectLiteralExpression(init)) objs.push(init)
          else {
            // 三元/标识符等：收集其中全部对象字面量与可解析标识符
            function gather(n: ts.Node): void {
              if (ts.isObjectLiteralExpression(n)) {
                objs.push(n)
                return
              }
              if (ts.isIdentifier(n) && styleVars.has(n.text)) {
                objs.push(styleVars.get(n.text)!)
                return
              }
              ts.forEachChild(n, gather)
            }
            gather(init)
          }
          for (const obj of objs) {
            const extracted = extractStyleObject(obj, styleVars)
            styleBg.push(...extracted.bgAlts)
            styleFg.push(...extracted.fgAlts)
          }
        }
      }
      const inhFg = nearestGroup(fgStack)
      const inhBg = nearestGroup(bgStack)
      const inhFgCands = inhFg === BARRIER_GROUP ? [] : (inhFg ?? [DEFAULT_FG])
      const inhBgCands = inhBg === BARRIER_GROUP ? [] : (inhBg ?? [DEFAULT_BG])
      const line = lineOf(node)
      const seenPairs = new Set<string>()
      const evalOne = (bgRef: ColorRef, fgRef: ColorRef) => {
        const key = `${bgRef.name}|${fgRef.name}`
        if (seenPairs.has(key)) return
        seenPairs.add(key)
        evalElement(line, [bgRef], [fgRef], inhFg, inhBg)
      }
      // style 内部配对（三元候选按位置 zip），单边时对继承底/字色
      if (styleBg.length > 0 && styleFg.length > 0) {
        for (const [bgRef, fgRef] of pairRefs(styleBg, styleFg)) evalOne(bgRef, fgRef)
      } else if (styleBg.length > 0) {
        for (const bgRef of styleBg) for (const fgRef of inhFgCands) evalOne(bgRef, fgRef)
      } else if (styleFg.length > 0) {
        for (const fgRef of styleFg) for (const bgRef of inhBgCands) evalOne(bgRef, fgRef)
      }
      // class 互斥分支集 × style 常驻候选：集内同时生效，集间互斥；
      // 双方皆空色集（纯布局类）跳过
      for (const set of classSets) {
        if (set.bg.length === 0 && set.fg.length === 0) continue
        const bgCands = [...styleBg, ...set.bg]
        const fgCands = [...styleFg, ...set.fg]
        if (bgCands.length > 0 && fgCands.length > 0) {
          for (const [bgRef, fgRef] of pairRefs(bgCands, fgCands)) evalOne(bgRef, fgRef)
        } else if (bgCands.length > 0) {
          for (const bgRef of bgCands) for (const fgRef of inhFgCands) evalOne(bgRef, fgRef)
        } else {
          for (const fgRef of fgCands) for (const bgRef of inhBgCands) evalOne(bgRef, fgRef)
        }
      }
      // 压栈供子树继承：条件候选并为一个组
      const unionBg = [...styleBg, ...classSets.flatMap((s) => s.bg)]
      const unionFg = [...styleFg, ...classSets.flatMap((s) => s.fg)]
      if (unionBg.length > 0) bgStack.push(unionBg.filter((r) => r.kind !== 'none'))
      if (unionFg.length > 0) fgStack.push(unionFg.filter((r) => r.kind !== 'none'))
      ts.forEachChild(node, visit)
      if (unionBg.length > 0) bgStack.pop()
      if (unionFg.length > 0) fgStack.pop()
      return
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return violations
}

// ---------------------------------------------------------------------------
// 门禁执行
// ---------------------------------------------------------------------------

function listTsxFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === '__tests__' || entry.name === 'node_modules') continue
      out.push(...listTsxFiles(full))
    } else if (entry.name.endsWith('.tsx') && !/\.(test|stories)\.tsx$/.test(entry.name)) {
      out.push(full)
    }
  }
  return out
}

function baselineKeys(): Set<string> {
  if (!existsSync(BASELINE_FILE)) return new Set()
  return new Set(
    readFileSync(BASELINE_FILE, 'utf-8')
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => l !== '' && !l.startsWith('#')),
  )
}

function violationKey(v: { file: string; bg: string; fg: string }): string {
  return `${v.file}|bg=${v.bg}|fg=${v.fg}`
}

export function collectViolations(): PairViolation[] {
  const all: PairViolation[] = []
  for (const full of listTsxFiles(SRC_DIR)) {
    const rel = full.slice(SRC_DIR.length).replace(/\\/g, '/')
    all.push(...scanTsxSource(rel, readFileSync(full, 'utf-8'), presetThemes))
  }
  return all
}

describe('组件级颜色对门禁（TSX 层级 × 全主题对比度）', () => {
  it('检测器自检：已知 bug 模式必被捕获（编辑框白字白底 / 附件行半透明底继承气泡字色）', () => {
    const buggy = `const A = () => (
      <div style={{ background: 'var(--bubble-user-bg)', color: 'var(--bubble-user-text)' }}>
        <textarea className="bg-background border-input p-3 text-sm" />
        <button className="bg-background/60 hover:bg-background px-2 text-sm">附件.pdf</button>
      </div>
    )`
    const found = scanTsxSource('selftest.tsx', buggy, presetThemes)
    const keys = new Set(found.map((v) => `${v.bg}|${v.fg}`))
    expect(keys.has('background|bubble-user-text'), JSON.stringify(found)).toBe(true)
    // 修复后形态：显式 text-foreground 配对 → 不再有 background×bubble-user-text 违例
    const fixed = buggy
      .replace('bg-background border-input', 'bg-background text-foreground border-input')
      .replace(
        'bg-background/60 hover:bg-background px-2',
        'bg-background/60 hover:bg-background text-foreground px-2',
      )
    const foundFixed = scanTsxSource('selftest.tsx', fixed, presetThemes)
    expect(
      foundFixed.filter((v) => v.fg === 'bubble-user-text'),
      JSON.stringify(foundFixed),
    ).toEqual([])
  })

  it('全仓 TSX 颜色对与基线精确相等（新增违例红 / 基线残留也红）', () => {
    const violations = collectViolations()
    const current = new Set(violations.map(violationKey))
    const baseline = baselineKeys()
    const newlyAdded = [...current].filter((k) => !baseline.has(k)).sort()
    const stale = [...baseline].filter((k) => !current.has(k)).sort()
    const detail = violations
      .filter((v) => !baseline.has(violationKey(v)))
      .map((v) => `${v.file}:${v.line} [${v.theme}] ${v.bg} × ${v.fg} = ${v.ratio.toFixed(2)}`)
      .sort()
    expect(
      { newlyAdded, stale },
      [
        newlyAdded.length > 0
          ? `新增低对比配对（禁止新增，须修复或经裁定入基线）：\n${detail.join('\n')}`
          : '',
        stale.length > 0 ? `基线残留（已清偿须删行，棘轮只减不增）：\n${stale.join('\n')}` : '',
      ]
        .filter(Boolean)
        .join('\n\n'),
    ).toEqual({ newlyAdded: [], stale: [] })
  })
})
