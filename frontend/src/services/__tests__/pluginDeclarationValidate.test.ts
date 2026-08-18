/**
 * 插件声明校验器测试：
 * - 负例（证明校验器对故意坏输入必报错，不是空转）；
 * - 真实语料（仓库 plugins/shared 下的真实 page/tool/widget 声明全过——
 *   若校验器在真实数据上抓不到问题，多半是坏的/没接上）。
 */
import { readdirSync, readFileSync } from 'fs'
import { join } from 'path'
import { describe, it, expect } from 'vitest'
import {
  validatePluginDeclaration,
  type PluginDeclarationInput,
} from '../pluginDeclarationValidate'

// ── 负例：校验器必须抓到故意坏输入 ─────────────────────────────

function errorsOf(input: PluginDeclarationInput): string[] {
  return validatePluginDeclaration(input).errors
}

describe('validatePluginDeclaration 负例（校验器不空转）', () => {
  it('page 缺 id → error', () => {
    const errs = errorsOf({ pages: [{ space: 'workspace' }] })
    expect(errs.some((e) => e.includes('缺 id'))).toBe(true)
  })

  it('page 未知 space → error（页面空间是封闭集合）', () => {
    const errs = errorsOf({ pages: [{ id: 'p', space: 'mars' }] })
    expect(errs.some((e) => e.includes('未知 space'))).toBe(true)
  })

  it('字段缺 name → error（渲染端会整条丢弃）', () => {
    const errs = errorsOf({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ type: 'string', label: 'x' }] } }],
    })
    expect(errs.some((e) => e.includes('缺 name'))).toBe(true)
  })

  it('字段未知 type → warning（落回 string 渲染，不崩但失实）', () => {
    const r = validatePluginDeclaration({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'a', type: 'not_a_type', label: 'x' }] } }],
    })
    expect(r.warnings.some((e) => e.includes('未知 type'))).toBe(true)
    expect(r.errors).toEqual([])
  })

  it('chat_card 非法块 type → error（该块被跳过、内容丢失）', () => {
    const errs = errorsOf({
      tools: [{ name: 't', ui: { chat_card: { title: 'x', blocks: [{ type: 'hologram' }] } } }],
    })
    expect(errs.some((e) => e.includes('非法块 type'))).toBe(true)
  })

  it('widget 缺 id/type → error（无法路由）', () => {
    const errs = errorsOf({ uiSchemaWidgets: [{ type: 'form', space: 'workspace' }] })
    expect(errs.some((e) => e.includes('缺 id'))).toBe(true)
  })

  it('select 字段缺 options 且无 datasource → warning（下拉为空）', () => {
    const r = validatePluginDeclaration({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'mode', type: 'select', label: '模式' }] } }],
    })
    expect(r.warnings.some((e) => e.includes('缺 options/datasourceUri'))).toBe(true)
  })

  it('source 模板括号未配平 → warning（渲染空值）', () => {
    const r = validatePluginDeclaration({
      tools: [{ name: 't', ui: { chat_card: { title: 'x', blocks: [{ type: 'text', source: 'result.{{status' }] } } }],
    })
    expect(r.warnings.some((e) => e.includes('模板括号未配平'))).toBe(true)
  })
})

// ── 真实语料：仓库 plugins/shared 下真实声明全过，采集问题暴露 ──

interface Decl {
  file: string
  kind: string
  name?: string
}

function walk(dir: string): string[] {
  const out: string[] = []
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    if (['node_modules', '.venv', '__pycache__', 'dsh_plugins', 'runtime', '.git'].includes(ent.name)) continue
    const p = join(dir, ent.name)
    if (ent.isDirectory()) out.push(...walk(p))
    else if (ent.name === 'plugin.json') out.push(p)
  }
  return out
}

describe('validatePluginDeclaration 真实语料（校验器在真实数据上验证）', () => {
  it('仓库全部插件声明通过，且显式列出找不到任何问题时的证明', () => {
    const files = walk(join(__dirname, '../../../../plugins/shared'))
    expect(files.length).toBeGreaterThan(10)
    const allErrors: string[] = []
    const allWarnings: string[] = []
    let declared = 0
    const declKinds: Map<string, number> = new Map()

    for (const f of files) {
      const d = JSON.parse(readFileSync(f, 'utf-8'))
      const input: PluginDeclarationInput = {}
      const contributes = (d.contributes ?? {}) as Record<string, unknown>
      const pages = Array.isArray(contributes.pages) ? contributes.pages : undefined
      if (pages?.length) {
        input.pages = pages as Array<Record<string, unknown>>
        declKinds.set('pages', (declKinds.get('pages') ?? 0) + pages.length)
      }
      const tools = (d.capabilities?.tools ?? []) as Array<Record<string, unknown>>
      if (tools.length) {
        input.tools = tools
        declKinds.set('tools', (declKinds.get('tools') ?? 0) + tools.length)
      }
      const widgets = (d.ui_schema?.widgets ?? []) as Array<Record<string, unknown>>
      if (widgets.length) {
        input.uiSchemaWidgets = widgets
        declKinds.set('ui_schema.widgets', (declKinds.get('ui_schema.widgets') ?? 0) + widgets.length)
      }
      if (!input.pages && !input.tools && !input.uiSchemaWidgets) continue
      declared += 1
      const r = validatePluginDeclaration(input)
      for (const e of r.errors) allErrors.push(`${f}: ${e}`)
      for (const w of r.warnings) allWarnings.push(`${f}: ${w}`)
    }

    // 真实验证面覆盖（校验器跑到了，不是空转）
    expect(declared).toBeGreaterThan(5)
    // 打印采到的问题（vitest 输出可见）
    // eslint-disable-next-line no-console
    console.log(
      `真实语料: ${files.length} 插件 / ${declared} 含声明 / 面=${JSON.stringify(Object.fromEntries(declKinds))}`,
    )

    if (allErrors.length > 0) {
      // 校验器在真实数据上抓到了硬错误 → 显式失败并要求修复
      // eslint-disable-next-line no-console
      console.log('真实语料硬错误：\n' + allErrors.slice(0, 40).join('\n'))
    }
    if (allWarnings.length > 0) {
      // eslint-disable-next-line no-console
      console.log('真实语料软警告：\n' + allWarnings.slice(0, 40).join('\n'))
    }
    // 硬错误不允许存在（有=声明不生效，应修 manifest 或改校准）；软警告容忍
    expect(allErrors).toEqual([])
  })
})
