// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * 交互模式内置默认件 ↔ human 插件 manifest 声明对账（总纲 #14）。
 *
 * DEFAULT_MODE_FEATURES 是 human plugin.json capabilities.tools[].
 * ui.interaction_modes 的前端镜像（注释自认"保持同构"）。插件声明装载后
 * 会覆盖内置默认件，但未覆盖路径仍走此表——两表漂移即运行时行为分叉。
 * 本闸锁：manifest 声明的每个 mode 的 features 与内置默认件逐一相等。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { DEFAULT_MODE_FEATURES } from '../interactionModes'

const REPO_ROOT = resolve(__dirname, '../../../..')
const HUMAN_MANIFEST = resolve(REPO_ROOT, 'plugins/shared/tools/human/plugin.json')

interface ModeDecl {
  mode: string
  features: string[]
}

function parseManifestInteractionModes(): ModeDecl[] {
  const manifest = JSON.parse(readFileSync(HUMAN_MANIFEST, 'utf-8')) as {
    capabilities?: { tools?: Array<{ ui?: { interaction_modes?: ModeDecl[] } }> }
  }
  const decls: ModeDecl[] = []
  for (const tool of manifest.capabilities?.tools ?? []) {
    for (const d of tool.ui?.interaction_modes ?? []) decls.push(d)
  }
  return decls
}

describe('interactionModes 内置默认件 ↔ human manifest 对账', () => {
  it('manifest 声明的每个 mode 在内置默认件中同构（features 逐一相等）', () => {
    const decls = parseManifestInteractionModes()
    expect(decls.length).toBeGreaterThan(0, 'human manifest 无 interaction_modes 声明——契约面已变，本闸需同步改版')
    for (const decl of decls) {
      const fallback = DEFAULT_MODE_FEATURES[decl.mode]
      expect(fallback, `内置默认件缺 manifest 声明的 mode "${decl.mode}"`).toBeDefined()
      expect(fallback.features).toEqual(decl.features)
    }
  })

  it('内置默认件无幽灵模式（每个 mode 在 manifest 中有声明源）', () => {
    const declared = new Set(parseManifestInteractionModes().map((d) => d.mode))
    for (const mode of Object.keys(DEFAULT_MODE_FEATURES)) {
      expect(declared.has(mode), `内置默认件 mode "${mode}" 在 manifest 无声明源`).toBe(true)
    }
  })
})
