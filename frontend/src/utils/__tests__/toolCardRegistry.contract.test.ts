/** @ci: frontend-test */
/**
 * 工具中文名映射表 ↔ 插件 manifest 工具名对账（总纲 #14）。
 *
 * TOOL_NAME_ZH 是前端硬编码的工具名→中文映射（L0 标题人性化）。表键必须
 * 是真实声明的插件工具名：manifest 改名/删除工具 → 本测试红（映射键成为
 * 幽灵词）。方向为"键集合 ⊆ 声明面"——新工具未映射走通用人性化兜底，
 * 不强制逐工具补中文。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { TOOL_NAME_ZH } from '../toolCardRegistry'

const REPO_ROOT = resolve(__dirname, '../../../..')

/** 收集 plugins 目录全部 plugin.json 声明的工具名全集 */
function collectDeclaredToolNames(): Set<string> {
  const names = new Set<string>()
  const walk = (dir: string): void => {
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }
    for (const name of entries) {
      const full = join(dir, name)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }
      if (stat.isDirectory()) {
        if (name !== 'node_modules' && name !== 'runtime' && name !== '__pycache__' && !name.startsWith('.')) {
          walk(full)
        }
        continue
      }
      if (name !== 'plugin.json') continue
      try {
        const manifest = JSON.parse(readFileSync(full, 'utf-8')) as {
          capabilities?: { tools?: Array<{ name?: string }> }
        }
        for (const tool of manifest.capabilities?.tools ?? []) {
          if (tool.name) names.add(tool.name)
        }
      } catch {
        // 不可读/非 JSON 跳过
      }
    }
  }
  walk(join(REPO_ROOT, 'plugins'))
  return names
}

describe('TOOL_NAME_ZH ↔ 插件 manifest 工具名对账', () => {
  it('映射表每个键都是真实声明的插件工具名（无幽灵键）', () => {
    const declared = collectDeclaredToolNames()
    expect(declared.size).toBeGreaterThan(0)
    const ghosts = Object.keys(TOOL_NAME_ZH).filter((k) => !declared.has(k))
    expect(ghosts).toEqual([])
  })
})
