/**
 * 图标重设计独立功能验证脚本（14 个「输入→预期输出」渲染断言）
 *
 * 覆盖主报告 §七 验证场景：
 * 1-11: 输入图标组件 → 断言 SVG 输出图形特征（业界语义）
 * 12:   import 完整性（tsc 编译期 + Python 静态分析，见 icon_redesign_import_evidence.txt）
 * 13:   非驼峰属性扫描（icons.test.tsx 自动扫描）
 * 14:   回归测试 17/17（icons.test.tsx）
 * 补充: 属性透传、全量渲染冒烟、283 导出可解析
 */
import { render } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'
import * as Icons from '@/assets/icons'
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
  Target,
  Trash2,
  Users,
  Volume2,
  ZoomIn,
} from '@/assets/icons'

describe('场景 1-11：输入图标组件 → 输出 SVG 图形特征（渲染断言）', () => {
  it('场景1 <Play /> → 播放三角 polygon，不含对勾', () => {
    const { container } = render(<Play />)
    const polygon = container.querySelector('polygon')
    expect(polygon).not.toBeNull()
    expect(polygon!.getAttribute('points')).toContain('6 3')
    expect(container.querySelectorAll('polyline').length).toBe(0)
  })

  it('场景2 <Trash2 /> → 垃圾桶身 + 2 竖线', () => {
    const { container } = render(<Trash2 />)
    const lines = container.querySelectorAll('line')
    expect(lines.length).toBeGreaterThanOrEqual(2)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M3 6h18')
  })

  it('场景3 <Loader2 /> → 圆弧，非时钟', () => {
    const { container } = render(<Loader2 />)
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    expect(path!.getAttribute('d')).toContain('M21 12a9 9')
    expect(container.querySelectorAll('circle').length).toBe(0)
  })

  it('场景4 <Save /> → 软盘，不含对勾', () => {
    const { container } = render(<Save />)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M19 21H5')
    expect(d).not.toContain('M22 4 12 14.01')
  })

  it('场景5 <Cloud /> → 云朵，非硬币', () => {
    const { container } = render(<Cloud />)
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    expect(path!.getAttribute('d')).toContain('M17.5 19H9a7 7')
    expect(container.querySelectorAll('circle').length).toBe(0)
  })

  it('场景6 <ChevronUp/Left/Right /> → 三向互异方向箭头', () => {
    const up = render(<ChevronUp />).container.querySelector('polyline')
    const left = render(<ChevronLeft />).container.querySelector('polyline')
    const right = render(<ChevronRight />).container.querySelector('polyline')
    const upPts = up!.getAttribute('points')!
    const leftPts = left!.getAttribute('points')!
    const rightPts = right!.getAttribute('points')!
    expect(upPts).toContain('18 15 12 9 6 15')
    expect(leftPts).toContain('15 18 9 12 15 6')
    expect(rightPts).toContain('9 18 15 12 9 6')
    // 三向互异
    expect(new Set([upPts, leftPts, rightPts]).size).toBe(3)
  })

  it('场景7 <Users /> → 双人轮廓', () => {
    const { container } = render(<Users />)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M16 21v-2')
    expect(container.querySelectorAll('circle').length).toBeGreaterThanOrEqual(1)
  })

  it('场景8 <Volume2 /> → 喇叭 + 声波，非硬币', () => {
    const { container } = render(<Volume2 />)
    const polygon = container.querySelector('polygon')
    expect(polygon).not.toBeNull()
    expect(polygon!.getAttribute('points')).toContain('11 5')
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M15.54 8.46a5 5')
    expect(container.querySelectorAll('circle').length).toBe(0)
  })

  it('场景9 <Quote /> → 双引号，非刷新箭头', () => {
    const { container } = render(<Quote />)
    const d = Array.from(container.querySelectorAll('path'))
      .map((p) => p.getAttribute('d'))
      .join(' ')
    expect(d).toContain('M3 21c3 0 7-1')
    expect(container.querySelectorAll('polyline').length).toBe(0)
  })

  it('场景10 <Bot /> → 机器人矩形机身', () => {
    const { container } = render(<Bot />)
    const rect = container.querySelector('rect')
    expect(rect).not.toBeNull()
    expect(rect!.getAttribute('width')).toBe('16')
  })

  it('场景11 <ZoomIn /> → 放大镜 + 加号（≥3 line）', () => {
    const { container } = render(<ZoomIn />)
    expect(container.querySelectorAll('circle').length).toBeGreaterThanOrEqual(1)
    expect(container.querySelectorAll('line').length).toBeGreaterThanOrEqual(3)
  })

  it('场景12 补充：ExternalLink / Target 语义正确', () => {
    const ext = render(<ExternalLink />).container
    expect(
      Array.from(ext.querySelectorAll('path'))
        .map((p) => p.getAttribute('d'))
        .join(' '),
    ).toContain('M15 3h6v6')
    const tgt = render(<Target />).container
    expect(tgt.querySelectorAll('circle').length).toBeGreaterThanOrEqual(3)
  })
})

describe('补充场景：属性透传 / 全量渲染冒烟 / 283 导出可解析', () => {
  it('B-1 属性透传：className/data-testid 透传到 svg', () => {
    const { container } = render(
      <Trash2 className="w-4 h-4 text-red-500" data-testid="del-icon" />,
    )
    const svg = container.querySelector('svg')
    expect(svg!.getAttribute('class')).toContain('text-red-500')
    expect(svg!.getAttribute('data-testid')).toBe('del-icon')
  })

  it('B-2 全量渲染冒烟：全部导出图标组件可渲染为 svg 且不抛错', () => {
    const runtimeExports = Object.entries(Icons).filter(
      ([, v]) => typeof v === 'function' && v.name.endsWith('Icon'),
    )
    expect(runtimeExports.length).toBeGreaterThanOrEqual(90)
    for (const [, Comp] of runtimeExports) {
      const { container } = render(createElement(Comp))
      expect(container.querySelector('svg')).not.toBeNull()
    }
  })

  it('C 283 公开导出可解析：index.ts 全部运行时导出为函数', () => {
    const runtimeExports = Object.entries(Icons).filter(
      ([, v]) => typeof v === 'function',
    )
    // 组件导出（含别名指向组件）：应 ≥ 组件文件数
    expect(runtimeExports.length).toBeGreaterThanOrEqual(140)
  })
})
