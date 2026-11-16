/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * 实际渲染背景对比度统一强制（backdropContrast v2：全主题 × 全 UI）测试
 *
 * 覆盖四层：
 * 1. 纯函数管线：颜色/渐变/color-mix 解析、像素合成、包络、步进强制、scrim
 *    自适应、指纹、HSL 三元组发射格式；
 * 2. 全量令牌：根级 fg/muted + 三区域（侧栏/聊天/工作区）+ 语义表面对 +
 *    双气泡 + 皮肤 ds 文字；
 * 3. 编排入口：无图/图片主题端到端发射、跨会话记忆、失败 fail-open、防串写；
 * 4. 四类主题同链 + store 接线。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyRenderedBackdropContrast,
  backdropEnvelope,
  buildBodyPixels,
  buildRegionPixels,
  compositeOver,
  computeAllTokens,
  emitBackdropTokens,
  enforceStepwise,
  fillSolid,
  fingerprintOf,
  gatherBackdropContext,
  hslTriple,
  loadImagePixels,
  parseColorMix,
  parseColorValue,
  parseGradientStops,
  rasterizeColorValue,
  resolveBubbleFace,
  solveOverlayAlpha,
  wcagRatio,
  type BackdropTokens,
  type RenderedBackdropContext,
  type Rgb,
} from '../backdropContrast'

// ── 断言工具 ──

const WHITE: Rgb = { r: 255, g: 255, b: 255 }
const BLACK: Rgb = { r: 0, g: 0, b: 0 }

function hexRgb(color: string): Rgb {
  const c = parseColorValue(color)
  expect(c, `解析失败: ${color}`).not.toBeNull()
  return { r: c!.r, g: c!.g, b: c!.b }
}

/** 可读性性质断言：前景对给定底色 ≥4.5（浮点容差；三元组/hex 均可解析） */
function expectReadable(fgColor: string, bgs: Rgb[]): void {
  const fg = hexRgb(fgColor)
  for (const bg of bgs) {
    expect(
      wcagRatio(fg, bg),
      `前景 ${fgColor} 对底色 rgb(${bg.r},${bg.g},${bg.b}) 不足 4.5`,
    ).toBeGreaterThanOrEqual(4.5 - 1e-9)
  }
}

/** 半黑半白 HDR 测试图（w×h RGBA，默认与栅格缓冲同尺寸=32×128） */
function hdrPixels(w = 32, h = 128): Uint8ClampedArray {
  const buf = new Uint8ClampedArray(w * h * 4)
  for (let y = 0; y < h; y++) {
    const v = y < h / 2 ? 0 : 255
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4
      buf[i] = v
      buf[i + 1] = v
      buf[i + 2] = v
      buf[i + 3] = 255
    }
  }
  return buf
}

function baseCtx(overrides: Partial<RenderedBackdropContext> = {}): RenderedBackdropContext {
  return {
    mainBg: '#111318',
    imageUrl: null,
    overlay: null,
    overlayOpacity: 1,
    chatLayers: [null, null, null],
    sidebarLayers: [null, null, null, null],
    workspaceLayers: [null, null, null],
    textSeed: '#e6e6e6',
    aiBubbleFace: null,
    userBubbleFace: null,
    userBubbleText: null,
    cardFallback: null,
    flatBubble: false,
    surfacePairs: [],
    dsTextTokens: { primary: null, secondary: null, muted: null },
    ...overrides,
  }
}

// ── 1. 纯函数管线 ──

describe('颜色解析', () => {
  it.each([
    ['#ff0000', { r: 255, g: 0, b: 0, a: 1 }],
    ['#0a1226', { r: 10, g: 18, b: 38, a: 1 }],
    ['rgb(1, 2, 3)', { r: 1, g: 2, b: 3, a: 1 }],
    ['rgba(1, 2, 3, 0.5)', { r: 1, g: 2, b: 3, a: 0.5 }],
    ['transparent', { r: 0, g: 0, b: 0, a: 0 }],
    // shadcn 三元组（themeService/桥发射形态；222 47% 11% = slate-900 #0f172a）
    ['210 40% 98%', { r: 248, g: 250, b: 252, a: 1 }],
    ['222 47% 11%', { r: 15, g: 23, b: 41, a: 1 }],
    ['hsl(210 40% 98%)', { r: 248, g: 250, b: 252, a: 1 }],
    ['hsl(210, 40%, 98%)', { r: 248, g: 250, b: 252, a: 1 }],
    ['hsl(0 100% 50% / 0.5)', { r: 255, g: 0, b: 0, a: 0.5 }],
  ])('parseColorValue(%s) → %j', (input, expected) => {
    expect(parseColorValue(input as string)).toEqual(expected)
  })

  it('无法解析的形态返回 null（fail-open 判据）', () => {
    expect(parseColorValue('var(--x)')).toBeNull()
    expect(parseColorValue('')).toBeNull()
  })

  it('parseColorMix：预乘 alpha（与透明混合留通道只降 alpha）；第二色任意', () => {
    expect(parseColorMix('color-mix(in srgb, #0A1226 80%, transparent)')).toEqual({
      r: 10,
      g: 18,
      b: 38,
      a: 0.8,
    })
    // --ds-bg-elevated 真实形态：82% #111318 + 18% 白
    expect(parseColorMix('color-mix(in srgb, #111318 82%, #ffffff)')).toEqual({
      r: 60,
      g: 61,
      b: 66,
      a: 1,
    })
    expect(parseColorMix('color-mix(in oklab, red, blue)')).toBeNull()
  })

  it('hslTriple：三元组发射格式与解析互逆（±1 量化容差）', () => {
    for (const rgb of [WHITE, BLACK, { r: 15, g: 23, b: 41 }, { r: 248, g: 250, b: 252 }]) {
      const back = parseColorValue(hslTriple(rgb))!
      expect(Math.abs(back.r - rgb.r)).toBeLessThanOrEqual(2)
      expect(Math.abs(back.g - rgb.g)).toBeLessThanOrEqual(2)
      expect(Math.abs(back.b - rgb.b)).toBeLessThanOrEqual(2)
    }
  })
})

describe('渐变解析', () => {
  it('authored 线性渐变：角度跳过、位次解析、色标保序', () => {
    const stops = parseGradientStops(
      'linear-gradient(135deg, #04060F 0%, #0A1226 55%, #020617 100%)',
    )
    expect(stops).toHaveLength(3)
    expect(stops!.map((s) => s.pos)).toEqual([0, 0.55, 1])
    expect(stops![0].color).toEqual({ r: 4, g: 6, b: 15, a: 1 })
  })

  it('rgba 色标与缺省位次：尾停显式、中间停均分', () => {
    const stops = parseGradientStops(
      'linear-gradient(to bottom, rgba(0,0,0,0.5), #ffffff 50%, #000000)',
    )
    expect(stops!.map((s) => s.pos)).toEqual([0, 0.5, 1])
    expect(stops![0].color).toEqual({ r: 0, g: 0, b: 0, a: 0.5 })

    const even = parseGradientStops('linear-gradient(#ffffff, #888888, #000000)')
    expect(even!.map((s) => s.pos)).toEqual([0, 0.5, 1])
  })

  it('radial-gradient 等不支持形态返回 null（fail-open）', () => {
    expect(parseGradientStops('radial-gradient(#fff, #000)')).toBeNull()
    expect(parseGradientStops('#808080')).toBeNull()
  })

  it('末停位次 <100%：末色延展到末端（t 超界取末停色，非外推）', () => {
    // 底部 ~10% 行（t>0.9）应为纯黑——黑区占 10%>5%，p5 暗端必落纯黑
    const { low } = backdropEnvelope(
      rasterizeColorValue('linear-gradient(#ffffff 0%, #000000 90%)')!,
    )
    expect(low).toEqual(BLACK)
  })
})

describe('栅格化与包络', () => {
  it('纯色缓冲：包络两端相等', () => {
    const { low, high } = backdropEnvelope(rasterizeColorValue('#808080')!)
    expect(low).toEqual({ r: 128, g: 128, b: 128 })
    expect(high).toEqual({ r: 128, g: 128, b: 128 })
  })

  it('白→黑垂直渐变：包络 low 暗端、high 亮端各贴两端', () => {
    const { low, high } = backdropEnvelope(
      rasterizeColorValue('linear-gradient(#ffffff 0%, #000000 100%)')!,
    )
    expect(low.r).toBeLessThan(30)
    expect(high.r).toBeGreaterThan(225)
  })

  it('半黑半白图：包络两端即黑白（p5/p95 不取杂散中间值）', () => {
    const { low, high } = backdropEnvelope(hdrPixels())
    expect(low).toEqual(BLACK)
    expect(high).toEqual(WHITE)
  })

  it('compositeOver 手算：50% 红压蓝（alpha 8bit 量化 128/255）', () => {
    const bottom = fillSolid({ r: 0, g: 0, b: 255, a: 1 }, 16)
    const out = compositeOver(bottom, fillSolid({ r: 255, g: 0, b: 0, a: 0.5 }, 16))
    expect(out[0]).toBe(128)
    expect(out[1]).toBe(0)
    expect(out[2]).toBe(127)
    expect(out[3]).toBe(255)
  })

  it('compositeOver 异长缓冲返回 bottom（防御，不产生 NaN）', () => {
    const bottom = fillSolid({ r: 1, g: 2, b: 3, a: 1 }, 16)
    expect(compositeOver(bottom, fillSolid(WHITE, 8))).toEqual(bottom)
  })
})

describe('步进强制（enforceStepwise）', () => {
  const solidBgs = ['#000000', '#ffffff', '#808080', '#ff0000', '#22d3ee', '#111318', '#fff1e8']

  it('性质：任意实底上结果对全部背景 ≥4.5（实底必有解）', () => {
    for (const hex of solidBgs) {
      const { color, ratio } = enforceStepwise(hexRgb('#e6e6e6'), [hexRgb(hex)])
      expect(ratio).toBeGreaterThanOrEqual(4.5 - 1e-9)
      expect(wcagRatio(color, hexRgb(hex))).toBeGreaterThanOrEqual(4.5 - 1e-9)
    }
  })

  it('双背景列表取最差端强制（包络口径；均衡灰锚点补中间调种子）', () => {
    const { color, ratio } = enforceStepwise(hexRgb('#888888'), [BLACK, WHITE])
    expect(ratio).toBeGreaterThanOrEqual(4.5 - 1e-9)
    expect(wcagRatio(color, BLACK)).toBeGreaterThanOrEqual(4.5 - 1e-9)
    expect(wcagRatio(color, WHITE)).toBeGreaterThanOrEqual(4.5 - 1e-9)
  })

  it('种子已达标原样保留（不动主题作者意图）', () => {
    expect(enforceStepwise(WHITE, [hexRgb('#111318')]).color).toEqual(WHITE)
  })

  it('删值实验：minRatio 提到 7 时中间调必走端点兜底（4.5 档不做兜底）', () => {
    const strict = enforceStepwise(hexRgb('#e6e6e6'), [hexRgb('#808080')], 7)
    expect(strict.color).toEqual(BLACK)
    expect(strict.ratio).toBeLessThan(7)
    const normal = enforceStepwise(hexRgb('#e6e6e6'), [hexRgb('#808080')], 4.5)
    expect(normal.ratio).toBeGreaterThanOrEqual(4.5 - 1e-9)
  })
})

describe('scrim 自适应（solveOverlayAlpha）', () => {
  it('HDR 图 + 薄遮罩：加强到最小必要 alpha，解后黑白端点可达 4.5', () => {
    const raw = hdrPixels()
    const overlay = { r: 0, g: 0, b: 0, a: 0.1 }
    const alpha = solveOverlayAlpha(raw, overlay, 0.1)
    expect(alpha).not.toBeNull()
    expect(alpha!).toBeGreaterThan(0.1)
    expect(alpha!).toBeLessThanOrEqual(0.75)
    const mixed = compositeOver(raw, fillSolid({ ...overlay, a: alpha! }, raw.length))
    const { low, high } = backdropEnvelope(mixed)
    expect(
      Math.max(wcagRatio(low, WHITE), wcagRatio(low, BLACK), wcagRatio(high, WHITE), wcagRatio(high, BLACK)),
    ).toBeGreaterThanOrEqual(4.5 - 1e-9)
  })

  it('单端已可解（暗图暗遮罩白字可达）→ 不加强', () => {
    const raw = fillSolid({ r: 17, g: 19, b: 24, a: 1 }, hdrPixels().length)
    expect(solveOverlayAlpha(raw, { r: 0, g: 0, b: 0, a: 0.1 }, 0.1)).toBeNull()
  })

  it('删值实验：中间调图薄遮罩 minRatio=4.5 不加强，收紧到 7 触发加强', () => {
    const raw = fillSolid({ r: 128, g: 128, b: 128, a: 1 }, hdrPixels().length)
    expect(solveOverlayAlpha(raw, { r: 0, g: 0, b: 0, a: 0.1 }, 0.1, 4.5)).toBeNull()
    const strict = solveOverlayAlpha(raw, { r: 0, g: 0, b: 0, a: 0.1 }, 0.1, 7)
    expect(strict).not.toBeNull()
    expect(strict!).toBeGreaterThan(0.1)
  })
})

describe('气泡面解析（resolveBubbleFace）', () => {
  it('声明玻璃面优先，实色面其次', () => {
    expect(resolveBubbleFace('color-mix(in srgb, #0A1226 80%, transparent)', null, false)?.a).toBe(0.8)
    expect(resolveBubbleFace('#d9738f', null, false)).toEqual({ r: 217, g: 115, b: 143, a: 1 })
  })

  it('flat 平铺缺省面 = 卡面 80% 玻璃；非 flat 无声明 → null', () => {
    expect(resolveBubbleFace(null, '222 47% 11%', true)).toEqual({ r: 15, g: 23, b: 41, a: 0.8 })
    expect(resolveBubbleFace(null, '222 47% 11%', false)).toBeNull()
  })
})

describe('指纹（fingerprintOf）', () => {
  it('同上下文同指纹；任一字段变异 → 指纹变化', () => {
    const ctx = baseCtx()
    const fp = fingerprintOf(ctx)
    expect(fingerprintOf(baseCtx())).toBe(fp)
    const mutations: Array<Partial<RenderedBackdropContext>> = [
      { mainBg: '#000000' },
      { imageUrl: 'x.png' },
      { overlay: 'rgba(0,0,0,0.5)' },
      { overlayOpacity: 0.5 },
      { chatLayers: ['#fff', null, null] },
      { sidebarLayers: [null, '#fff', null, null] },
      { workspaceLayers: [null, null, '#fff'] },
      { textSeed: '#000000' },
      { aiBubbleFace: '#fff' },
      { userBubbleFace: '#fff' },
      { userBubbleText: '#000' },
      { cardFallback: '0 0% 100%' },
      { flatBubble: true },
      { surfacePairs: [{ token: '--card-foreground', face: '#fff', fg: '#fff' }] },
      { dsTextTokens: { primary: '#fff', secondary: null, muted: null } },
    ]
    for (const patch of mutations) {
      expect(fingerprintOf(baseCtx(patch)), `${JSON.stringify(patch)} 未反映到指纹`).not.toBe(fp)
    }
  })
})

describe('背景栈构建（buildBodyPixels / buildRegionPixels）', () => {
  it('无图：纯色主背景即整幅 body 底色', () => {
    const pixels = buildBodyPixels(baseCtx(), null)
    const { low, high } = backdropEnvelope(pixels!)
    expect(low).toEqual(hexRgb('#111318'))
    expect(high).toEqual(hexRgb('#111318'))
  })

  it('有图：白图 + 50% 黑遮罩 → 全幅中灰（alpha 8bit 量化后 255×0.498≈127）', () => {
    const pixels = buildBodyPixels(
      baseCtx({ imageUrl: 'x.webp', overlay: 'rgba(0,0,0,0.5)' }),
      fillSolid(WHITE, 128 * 128 * 4),
    )
    const { low, high } = backdropEnvelope(pixels!)
    expect(low.r).toBe(127)
    expect(high.r).toBe(127)
  })

  it('区域链：半透明区域面压在 body 底色上（玻璃区域面合成）', () => {
    const body = buildBodyPixels(baseCtx(), null)!
    const pixels = buildRegionPixels(body, ['rgba(255, 255, 255, 0.5)'])
    const { low } = backdropEnvelope(pixels)
    // 0.5 白 + 0.5 #111318 → 136
    expect(low.r).toBe(136)
  })

  it('带图主题：半透明区域面压在图片上真合成（缓冲等长，不静默丢弃）', () => {
    const image = hdrPixels()
    const body = buildBodyPixels(
      baseCtx({ imageUrl: 'a.webp', overlay: 'rgba(0, 0, 0, 0)' }),
      image,
    )!
    const pixels = buildRegionPixels(body, ['rgba(255, 255, 255, 0.5)'])
    const { low, high } = backdropEnvelope(pixels)
    // 白面 50% 压图：黑半→128、白半→255；若面被丢弃（等长防御触发）则是 0/255
    expect(low.r).toBe(128)
    expect(high.r).toBe(255)
  })

  it('区域链：--ds-bg-elevated 的 color-mix（第二色实色）可栅格化', () => {
    const body = buildBodyPixels(baseCtx(), null)!
    const pixels = buildRegionPixels(body, [
      'color-mix(in srgb, #111318 82%, #ffffff)',
    ])
    const { low } = backdropEnvelope(pixels)
    expect(low.r).toBe(60)
  })
})

describe('全量令牌（computeAllTokens）', () => {
  it('根级 + 三区域：无图纯色主题全令牌齐发且各自可读', () => {
    const ctx = baseCtx({
      mainBg: '#111318',
      textSeed: '#e6e6e6',
      sidebarLayers: [null, null, null, '#111318'],
      workspaceLayers: [null, '#111318', '#111318'],
    })
    const tokens = computeAllTokens(ctx, buildBodyPixels(ctx, null)!, 'fp')
    const bg = hexRgb('#111318')
    // 根级：三元组发射格式（hsl(var()) 包装链），可读性对 body 包络
    expect(tokens.foreground.split(' ')).toHaveLength(3)
    expect(tokens.mutedForeground.split(' ')).toHaveLength(3)
    expectReadable(tokens.foreground, [bg])
    expectReadable(tokens.mutedForeground, [bg])
    // 三区域：fg hex、mutedFg 三元组，各对区域底色可读
    for (const region of [tokens.region.sidebar, tokens.region.chat, tokens.region.workspace]) {
      expect(region.fg).toMatch(/^#[0-9a-f]{6}$/)
      expect(region.mutedFg.split(' ')).toHaveLength(3)
      expectReadable(region.fg, [bg])
      expectReadable(region.mutedFg, [bg])
    }
  })

  it('亮色主题族：根级与区域同样 ≥4.5', () => {
    const ctx = baseCtx({ mainBg: '#fff9f5', textSeed: '#5d4a41' })
    const tokens = computeAllTokens(ctx, buildBodyPixels(ctx, null)!, 'fp')
    const bg = hexRgb('#fff9f5')
    expectReadable(tokens.foreground, [bg])
    expectReadable(tokens.mutedForeground, [bg])
    expectReadable(tokens.region.chat.fg, [bg])
  })

  it('语义表面对：白面白字被强制可读；半透明面压 body 底色后强制', () => {
    const ctx = baseCtx({
      mainBg: '#111318',
      surfacePairs: [
        { token: '--card-foreground', face: '#ffffff', fg: '#ffffff' },
        { token: '--popover-foreground', face: 'rgba(255, 255, 255, 0.5)', fg: '#ffffff' },
      ],
    })
    const tokens = computeAllTokens(ctx, buildBodyPixels(ctx, null)!, 'fp')
    expectReadable(tokens.surfaceOverrides['--card-foreground'], [WHITE])
    // 玻璃卡真实观感 = 50% 白 + 50% body 底色
    expectReadable(tokens.surfaceOverrides['--popover-foreground'], [
      { r: 136, g: 136, b: 137 },
    ])
    // 未声明的对不出现
    expect(tokens.surfaceOverrides['--primary-foreground']).toBeUndefined()
  })

  it('皮肤 --ds-text-*：对 body 包络强制（hex 发射）', () => {
    const ctx = baseCtx({
      mainBg: '#111318',
      dsTextTokens: { primary: '#94A3B8', secondary: '#CBD5E1', muted: null },
    })
    const tokens = computeAllTokens(ctx, buildBodyPixels(ctx, null)!, 'fp')
    expect(tokens.dsTextOverrides['--ds-text-primary']).toMatch(/^#[0-9a-f]{6}$/)
    expectReadable(tokens.dsTextOverrides['--ds-text-primary'], [hexRgb('#111318')])
    expectReadable(tokens.dsTextOverrides['--ds-text-secondary'], [hexRgb('#111318')])
    expect(tokens.dsTextOverrides['--ds-text-muted']).toBeUndefined()
  })

  it('双气泡：玻璃 AI 面与半透明用户面配对可读；不透明面保留声明成对值', () => {
    const ctx = baseCtx({
      mainBg: '#000000',
      aiBubbleFace: 'color-mix(in srgb, #0A1226 80%, transparent)',
      userBubbleFace: 'rgba(217, 115, 143, 0.85)',
      userBubbleText: '#F8F3F5',
    })
    const tokens = computeAllTokens(ctx, buildBodyPixels(ctx, null)!, 'fp')
    const aiFace = { r: 10, g: 18, b: 38, a: 0.8 }
    expectReadable(tokens.bubbleAiText!, [
      { r: 8, g: 14, b: 30 },
      { r: Math.round(aiFace.r * 0.8 + 51), g: Math.round(aiFace.g * 0.8 + 51), b: Math.round(aiFace.b * 0.8 + 51) },
    ])
    const userFace = { r: 217, g: 115, b: 143, a: 0.85 }
    expectReadable(tokens.bubbleUserText!, [
      blend(userFace, BLACK),
      blend(userFace, WHITE),
    ])
    // 不透明用户面：跳过（声明成对值保留）
    const opaque = computeAllTokens(
      baseCtx({ userBubbleFace: '#d9738f' }),
      buildBodyPixels(baseCtx(), null)!,
      'fp',
    )
    expect(opaque.bubbleUserText).toBeNull()
  })

  it('HDR 图片主题（无 scrim 加强路径）：令牌对整幅包络可读', () => {
    const ctx = baseCtx({ mainBg: '#000000', textSeed: '#e6e6e6' })
    const tokens = computeAllTokens(ctx, hdrPixels(), 'fp')
    expectReadable(tokens.foreground, [BLACK, WHITE])
    expectReadable(tokens.region.chat.fg, [BLACK, WHITE])
  })
})

function blend(face: { r: number; g: number; b: number; a: number }, bg: Rgb): Rgb {
  const a = face.a
  return {
    r: Math.round(face.r * a + bg.r * (1 - a)),
    g: Math.round(face.g * a + bg.g * (1 - a)),
    b: Math.round(face.b * a + bg.b * (1 - a)),
  }
}

// ── 2. 编排入口（DOM）──

/** 图片解码桩：FakeImage + canvas 2d stub，解码计数可断言"同主题只算一次" */
function stubImageDecoding(pixelSource: () => Uint8ClampedArray): {
  decodeCount: () => number
  lastImage: () => { onload: () => void; onerror: () => void; src: string } | undefined
} {
  const images: Array<{ onload: () => void; onerror: () => void; src: string }> = []
  let count = 0
  class FakeImage {
    onload: () => void = () => {}
    onerror: () => void = () => {}
    src = ''
    constructor() {
      images.push(this)
    }
  }
  vi.stubGlobal('Image', FakeImage)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (
    this: HTMLCanvasElement,
  ) {
    return {
      drawImage: () => {},
      getImageData: () => {
        count++
        return { data: pixelSource() } as ImageData
      },
    } as unknown as CanvasRenderingContext2D
  })
  return {
    decodeCount: () => count,
    lastImage: () =>
      images.at(-1) as { onload: () => void; onerror: () => void; src: string } | undefined,
  }
}

function setInlineVars(vars: Record<string, string>): void {
  for (const [k, v] of Object.entries(vars)) {
    document.documentElement.style.setProperty(k, v)
  }
}

const STORAGE_PREFIX = 'backdrop-contrast:'

describe('编排入口 applyRenderedBackdropContrast', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('style')
    document.body.removeAttribute('style')
  })

  it('四类主题同链：预设无图 / 插件主题 / DSH 皮肤声明同一方法同一数据形状', async () => {
    const presetSolid = baseCtx({ mainBg: '#04060f', textSeed: '220 25% 95%' })
    // 插件主题：渐变主背景 + 语义表面对 + 玻璃气泡面
    const pluginGradient = baseCtx({
      mainBg: 'linear-gradient(160deg, #fefce8 0%, #ecfeff 100%)',
      textSeed: '222 47% 11%',
      aiBubbleFace: 'color-mix(in srgb, #ffffff 80%, transparent)',
      surfacePairs: [{ token: '--card-foreground', face: '#f8fafc', fg: '#f8fafc' }],
    })
    // DSH 皮肤声明：区域面 + 卡面三元组 + flat 玻璃缺省 + ds 文字
    const dshSkin = baseCtx({
      mainBg: '#eef5ff',
      textSeed: '#1f2937',
      chatLayers: ['transparent', null, null],
      sidebarLayers: [null, 'color-mix(in srgb, #eef5ff 82%, #ffffff)', null, '210 40% 98%'],
      workspaceLayers: [null, 'color-mix(in srgb, #eef5ff 82%, #ffffff)', '210 40% 98%'],
      aiBubbleFace: 'color-mix(in srgb, #eef5ff 80%, transparent)',
      cardFallback: '210 40% 98%',
      flatBubble: true,
      dsTextTokens: { primary: '#334155', secondary: null, muted: null },
    })
    for (const ctx of [presetSolid, pluginGradient, dshSkin]) {
      const fp = fingerprintOf(ctx)
      const tokens: BackdropTokens | null = await new Promise((resolve) => {
        void applyRenderedBackdropContrast(ctx, resolve)
      })
      expect(tokens, `指纹 ${fp} 应产出令牌`).not.toBeNull()
      expect(tokens!.fingerprint).toBe(fp)
      expect(tokens!.foreground.split(' ')).toHaveLength(3)
      expect(tokens!.region.chat.fg).toMatch(/^#[0-9a-f]{6}$/)
      expect(tokens!.region.workspace.mutedFg.split(' ')).toHaveLength(3)
    }
  })

  it('无图主题端到端：根级+区域全发射 + 回写槽回调 + localStorage 跨会话记忆', async () => {
    const ctx = baseCtx({ mainBg: '#111318', textSeed: '#e6e6e6' })
    setInlineVars({ '--foreground': '#e6e6e6', '--chat-bg-color': '#111318' })
    const onResolved = vi.fn()
    await applyRenderedBackdropContrast(ctx, onResolved)
    expect(onResolved).toHaveBeenCalledWith(
      expect.objectContaining({ fingerprint: fingerprintOf(ctx) }),
    )
    const root = document.documentElement.style
    expect(root.getPropertyValue('--foreground').split(' ')).toHaveLength(3)
    expect(root.getPropertyValue('--muted-foreground').split(' ')).toHaveLength(3)
    expect(root.getPropertyValue('--region-chat-fg')).toMatch(/^#[0-9a-f]{6}$/)
    expect(root.getPropertyValue('--region-sidebar-fg')).toMatch(/^#[0-9a-f]{6}$/)
    expect(root.getPropertyValue('--region-workspace-muted-fg').split(' ')).toHaveLength(3)
    expectReadable(root.getPropertyValue('--region-chat-fg'), [hexRgb('#111318')])
    expect(localStorage.getItem(STORAGE_PREFIX + fingerprintOf(ctx))).not.toBeNull()
  })

  it('图片主题端到端：HDR 图薄遮罩触发 scrim 加强并发射；同指纹二次调用零解码', async () => {
    const stub = stubImageDecoding(() => hdrPixels())
    const ctx = baseCtx({
      mainBg: '#04060f',
      imageUrl: '/assets/art.webp',
      overlay: 'rgba(0, 0, 0, 0.1)',
    })
    let resolveTokens!: (t: BackdropTokens | null) => void
    const tokensPromise = new Promise<BackdropTokens | null>((resolve) => {
      resolveTokens = resolve
    })
    const pipeline = applyRenderedBackdropContrast(ctx, resolveTokens)
    stub.lastImage()!.onload()
    const tokens = await tokensPromise
    await pipeline
    expect(stub.lastImage()!.src).toBe('/assets/art.webp')
    expect(tokens).not.toBeNull()
    expect(tokens!.overlayOverride).not.toBeNull()
    expect(document.documentElement.style.getPropertyValue('--bg-overlay')).toBe(
      tokens!.overlayOverride,
    )
    expect(document.documentElement.style.getPropertyValue('--bg-overlay-opacity')).toBe('1')
    // 加强后包络：令牌前景对包络两端可读（复算加强栈）
    const alpha = Number(tokens!.overlayOverride!.match(/([\d.]+)\)$/)![1])
    const strengthened = compositeOver(
      hdrPixels(),
      fillSolid({ r: 0, g: 0, b: 0, a: alpha }, hdrPixels().length),
    )
    const { low, high } = backdropEnvelope(strengthened)
    expectReadable(tokens!.foreground, [low, high])
    expectReadable(tokens!.region.chat.fg, [low, high])

    // 同指纹二次调用：走记忆，解码计数不变（同主题只算一次）
    const before = stub.decodeCount()
    let resolveSecond!: (t: BackdropTokens | null) => void
    const secondPromise = new Promise<BackdropTokens | null>((resolve) => {
      resolveSecond = resolve
    })
    void applyRenderedBackdropContrast(ctx, resolveSecond)
    const second = await secondPromise
    expect(second).toEqual(tokens)
    expect(stub.decodeCount()).toBe(before)
  })

  it('图片加载失败：fail-open 回调 null（store 侧还原写入者值）', async () => {
    const stub = stubImageDecoding(() => hdrPixels())
    const ctx = baseCtx({ imageUrl: '/assets/broken.webp' })
    const onResolved = vi.fn()
    const promise = applyRenderedBackdropContrast(ctx, onResolved)
    stub.lastImage()!.onerror()
    await promise
    expect(onResolved).toHaveBeenCalledWith(null)
    expect(localStorage.getItem(STORAGE_PREFIX + fingerprintOf(ctx))).toBeNull()
  })

  it('快速切主题防串写：旧主题异步完成后不发射（最新指纹已易主）', async () => {
    const stub = stubImageDecoding(() => hdrPixels())
    const ctxA = baseCtx({ mainBg: '#000000', imageUrl: '/assets/a.webp', overlay: 'rgba(0,0,0,0.1)' })
    const ctxB = baseCtx({ mainBg: '#ffffff', textSeed: '#111318' })
    const onA = vi.fn()
    const promiseA = applyRenderedBackdropContrast(ctxA, onA)
    // A 停在解码等待；B（纯色，同步走完）后到
    await applyRenderedBackdropContrast(ctxB, vi.fn())
    const fgBefore = document.documentElement.style.getPropertyValue('--foreground')
    stub.lastImage()!.onload()
    await promiseA
    expect(onA).not.toHaveBeenCalled()
    expect(document.documentElement.style.getPropertyValue('--foreground')).toBe(fgBefore)
  })

  it('gatherBackdropContext：内联变量 → 上下文字段（区域链/表面对/ds 文字全收集）', () => {
    setInlineVars({
      '--bg-image': 'url(/assets/art.webp)',
      '--bg-overlay': 'rgba(0, 0, 0, 0.35)',
      '--foreground': '210 40% 98%',
      '--card': '222 47% 11%',
      '--card-foreground': '210 40% 98%',
      '--popover': '222 47% 11%',
      '--popover-foreground': '210 40% 98%',
      '--bubble-ai-bg': 'color-mix(in srgb, #0A1226 80%, transparent)',
      '--bubble-user-bg': '#d9738f',
      '--bubble-user-text': '#F8F3F5',
      '--ds-text-primary': '#334155',
    })
    const ctx = gatherBackdropContext({ mainBg: '#04060f', flatBubble: true })!
    expect(ctx.mainBg).toBe('#04060f')
    expect(ctx.imageUrl).toBe('/assets/art.webp')
    expect(ctx.textSeed).toBe('210 40% 98%')
    expect(ctx.userBubbleText).toBe('#F8F3F5')
    expect(ctx.cardFallback).toBe('222 47% 11%')
    expect(ctx.surfacePairs.map((p) => p.token)).toEqual([
      '--card-foreground',
      '--popover-foreground',
    ])
    expect(ctx.dsTextTokens.primary).toBe('#334155')
    // 区域链收集：侧栏以 --card 兜底
    expect(ctx.sidebarLayers.at(-1)).toBe('222 47% 11%')
  })

  it('背景图模式：聊天区 color 槽让位（与 index.css 规则对齐），region-chat-bg 仍生效', () => {
    document.body.classList.add('has-bg-image')
    setInlineVars({
      '--chat-bg-color': '#04060f',
      '--region-chat-bg': 'transparent',
      '--chat-bg-image': 'none',
    })
    const ctx = gatherBackdropContext({ mainBg: '#000000', flatBubble: false })!
    expect(ctx.chatLayers).toEqual(['transparent', null, 'none'])
    document.body.classList.remove('has-bg-image')
    const ctxNoImage = gatherBackdropContext({ mainBg: '#000000', flatBubble: false })!
    expect(ctxNoImage.chatLayers).toEqual(['transparent', '#04060f', 'none'])
  })

  it('ctx=null：直接返回不回调（非浏览器环境守卫）', async () => {
    const onResolved = vi.fn()
    await applyRenderedBackdropContrast(null, onResolved)
    expect(onResolved).not.toHaveBeenCalled()
  })

  it('无 Image 全局（SSR/Node 打包上下文）：守卫短路返回 null 不抛错', async () => {
    vi.stubGlobal('Image', undefined)
    await expect(loadImagePixels('/assets/x.webp')).resolves.toBeNull()
  })

  it('getContext 返回 null（无 2d 上下文）：fail-open 返回 null', async () => {
    // 不打 getContext 桩：jsdom 裸 canvas 的 getContext('2d') 返回 null
    const images: Array<{ onload: () => void }> = []
    class FakeImage {
      onload: () => void = () => {}
      onerror: () => void = () => {}
      src = ''
      constructor() {
        images.push(this)
      }
    }
    vi.stubGlobal('Image', FakeImage)
    const promise = loadImagePixels('/assets/x.webp')
    images.at(-1)!.onload()
    await expect(promise).resolves.toBeNull()
  })

  it('canvas 采样抛错（跨域污染等）：onload 后仍 fail-open 返回 null', async () => {
    const images: Array<{ onload: () => void }> = []
    class FakeImage {
      onload: () => void = () => {}
      onerror: () => void = () => {}
      src = ''
      constructor() {
        images.push(this)
      }
    }
    vi.stubGlobal('Image', FakeImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (
      this: HTMLCanvasElement,
    ) {
      return {
        drawImage: () => {
          throw new Error('tainted canvas')
        },
        getImageData: () => ({ data: new Uint8ClampedArray(4) }) as ImageData,
      } as unknown as CanvasRenderingContext2D
    })
    const promise = loadImagePixels('/assets/x.webp')
    images.at(-1)!.onload()
    await expect(promise).resolves.toBeNull()
  })

  it('缓存载荷损坏：忽略缓存走重算（fail-open）', async () => {
    const ctx = baseCtx({ mainBg: '#111318', textSeed: '#e6e6e6' })
    localStorage.setItem(STORAGE_PREFIX + fingerprintOf(ctx), '{corrupt json')
    const onResolved = vi.fn()
    await applyRenderedBackdropContrast(ctx, onResolved)
    expect(onResolved).toHaveBeenCalledWith(
      expect.objectContaining({ fingerprint: fingerprintOf(ctx) }),
    )
  })

  it('emitBackdropTokens：语义表面对与 ds 文字逐令牌发射', () => {
    emitBackdropTokens({
      fingerprint: 'fp',
      foreground: '0 0% 100%',
      mutedForeground: '210 5% 60%',
      region: {
        sidebar: { fg: '#ffffff', mutedFg: '210 5% 60%' },
        chat: { fg: '#ffffff', mutedFg: '210 5% 60%' },
        workspace: { fg: '#ffffff', mutedFg: '210 5% 60%' },
      },
      bubbleUserText: '#ffffff',
      bubbleAiText: null,
      surfaceOverrides: { '--card-foreground': '0 0% 0%' },
      dsTextOverrides: { '--ds-text-primary': '#ffffff' },
      overlayOverride: null,
    })
    const root = document.documentElement.style
    expect(root.getPropertyValue('--card-foreground')).toBe('0 0% 0%')
    expect(root.getPropertyValue('--ds-text-primary')).toBe('#ffffff')
    expect(root.getPropertyValue('--bubble-user-text')).toBe('#ffffff')
    expect(root.getPropertyValue('--bubble-ai-text')).toBe('')
    expect(root.getPropertyValue('--bg-overlay')).toBe('')
  })
})

// ── 3. store 接线 ──

describe('themeStore 接线', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('style')
  })

  it('applyTheme 后 backdropResolved 槽写入 + 根级/区域令牌同步发射（纯色主题零异步）', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    const { darkTheme } = await import('@/config/themes')
    useThemeStore.setState({ themeConfig: structuredClone(darkTheme), activePluginTheme: null })
    useThemeStore.getState().applyTheme()
    // 纯色主题无解码：管线同步完成，槽与 DOM 双面落位
    expect(useThemeStore.getState().backdropResolved).not.toBeNull()
    const fg = document.documentElement.style.getPropertyValue('--foreground')
    expect(fg.split(' ')).toHaveLength(3)
    const chatBg =
      document.documentElement.style.getPropertyValue('--chat-bg-color') || '#04060f'
    const chatRegionFg = document.documentElement.style.getPropertyValue('--region-chat-fg')
    expect(chatRegionFg).toMatch(/^#[0-9a-f]{6}$/)
    expectReadable(chatRegionFg, [hexRgb(chatBg)])
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
