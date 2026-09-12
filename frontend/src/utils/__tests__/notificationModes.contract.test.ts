// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * 通知分类内置默认件 ↔ human 插件 manifest 声明对账（总纲 #14）。
 *
 * DEFAULT_CATEGORY_DECLS 是 human plugin.json capabilities.tools[].
 * ui.notification_modes 的前端镜像（注释自认"保持同构"）。声明装载后可
 * 覆盖，但未覆盖/装载失败路径仍走此表——漂移即运行时行为分叉。
 * 本闸锁：manifest 声明的每个 category 的 features 与 icon 和内置默认件
 * 逐一相等。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { DEFAULT_CATEGORY_DECLS } from '../notificationModes'

const REPO_ROOT = resolve(__dirname, '../../../..')
const HUMAN_MANIFEST = resolve(REPO_ROOT, 'plugins/shared/tools/human/plugin.json')

interface CategoryDecl {
  category: string
  features: string[]
  icon?: string
}

function parseManifestNotificationModes(): CategoryDecl[] {
  const manifest = JSON.parse(readFileSync(HUMAN_MANIFEST, 'utf-8')) as {
    capabilities?: { tools?: Array<{ ui?: { notification_modes?: CategoryDecl[] } }> }
  }
  const decls: CategoryDecl[] = []
  for (const tool of manifest.capabilities?.tools ?? []) {
    for (const d of tool.ui?.notification_modes ?? []) decls.push(d)
  }
  return decls
}

describe('notificationModes 内置默认件 ↔ human manifest 对账', () => {
  it('manifest 声明的每个 category 在内置默认件中同构（features/icon 逐一相等）', () => {
    const decls = parseManifestNotificationModes()
    expect(decls.length).toBeGreaterThan(0, 'human manifest 无 notification_modes 声明——契约面已变，本闸需同步改版')
    for (const decl of decls) {
      const fallback = DEFAULT_CATEGORY_DECLS[decl.category]
      expect(fallback, `内置默认件缺 manifest 声明的 category "${decl.category}"`).toBeDefined()
      expect(fallback.features).toEqual(decl.features)
      expect(fallback.icon).toEqual(decl.icon)
    }
  })

  it('内置默认件无幽灵分类（每个 category 在 manifest 中有声明源）', () => {
    const declared = new Set(parseManifestNotificationModes().map((d) => d.category))
    for (const category of Object.keys(DEFAULT_CATEGORY_DECLS)) {
      expect(declared.has(category), `内置默认件 category "${category}" 在 manifest 无声明源`).toBe(true)
    }
  })
})
