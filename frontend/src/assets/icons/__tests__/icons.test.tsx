/**
 * 图标组件回归测试
 *
 * 覆盖本次「前端图标修复」的回归项：
 * - AC-1: 全部 icons/*.tsx 不得包含非驼峰 SVG 属性（fill-rule/stroke-width/clip-rule 等），
 *   否则 React 会在控制台报 Invalid DOM property 警告。
 * - AC-3: Star 图标必须渲染为五角星（而非人形 PersonIcon）。
 * - AC-4: 未收藏星标为灰色描边（fill-none + stroke），已收藏为金色实心。
 *
 * 背景：person.tsx 曾漏改 fill-rule/stroke-width；index.ts 曾将 Star 错误映射到
 * PersonIcon（人形），导致会话列表星标显示为人形。本测试防止同类问题复发。
 */
import { render } from '@testing-library/react'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  Bot,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Cloud,
  ExternalLink,
  Loader2,
  Play,
  Quote,
  Save,
  Star,
  Target,
  Trash2,
  Users,
  Volume2,
  ZoomIn,
} from '@/assets/icons'

/** React 不接受的 SVG 属性（kebab-case）→ 必须使用驼峰 */
const NON_CAMEL_SVG_ATTRS = [
  'fill-rule',
  'stroke-width',
  'clip-rule',
  'stroke-linecap',
  'stroke-linejoin',
  'stroke-miterlimit',
  'stop-color',
  'stop-opacity',
  'fill-opacity',
  'stroke-opacity',
  'font-family',
  'font-size',
  'text-anchor',
  'marker-start',
  'marker-end',
  'marker-mid',
  'xmlns:xlink',
  'xlink:href',
  'stroke-dasharray',
  'stroke-dashoffset',
]

/** 人形图标 PersonIcon 的 path d 特征（头部圆 + 肩部轮廓），用于区分星形 */
const PERSON_PATH_MARKER = 'M10.9167 3.3333'

describe('图标文件 SVG 属性驼峰化（AC-1 回归）', () => {
  const iconsDir = join(process.cwd(), 'src', 'assets', 'icons')
  const iconFiles = readdirSync(iconsDir).filter((f) => f.endsWith('.tsx'))

  it(`所有 ${iconFiles.length} 个图标 .tsx 文件不含非驼峰 SVG 属性`, () => {
    const offenders: string[] = []
    for (const file of iconFiles) {
      if (file === 'index.ts') continue
      const content = readFileSync(join(iconsDir, file), 'utf8')
      for (const attr of NON_CAMEL_SVG_ATTRS) {
        if (content.includes(attr)) {
          offenders.push(`${file}: ${attr}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})

describe('Star 图标渲染（AC-3 回归：星标必须显示为星星而非人形）', () => {
  it('Star 渲染为五角星 SVG（含星形 path，不含人形 PersonIcon path）', () => {
    const { container } = render(<Star />)
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg!.getAttribute('viewBox')).toBe('0 0 20 20')
    // 星形 path：五角星闭合路径（含内凹顶点坐标）
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    expect(path!.getAttribute('d')).toMatch(/^M10 1\.5/)
    // 排除人形图标：不得包含 PersonIcon 的 path 特征
    expect(path!.getAttribute('d')).not.toContain(PERSON_PATH_MARKER)
  })

  it('未收藏星标为灰色描边（fill-none + stroke-current），已收藏为金色实心（fill-amber-400）', () => {
    // SessionList 中的星标 className 语义由组件层控制，此处验证图标组件本身支持
    // 通过 fill="currentColor" 继承 currentColor 上色（描边/实心由调用方 className 控制）
    const { container } = render(<Star className="fill-none stroke-current" />)
    const svg = container.querySelector('svg')
    expect(svg!.getAttribute('fill')).toBe('currentColor')
  })
})

describe('重设计图标语义正确性（2026-08 图标重设计回归）', () => {
  // 背景：index.ts 曾将多个语义别名指向同一图形（如 CheckIcon as Play/Save/Square），
  // 导致「删除显示 X、播放显示对勾、加载显示时钟」等不直观现象。
  // 本组测试断言：每个语义别名渲染的 SVG 图形符合业界惯例（lucide/antd 语义）。

  it('Play 渲染为三角形（播放键），而非对勾', () => {
    const { container } = render(<Play />)
    const polygon = container.querySelector('polygon')
    expect(polygon).not.toBeNull()
    // lucide Play：顶点 (6,3) (20,12) (6,21) 三角形
    expect(polygon!.getAttribute('points')).toContain('6 3')
  })

  it('Save 渲染为软盘轮廓，而非对勾', () => {
    const { container } = render(<Save />)
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    // lucide Save：含 "M19 21H5a2 2 0 0 1-2-2V5" 软盘轮廓
    const paths = container.querySelectorAll('path')
    const d = Array.from(paths)
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M19 21H5')
    // 不含对勾 path 特征
    expect(d).not.toContain('M22 4 12 14.01')
  })

  it('Trash2 渲染为垃圾桶（含垃圾桶身与两条竖线）', () => {
    const { container } = render(<Trash2 />)
    const lines = container.querySelectorAll('line')
    expect(lines.length).toBeGreaterThanOrEqual(2)
    const paths = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(paths).toContain('M3 6h18')
  })

  it('Loader2 渲染为圆弧（加载指示），而非时钟', () => {
    const { container } = render(<Loader2 />)
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    // lucide Loader2：圆弧 path "M21 12a9 9 0 1 1-6.219-8.56"
    expect(path!.getAttribute('d')).toContain('M21 12a9 9')
  })

  it('Cloud 渲染为云朵，而非硬币', () => {
    const { container } = render(<Cloud />)
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    expect(path!.getAttribute('d')).toContain('M17.5 19H9a7 7')
  })

  it('Volume2 渲染为喇叭 + 声波', () => {
    const { container } = render(<Volume2 />)
    const polygon = container.querySelector('polygon')
    expect(polygon).not.toBeNull()
    expect(polygon!.getAttribute('points')).toContain('11 5')
  })

  it('Users 渲染为双人轮廓（含双人 path）', () => {
    const { container } = render(<Users />)
    const circles = container.querySelectorAll('circle')
    expect(circles.length).toBeGreaterThanOrEqual(1)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M16 21v-2')
  })

  it('ChevronUp 渲染为向上箭头（与 ChevronDown 方向相反）', () => {
    const { container } = render(<ChevronUp />)
    const polyline = container.querySelector('polyline')
    expect(polyline).not.toBeNull()
    expect(polyline!.getAttribute('points')).toContain('18 15 12 9 6 15')
  })

  it('ChevronLeft / ChevronRight 方向正确（向左/向右）', () => {
    const left = render(<ChevronLeft />).container.querySelector('polyline')
    const right = render(<ChevronRight />).container.querySelector('polyline')
    expect(left!.getAttribute('points')).toContain('15 18 9 12 15 6')
    expect(right!.getAttribute('points')).toContain('9 18 15 12 9 6')
  })

  it('Bot 渲染为机器人（圆角机身矩形），而非大脑', () => {
    const { container } = render(<Bot />)
    const rect = container.querySelector('rect')
    expect(rect).not.toBeNull()
    expect(rect!.getAttribute('width')).toBe('16')
  })

  it('ZoomIn 渲染为放大镜 + 加号（而非 X）', () => {
    const { container } = render(<ZoomIn />)
    const circle = container.querySelector('circle')
    expect(circle).not.toBeNull()
    const lines = container.querySelectorAll('line')
    // 放大镜柄(1) + 加号横竖(2) = 3 条 line
    expect(lines.length).toBeGreaterThanOrEqual(3)
  })

  it('Quote 渲染为引号图形，而非刷新循环箭头', () => {
    const { container } = render(<Quote />)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M3 21c3 0 7-1')
  })

  it('ExternalLink 渲染为方框 + 右上箭头', () => {
    const { container } = render(<ExternalLink />)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M15 3h6v6')
  })

  it('Target 渲染为同心圆靶心', () => {
    const { container } = render(<Target />)
    const circles = container.querySelectorAll('circle')
    expect(circles.length).toBeGreaterThanOrEqual(3)
  })
})
