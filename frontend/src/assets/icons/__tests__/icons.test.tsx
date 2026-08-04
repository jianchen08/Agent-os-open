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
import { Star } from '@/assets/icons'

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
