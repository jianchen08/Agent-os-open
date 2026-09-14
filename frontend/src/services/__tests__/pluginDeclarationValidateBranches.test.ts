/**
 * pluginDeclarationValidate 分支补测：把校验器逐条规则的正/负两面都走一遍
 * （pages / tools.render / chat_card / widgets / streaming），
 * 补齐既有负例测试未覆盖的「合法路径 + 边界形状」分支。
 *
 * 断言可观察行为：返回的 errors / warnings 文案与 valid 派生值。
 */
import { describe, expect, it } from 'vitest'
import {
  validatePluginDeclaration,
  type PluginDeclarationInput,
} from '../pluginDeclarationValidate'

const run = (input: PluginDeclarationInput | undefined) => validatePluginDeclaration(input)

describe('validatePluginDeclaration — 输入边界与 valid 派生', () => {
  it('undefined 输入返回空 errors/warnings 且 valid 为 true', () => {
    const r = run(undefined)
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
    expect(r.valid).toBe(true)
  })

  it('空对象输入同样合法（无声明面无问题）', () => {
    const r = run({})
    expect(r.valid).toBe(true)
    expect(r.errors).toEqual([])
  })

  it('valid 随 errors 出现转为 false（同一实例动态派生，非构造期快照）', () => {
    const bad = run({ pages: [{ space: 'settings' }] })
    expect(bad.valid).toBe(false)
    const good = run({ pages: [{ id: 'p', space: 'settings' }] })
    expect(good.valid).toBe(true)
  })

  it('仅有 warning 时 valid 仍为 true（warning 不阻断）', () => {
    const r = run({
      pages: [
        { id: 'p', space: 'settings', schema: { fields: [{ name: 'a', type: 'nope' }] } },
      ],
    })
    expect(r.warnings.length).toBeGreaterThan(0)
    expect(r.valid).toBe(true)
  })
})

describe('validatePluginDeclaration — pages 校验', () => {
  it('page 缺 space → error', () => {
    const r = run({ pages: [{ id: 'p' }] })
    expect(r.errors.some((e) => e.includes('缺 space'))).toBe(true)
  })

  it('page.schema 非对象 → warning，不继续校验字段', () => {
    const r = run({ pages: [{ id: 'p', space: 'settings', schema: 'oops' }] })
    expect(r.warnings.some((e) => e.includes('.schema 非对象'))).toBe(true)
    expect(r.errors).toEqual([])
  })

  it('schema.fields 非数组 → error', () => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: { name: 'a' } } }],
    })
    expect(r.errors.some((e) => e.includes('.fields 应为数组'))).toBe(true)
  })

  it('schema.fields 为 null 视同未声明（不报错）', () => {
    const r = run({ pages: [{ id: 'p', space: 'settings', schema: { fields: null } }] })
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })

  it('fields 含非对象条目 → error 且继续校验后续条目', () => {
    const r = run({
      pages: [
        {
          id: 'p',
          space: 'settings',
          schema: { fields: [null, { type: 'string' }] },
        },
      ],
    })
    expect(r.errors.some((e) => e.includes('[0] 不是对象'))).toBe(true)
    expect(r.errors.some((e) => e.includes('[1] 缺 name'))).toBe(true)
  })

  it.each([
    ['settings'],
    ['workspace'],
    ['chat'],
    ['floating'],
    ['dock'],
    ['fullscreen'],
    ['debug_center'],
  ])('封闭空间集内的 space=%s 通过', (space) => {
    expect(run({ pages: [{ id: 'p', space }] }).errors).toEqual([])
  })

  it('空白字符串 id/space 视同缺失（trim 后为空 → error）', () => {
    const r = run({ pages: [{ id: '   ', space: '  ' }] })
    expect(r.errors.some((e) => e.includes('缺 id'))).toBe(true)
    expect(r.errors.some((e) => e.includes('缺 space'))).toBe(true)
  })
})

describe('validatePluginDeclaration — 字段类型与下拉源', () => {
  it.each([
    'string',
    'number',
    'boolean',
    'select',
    'multiselect',
    'textarea',
    'date',
    'file',
    'input',
    'toggle',
    'slider',
    'color',
    'radio',
    'checkbox',
  ])('词汇表内 type=%s 不产生 warning', (type) => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'f', type }] } }],
    })
    expect(r.warnings.filter((w) => w.includes('未知 type'))).toEqual([])
  })

  it.each(['select', 'multiselect', 'radio', 'checkbox'])(
    '下拉型 %s 有 options 时不再告警',
    (type) => {
      const r = run({
        pages: [
          {
            id: 'p',
            space: 'settings',
            schema: { fields: [{ name: 'f', type, options: [{ label: 'a', value: 'a' }] }] },
          },
        ],
      })
      expect(r.warnings.filter((w) => w.includes('缺 options/datasourceUri'))).toEqual([])
    },
  )

  it('下拉型字段仅有非空 datasourceUri 也可（动态源合法）', () => {
    const r = run({
      pages: [
        {
          id: 'p',
          space: 'settings',
          schema: { fields: [{ name: 'f', type: 'select', datasourceUri: '/api/list' }] },
        },
      ],
    })
    expect(r.warnings).toEqual([])
  })

  it('下拉型字段空数组 options + 空白 datasourceUri → 仍告警', () => {
    const r = run({
      pages: [
        {
          id: 'p',
          space: 'settings',
          schema: { fields: [{ name: 'f', type: 'select', options: [], datasourceUri: ' ' }] },
        },
      ],
    })
    expect(r.warnings.some((w) => w.includes('缺 options/datasourceUri'))).toBe(true)
  })

  it('非下拉型字段无 options 不告警（仅下拉型规则）', () => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'f', type: 'string' }] } }],
    })
    expect(r.warnings).toEqual([])
  })

  it('字段 type 未声明（undefined）不告警（缺省由渲染端决定）', () => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'f' }] } }],
    })
    expect(r.warnings).toEqual([])
  })

  it.each([
    ['datasourceUri', { datasourceUri: '/x/{{a}}' }],
    ['source', { source: '{{a}} + {{b}}' }],
    ['defaultSource', { defaultSource: '{{a}}' }],
  ])('模板键 %s 配平时不告警', (_key, extra) => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'f', ...extra }] } }],
    })
    expect(r.warnings.filter((w) => w.includes('模板括号未配平'))).toEqual([])
  })

  it.each([
    ['datasourceUri', { datasourceUri: '/x/{{a' }],
    ['source', { source: '{{a}' }],
    ['defaultSource', { defaultSource: '{{a}} }}' }],
  ])('模板键 %s 未配平时告警并点名该键', (_key, extra) => {
    const r = run({
      pages: [{ id: 'p', space: 'settings', schema: { fields: [{ name: 'f', ...extra }] } }],
    })
    expect(r.warnings.some((w) => w.includes(`${_key} 模板括号未配平`))).toBe(true)
  })

  it('非字符串模板值被跳过（number/对象不参与配平）', () => {
    const r = run({
      pages: [
        {
          id: 'p',
          space: 'settings',
          schema: { fields: [{ name: 'f', datasourceUri: 42, source: { x: 1 } }] },
        },
      ],
    })
    expect(r.warnings).toEqual([])
  })
})

describe('validatePluginDeclaration — tools.render 与 ui 面', () => {
  it('tool 缺 name → error（上下文标注 ?name）', () => {
    const r = run({ tools: [{ render: { card: { kind: 'x' } } }] })
    expect(r.errors.some((e) => e.includes('tools[?name] 缺 name'))).toBe(true)
  })

  it('render 非对象 → warning（声明被弃）', () => {
    const r = run({ tools: [{ name: 't', render: 'card' }] })
    expect(r.warnings.some((w) => w.includes('render 非对象'))).toBe(true)
    expect(r.errors).toEqual([])
  })

  it('render 为 null 视同未声明（不告警）', () => {
    const r = run({ tools: [{ name: 't', render: null }] })
    expect(r.warnings).toEqual([])
  })

  it('render.card 对象缺 kind → warning', () => {
    const r = run({ tools: [{ name: 't', render: { card: {} } }] })
    expect(r.warnings.some((w) => w.includes('render.card 缺 kind'))).toBe(true)
  })

  it('render.card 带非空 kind → 无告警', () => {
    const r = run({ tools: [{ name: 't', render: { card: { kind: 'table' } } }] })
    expect(r.warnings).toEqual([])
    expect(r.errors).toEqual([])
  })

  it('render 无 card 字段 → 不触发 card 规则', () => {
    const r = run({ tools: [{ name: 't', render: { mode: 'auto' } }] })
    expect(r.warnings).toEqual([])
  })

  it('ui.chat_card 非对象 → error', () => {
    const r = run({ tools: [{ name: 't', ui: { chat_card: 'oops' } }] })
    expect(r.errors.some((e) => e.includes('ui.chat_card 非对象'))).toBe(true)
  })

  it.each(['interaction_modes', 'view_modes'])('ui.%s 非对象 → warning', (facet) => {
    const r = run({ tools: [{ name: 't', ui: { [facet]: 'plain' } }] })
    expect(r.warnings.some((w) => w.includes(`ui.${facet} 非对象`))).toBe(true)
  })

  it.each(['interaction_modes', 'view_modes'])('ui.%s 为对象时不告警', (facet) => {
    const r = run({ tools: [{ name: 't', ui: { [facet]: { a: 1 } } }] })
    expect(r.warnings).toEqual([])
  })

  it('ui 为 null/非对象时跳过整段（不抛错）', () => {
    expect(run({ tools: [{ name: 't', ui: null }] }).errors).toEqual([])
    expect(run({ tools: [{ name: 't', ui: 5 }] }).errors).toEqual([])
  })

  it('tools 含非对象条目 → warning 并继续', () => {
    const r = run({ tools: [null as never, { name: 'ok' }] })
    expect(r.warnings.some((w) => w.includes('tools 有非对象条目'))).toBe(true)
  })

  it('externalMcp 工具缺 input_schema → error；同工具补上后 errors 清空（对照）', () => {
    const withoutSchema = run({ externalMcp: true, tools: [{ name: 'search' }] })
    expect(withoutSchema.errors.some((e) => e.includes('缺 input_schema'))).toBe(true)
    const withSchema = run({
      externalMcp: true,
      tools: [{ name: 'search', input_schema: { type: 'object' } }],
    })
    expect(withSchema.errors).toEqual([])
  })

  it('externalMcp 工具名缺失时错误文案仍可读（? 占位）', () => {
    const r = run({ externalMcp: true, tools: [{}] })
    expect(r.errors.some((e) => e.includes('tools[name=?] external MCP 工具缺 input_schema'))).toBe(
      true,
    )
  })
})

describe('validatePluginDeclaration — chat_card 块规则', () => {
  const toolWithCard = (card: Record<string, unknown>) => ({
    tools: [{ name: 't', ui: { chat_card: card } }],
  })

  it('title 非字符串（空串）→ warning', () => {
    const r = run(toolWithCard({ title: '  ', blocks: [] }))
    expect(r.warnings.some((w) => w.includes('title 非字符串'))).toBe(true)
  })

  it('title 合法字符串不告警', () => {
    const r = run(toolWithCard({ title: '卡片', blocks: [] }))
    expect(r.warnings).toEqual([])
  })

  it('blocks 未声明 → 直接返回（title 规则外的块规则不触发）', () => {
    const r = run(toolWithCard({ title: '卡片' }))
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })

  it('blocks 非数组 → error', () => {
    const r = run(toolWithCard({ blocks: { type: 'text' } }))
    expect(r.errors.some((e) => e.includes('.blocks 应为数组'))).toBe(true)
  })

  it('blocks 含非对象条目 → error 且后续块继续校验', () => {
    const r = run(toolWithCard({ blocks: [0, { type: 'text', label: 'L' }] }))
    expect(r.errors.some((e) => e.includes('[0] 不是对象'))).toBe(true)
    expect(r.warnings).toEqual([])
  })

  it.each([
    'text',
    'code',
    'json',
    'markdown',
    'diff',
    'kv',
    'file',
    'image',
    'link',
    'log',
    'form',
  ])('合法块 type=%s 不报非法块错误', (type) => {
    const r = run(toolWithCard({ blocks: [{ type }] }))
    expect(r.errors.filter((e) => e.includes('非法块 type'))).toEqual([])
  })

  it('块 type 缺失/非字符串 → error 且跳过该块后续规则', () => {
    const r = run(toolWithCard({ blocks: [{ label: 'x' }, { type: 7 }] }))
    expect(r.errors.filter((e) => e.includes('非法块 type')).length).toBe(2)
  })

  it('text 块有 label 或 source 任一即不告警', () => {
    expect(run(toolWithCard({ blocks: [{ type: 'text', label: 'L' }] })).warnings).toEqual([])
    expect(run(toolWithCard({ blocks: [{ type: 'text', source: '{{a}}' }] })).warnings).toEqual([])
  })

  it('text 块 label/source 均为空白 → warning', () => {
    const r = run(toolWithCard({ blocks: [{ type: 'text', label: ' ', source: '  ' }] }))
    expect(r.warnings.some((w) => w.includes('text 块缺 label/source'))).toBe(true)
  })

  it('块级 source 模板配平/未配平两分支', () => {
    const balanced = run(toolWithCard({ blocks: [{ type: 'code', source: '{{a}}' }] }))
    expect(balanced.warnings.filter((w) => w.includes('source 模板括号未配平'))).toEqual([])

    const broken = run(toolWithCard({ blocks: [{ type: 'code', source: '{{a' }] }))
    expect(broken.warnings.some((w) => w.includes('source 模板括号未配平'))).toBe(true)
  })

  it('kv 块缺 fields → warning；fields 为数组则不告警', () => {
    const missing = run(toolWithCard({ blocks: [{ type: 'kv' }] }))
    expect(missing.warnings.some((w) => w.includes('kv 块缺 fields'))).toBe(true)

    const present = run(toolWithCard({ blocks: [{ type: 'kv', fields: [{ name: 'k', type: 'string' }] }] }))
    expect(present.warnings).toEqual([])
  })

  it('form 块内嵌字段走字段校验（缺 name → error）', () => {
    const r = run(
      toolWithCard({ blocks: [{ type: 'form', form: { fields: [{ type: 'string' }] } }] }),
    )
    expect(r.errors.some((e) => e.includes('blocks[0].form.fields[0] 缺 name'))).toBe(true)
  })

  it('form 块无 form 字段时不抛错（可选链缺省）', () => {
    expect(run(toolWithCard({ blocks: [{ type: 'form' }] })).errors).toEqual([])
  })

  it.each(['code', 'json', 'markdown', 'file', 'image', 'link', 'log'])(
    '%s 内容块缺 content/source/diffOld → warning',
    (type) => {
      const r = run(toolWithCard({ blocks: [{ type }] }))
      expect(r.warnings.some((w) => w.includes(`${type} 块缺 content/source`))).toBe(true)
    },
  )

  it.each(['code', 'json', 'markdown', 'file', 'image', 'link', 'log'])(
    '%s 内容块带 content → 不告警',
    (type) => {
      const r = run(toolWithCard({ blocks: [{ type, content: 'payload' }] }))
      expect(r.warnings).toEqual([])
    },
  )

  it('diff 块用 diffOld 数组满足内容检查（diff 未列入内容块清单，故无告警）', () => {
    const r = run(toolWithCard({ blocks: [{ type: 'diff', diffOld: ['a'] }] }))
    expect(r.warnings).toEqual([])
  })

  it('diff 块无任何内容键也不告警（diff 不在内容块清单内）', () => {
    const r = run(toolWithCard({ blocks: [{ type: 'diff' }] }))
    expect(r.warnings).toEqual([])
    expect(r.errors).toEqual([])
  })
})

describe('validatePluginDeclaration — ui_schema.widgets 校验', () => {
  it('widget 非对象 → error 且跳过后续规则', () => {
    const r = run({ uiSchemaWidgets: ['x' as never] })
    expect(r.errors.some((e) => e.includes('非对象'))).toBe(true)
  })

  it('widget 缺 type → error（无法路由渲染）', () => {
    const r = run({ uiSchemaWidgets: [{ id: 'w1' }] })
    expect(r.errors.some((e) => e.includes('缺 type'))).toBe(true)
  })

  it('widget 合法 id/type → 无错误', () => {
    const r = run({ uiSchemaWidgets: [{ id: 'w1', type: 'form' }] })
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })

  it('widget 自定义 space（任意字符串）合法——扩展 space 不报错', () => {
    const r = run({ uiSchemaWidgets: [{ id: 'w1', type: 'form', space: 'my-custom-space' }] })
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })

  it('widget space 非字符串（空串）→ warning', () => {
    const r = run({ uiSchemaWidgets: [{ id: 'w1', type: 'form', space: '' }] })
    expect(r.warnings.some((w) => w.includes('space 非字符串'))).toBe(true)
  })

  it('widget props.fields 中的坏字段 → error（上下文含 widgets 下标）', () => {
    const r = run({
      uiSchemaWidgets: [{ id: 'w1', type: 'form', props: { fields: [{ type: 'string' }] } }],
    })
    expect(r.errors.some((e) => e.includes('ui_schema.widgets[0].props.fields[0] 缺 name'))).toBe(
      true,
    )
  })

  it('两个 widget 中仅第二个坏 → 错误下标精确指向 [1]', () => {
    const r = run({ uiSchemaWidgets: [{ id: 'w1', type: 'form' }, { type: 'form' }] })
    expect(r.errors.some((e) => e.includes('ui_schema.widgets[1] 缺 id'))).toBe(true)
    expect(r.errors.some((e) => e.includes('ui_schema.widgets[0] 缺'))).toBe(false)
  })
})

describe('validatePluginDeclaration — capabilities.streaming 校验', () => {
  it('streaming 为 null 视同未声明（无错误）', () => {
    expect(run({ streaming: null as never }).errors).toEqual([])
  })

  it('streaming 为字符串（真非对象）→ error', () => {
    const r = run({ streaming: 'yes' as never })
    expect(r.errors.some((e) => e.includes('capabilities.streaming 非对象'))).toBe(true)
  })

  it('streaming 为数组时未被拦下（typeof [] === object；子键均缺省故无进一步校验）', () => {
    // 现状契约：数组形状的 streaming 声明不报错也不校验子键（见回报中的疑似缺陷）
    const r = run({ streaming: [] as never })
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })

  it('streaming 只有 persist 布尔 → 无错误', () => {
    expect(run({ streaming: { persist: true } }).errors).toEqual([])
  })

  it('events 未声明时跳过事件校验', () => {
    expect(run({ streaming: { persist: false } }).errors).toEqual([])
  })

  it.each([
    'block_start',
    'text_delta',
    'reasoning_delta',
    'tool_call_delta',
    'block_end',
    'usage',
    'finish',
    'keepalive',
    'stream_start',
    'tool_start',
    'tool_result',
    'new_message',
    'stream_end',
    'stream_error',
  ])('契约事件 %s 合法', (event) => {
    expect(run({ streaming: { events: [event] } }).errors).toEqual([])
  })

  it('events 含非字符串（数字）→ error 并点名下标', () => {
    const r = run({ streaming: { events: [42] } })
    expect(r.errors.some((e) => e.includes('events[0] 未知事件'))).toBe(true)
  })

  it('persist 未声明时跳过布尔校验', () => {
    expect(run({ streaming: { events: ['stream_start'] } }).errors).toEqual([])
  })

  it('events 非数组（字符串）→ error', () => {
    const r = run({ streaming: { events: 'stream_start' } })
    expect(r.errors.some((e) => e.includes('events 应为数组'))).toBe(true)
  })

  it('persist 非布尔（字符串/数字）→ error', () => {
    expect(run({ streaming: { persist: 'yes' } }).errors.some((e) => e.includes('persist 应为布尔'))).toBe(true)
    expect(run({ streaming: { persist: 1 } }).errors.some((e) => e.includes('persist 应为布尔'))).toBe(true)
    expect(run({ streaming: { persist: false } }).errors).toEqual([])
  })

  it('part_types 未声明时跳过校验', () => {
    expect(run({ streaming: { events: ['stream_start'] } }).errors).toEqual([])
  })

  it('part_types 非数组 → error', () => {
    const r = run({ streaming: { part_types: 'progress_card' } })
    expect(r.errors.some((e) => e.includes('part_types 应为数组'))).toBe(true)
  })

  it('part_types 合法命名（小写+数字+下划线）无告警', () => {
    const r = run({ streaming: { part_types: ['progress_card', 'k8s_pod'] } })
    expect(r.warnings).toEqual([])
    expect(r.errors).toEqual([])
  })

  it.each(['Progress', 'with-dash', 'with space', 'a'.repeat(33), ''])(
    'part_types 非法命名 %j → 报错或告警（不静默）',
    (pt) => {
      const r = run({ streaming: { part_types: [pt] } })
      expect(r.errors.length + r.warnings.length).toBeGreaterThan(0)
    },
  )
})
