/**
 * SchemaParser 0.2 扩展测试
 *
 * 覆盖 AC-11-1: SchemaDriver 引擎可解析新的 ui 字段（input_form/result_widget）
 * 覆盖 AC-11-2: 支持 ui_contributions（动态创建面板/快捷按钮/右键菜单）
 * 覆盖 AC-11-5: 0.1 的 ui_schema 配置继续可用（向后兼容）
 * 覆盖 AC-11-6: scene 渲染空间
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { SchemaParser } from '@/services/schema/SchemaParser'
import type { ModuleUISchema } from '@/types/schema'

/** 创建有效的 0.2 Schema（含 ui 字段） */
function createV2Schema(overrides?: Partial<ModuleUISchema>): ModuleUISchema {
  return {
    identity: {
      id: 'plugin-doc-review',
      name: 'Document Review',
      version: '2.0.0',
      category: 'extension',
      description: 'A document review plugin',
      icon: '📄',
    },
    actions: [
      { id: 'review', name: '审阅', type: 'command' },
    ],
    rendering: {
      chat: [],
      spaces: [],
    },
    clients: {
      requiredSpaces: ['workspace', 'dock'],
      requiredWidgets: [],
    },
    ui: {
      inputForm: {
        fields: [
          {
            name: 'document',
            type: 'file',
            label: '文档',
            required: true,
          },
          {
            name: 'category',
            type: 'select',
            label: '类别',
            datasourceUri: 'datasource://categories/list',
            options: [
              { label: '技术', value: 'tech' },
              { label: '法律', value: 'legal' },
            ],
          },
        ],
        submitLabel: '提交审阅',
      },
      resultWidget: {
        type: 'review_document',
        renderSpace: 'workspace',
        props: { showDiff: true },
      },
    },
    ui_contributions: [
      {
        type: 'widget',
        widgetType: 'review_document',
        renderSpace: 'workspace',
        label: '文档审阅',
        icon: '📄',
      },
      {
        type: 'panel',
        widgetType: 'review_toolbar',
        renderSpace: 'dock',
        label: '审阅工具栏',
      },
      {
        type: 'shortcut',
        widgetType: 'quick_review',
        renderSpace: 'dock',
        label: '快速审阅',
        order: 1,
      },
    ],
    ...overrides,
  }
}

/** 创建 0.1 向后兼容 Schema（只有 ui_schema，无 ui/ui_contributions） */
function createV1CompatSchema(): ModuleUISchema {
  return {
    identity: {
      id: 'legacy-module',
      name: 'Legacy Module',
      version: '1.0.0',
      category: 'builtin',
    },
    actions: [
      { id: 'run', name: '运行', type: 'command' },
    ],
    rendering: {
      chat: [
        { type: 'form' },
      ],
      spaces: [
        { space: 'workspace', widget: 'table' },
      ],
    },
    clients: {
      requiredSpaces: ['chat', 'workspace'],
      requiredWidgets: ['form'],
    },
    // 0.1 风格的 ui_schema
    ui_schema: {
      form: {
        fields: [
          { name: 'query', type: 'string', label: '查询' },
        ],
      },
    },
  }
}

describe('SchemaParser 0.2 — AC-11-1: 解析新 ui 字段', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('应正确解析 ui.input_form', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    expect(parsed.ui).toBeDefined()
    expect(parsed.ui?.inputForm).toBeDefined()
    expect(parsed.ui?.inputForm?.fields).toHaveLength(2)
    expect(parsed.ui?.inputForm?.fields[0].name).toBe('document')
    expect(parsed.ui?.inputForm?.fields[0].type).toBe('file')
    expect(parsed.ui?.inputForm?.fields[1].datasourceUri).toBe('datasource://categories/list')
    expect(parsed.ui?.inputForm?.submitLabel).toBe('提交审阅')
  })

  it('应正确解析 ui.result_widget', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    expect(parsed.ui?.resultWidget).toBeDefined()
    expect(parsed.ui?.resultWidget?.type).toBe('review_document')
    expect(parsed.ui?.resultWidget?.renderSpace).toBe('workspace')
    expect(parsed.ui?.resultWidget?.props).toEqual({ showDiff: true })
  })

  it('ui 字段缺失时 parsed.ui 应为 undefined', () => {
    const schema = createV1CompatSchema()
    const { parsed } = parser.parse(schema)

    expect(parsed.ui).toBeUndefined()
  })
})

describe('SchemaParser 0.2 — AC-11-2: 解析 ui_contributions', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('应正确解析 ui_contributions 列表', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    expect(parsed.ui_contributions).toBeDefined()
    expect(parsed.ui_contributions).toHaveLength(3)
  })

  it('ui_contributions 中 widget 类型贡献项正确解析', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    const widgetContrib = parsed.ui_contributions?.find((c) => c.type === 'widget')
    expect(widgetContrib).toBeDefined()
    expect(widgetContrib?.widgetType).toBe('review_document')
    expect(widgetContrib?.renderSpace).toBe('workspace')
    expect(widgetContrib?.label).toBe('文档审阅')
  })

  it('ui_contributions 中 panel 类型贡献项正确解析', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    const panelContrib = parsed.ui_contributions?.find((c) => c.type === 'panel')
    expect(panelContrib).toBeDefined()
    expect(panelContrib?.widgetType).toBe('review_toolbar')
    expect(panelContrib?.renderSpace).toBe('dock')
  })

  it('ui_contributions 中 shortcut 类型贡献项正确解析', () => {
    const schema = createV2Schema()
    const { parsed } = parser.parse(schema)

    const shortcutContrib = parsed.ui_contributions?.find((c) => c.type === 'shortcut')
    expect(shortcutContrib).toBeDefined()
    expect(shortcutContrib?.widgetType).toBe('quick_review')
    expect(shortcutContrib?.order).toBe(1)
  })

  it('ui_contributions 缺失时 parsed.ui_contributions 应为 undefined', () => {
    const schema = createV1CompatSchema()
    const { parsed } = parser.parse(schema)

    expect(parsed.ui_contributions).toBeUndefined()
  })
})

describe('SchemaParser 0.2 — AC-11-5: 0.1 ui_schema 向后兼容', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('0.1 ui_schema 存在时解析不报错', () => {
    const schema = createV1CompatSchema()
    expect(() => parser.parse(schema)).not.toThrow()
  })

  it('0.1 schema 验证通过', () => {
    const schema = createV1CompatSchema()
    const result = parser.validate(schema)
    expect(result.valid).toBe(true)
  })

  it('0.1 rendering.chat 和 rendering.spaces 正常解析', () => {
    const schema = createV1CompatSchema()
    const { parsed } = parser.parse(schema)

    expect(parsed.rendering.chat).toHaveLength(1)
    expect(parsed.rendering.chat[0].type).toBe('form')
    expect(parsed.rendering.spaces).toHaveLength(1)
    expect(parsed.rendering.spaces[0].space).toBe('workspace')
  })
})

describe('SchemaParser 0.2 — AC-11-6: scene 渲染空间支持', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('应正确解析 renderSpace 为 scene 的 ui_contributions', () => {
    const schema = createV2Schema({
      ui_contributions: [
        {
          type: 'widget',
          widgetType: 'digital_human',
          renderSpace: 'scene',
          label: '数字人形象',
        },
      ],
    })
    const { parsed } = parser.parse(schema)

    expect(parsed.ui_contributions).toHaveLength(1)
    expect(parsed.ui_contributions?.[0].renderSpace).toBe('scene')
  })
})

describe('SchemaParser 0.2 — 增量更新支持新字段', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('相同 ui 字段的 Schema 增量更新返回 changed=false', () => {
    const schema = createV2Schema()
    const { changed: first } = parser.parse(schema)
    const { changed: second } = parser.parse(schema)

    expect(first).toBe(true)
    expect(second).toBe(false)
  })

  it('修改 ui_contributions 后增量更新返回 changed=true', () => {
    const schema1 = createV2Schema()
    parser.parse(schema1)

    const schema2 = createV2Schema({
      ui_contributions: [
        ...schema1.ui_contributions!,
        {
          type: 'context_menu',
          widgetType: 'context_action',
          renderSpace: 'workspace',
          label: '右键操作',
        },
      ],
    })
    const { changed } = parser.parse(schema2)

    expect(changed).toBe(true)
  })
})
