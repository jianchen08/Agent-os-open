/**
 * harness 自检：源码扫描工具本身可工作
 *
 * 意图：架构契约测试依赖 harness 读源码；harness 自己必须先被验证，
 * 否则契约测试的"零命中/非零命中"结论不可信。
 */

import { describe, expect, it } from 'vitest'
import { countPatternInDir, listSourceFiles, readSource, scanSourceForPattern } from './harness'

describe('architecture harness — 源码扫描工具', () => {
  it('readSource 能读已知文件', () => {
    const content = readSource('src/services/schema/WidgetRegistry.ts')
    expect(content).toContain('WidgetRegistry')
  })

  it('readSource 不存在文件返回空串（不抛错）', () => {
    expect(readSource('src/__nonexistent__.tsx')).toBe('')
  })

  it('listSourceFiles 收集 .ts/.tsx，跳过 __tests__/node_modules/dist', () => {
    const files = listSourceFiles('src/services/schema')
    expect(files.some((f) => f.endsWith('WidgetRegistry.ts'))).toBe(true)
    // 测试文件不应进入生产源码清单
    expect(files.some((f) => f.includes('__tests__'))).toBe(false)
  })

  it('scanSourceForPattern 返回命中文件与行号', () => {
    const hits = scanSourceForPattern('getAllWidgets', [
      'src/services/schema/ContributionRegistry.ts',
    ])
    expect(hits.length).toBeGreaterThan(0)
    expect(hits[0].file).toBe('src/services/schema/ContributionRegistry.ts')
    expect(hits[0].line).toBeGreaterThan(0)
  })

  it('countPatternInDir 在目录树上计数', () => {
    const count = countPatternInDir('class ContributionRegistry', 'src/services/schema')
    expect(count).toBeGreaterThanOrEqual(1)
  })
})
