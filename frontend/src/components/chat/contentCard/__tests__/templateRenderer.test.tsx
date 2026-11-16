/** @feature FP-RP面板v3.1刀5 | @ci: frontend-test */
/**
 * templateRenderer — 声明式模板解释器测试（ADR §4：剥离+textContent 注入安全模型）。
 */
import { describe, expect, it } from 'vitest'
import { buildCsvTable, renderTemplate } from '../templateRenderer'

describe('renderTemplate — 声明面剥离（纵深防御）', () => {
  it('script/iframe/object/embed/link/meta 整元素剥离', () => {
    const frag = renderTemplate(
      '<div>ok</div><script>alert(1)</script><iframe src="x"></iframe><link rel="x" href="y"><meta charset="utf-8">',
      '内容',
      null,
    )
    const div = document.createElement('div')
    div.appendChild(frag)
    expect(div.querySelector('div')?.textContent).toBe('ok')
    expect(div.querySelector('script')).toBeNull()
    expect(div.querySelector('iframe')).toBeNull()
    expect(div.querySelector('link')).toBeNull()
    expect(div.querySelector('meta')).toBeNull()
  })

  it('on* 事件属性剥离（onclick/onerror…），其余属性保留', () => {
    const frag = renderTemplate(
      '<div class="card" onclick="evil()" onerror="evil()">x</div>',
      '内容',
      null,
    )
    const div = document.createElement('div')
    div.appendChild(frag)
    const el = div.querySelector('div')!
    expect(el.getAttribute('class')).toBe('card')
    expect(el.getAttribute('onclick')).toBeNull()
    expect(el.getAttribute('onerror')).toBeNull()
  })
})

describe('renderTemplate — 指令展开（textContent 注入安全面）', () => {
  it('{{content}} 注入 fence 原文（含 HTML 标签也按文本不解析——不可信内容不走 innerHTML）', () => {
    const frag = renderTemplate('<p data-slot="c">{{content}}</p>', '<img src=x onerror=evil()> 剧情文本', null)
    const div = document.createElement('div')
    div.appendChild(frag)
    const slot = div.querySelector('[data-slot="c"]')!
    expect(slot.querySelector('img')).toBeNull()
    expect(slot.textContent).toBe('<img src=x onerror=evil()> 剧情文本')
  })

  it('{{csv_table}} → table DOM（首行表头；单元格 textContent 天然转义）', () => {
    const frag = renderTemplate('{{csv_table}}', '', [
      ['名', '备注'],
      ['a,b', '<script>alert(1)</script>'],
    ])
    const div = document.createElement('div')
    div.appendChild(frag)
    const table = div.querySelector('table')!
    expect(table.querySelectorAll('thead th')).toHaveLength(2)
    expect(table.querySelector('thead th')?.textContent).toBe('名')
    const cells = Array.from(table.querySelectorAll('tbody td'))
    expect(cells.map((c) => c.textContent)).toEqual(['a,b', '<script>alert(1)</script>'])
    expect(cells[1]!.querySelector('script')).toBeNull()
  })

  it('csv=null 时 {{csv_table}} 指令缺席展开（零渲染，诚实缺省）', () => {
    const frag = renderTemplate('<div>A{{csv_table}}B</div>', '', null)
    const div = document.createElement('div')
    div.appendChild(frag)
    expect(div.querySelector('div')!.textContent).toBe('AB')
    expect(div.querySelector('table')).toBeNull()
  })

  it('模板无指令 → 原样装配（content 参数不注入）', () => {
    const frag = renderTemplate('<section>静态</section>', '数据', null)
    const div = document.createElement('div')
    div.appendChild(frag)
    expect(div.querySelector('section')!.textContent).toBe('静态')
  })
})

describe('buildCsvTable — 表格构建', () => {
  it('空行数组 → 空 table；只有表头行 → 无 tbody 行', () => {
    expect(buildCsvTable([]).querySelectorAll('tr')).toHaveLength(0)
    const headerOnly = buildCsvTable([['h1', 'h2']])
    expect(headerOnly.querySelectorAll('thead th')).toHaveLength(2)
    expect(headerOnly.querySelectorAll('tbody tr')).toHaveLength(0)
  })

  it('多行体按序产出（性质断言：行数=输入体行数）', () => {
    const rows = [
      ['h'],
      ['r1'],
      ['r2'],
      ['r3'],
    ]
    expect(buildCsvTable(rows).querySelectorAll('tbody tr')).toHaveLength(3)
  })
})

describe('{{content_text}}（HTML 去标签纯文本，2026-10-25 任务卡双形态统一）', () => {
  it('li/块级闭合补换行、实体解码、标签剥离、空白收敛', () => {
    const html = '<details><summary>📋 任务清单 3/7</summary><ul>' +
      '<li style="color:red;">☑ 三线恢复执行 &amp; 补台账</li>' +
      '<li><span>🔄</span>步骤 1b 收尾</li>' +
      '<li><span>☐</span>剩余项</li></ul></details>'
    const frag = renderTemplate('<pre>{{content_text}}</pre>', html, null)
    const pre = frag.querySelector('pre')!
    const lines = (pre.textContent ?? '').split('\n').filter((l) => l.trim())
    expect(lines).toEqual(['📋 任务清单 3/7', '☑ 三线恢复执行 & 补台账', '🔄 步骤 1b 收尾', '☐ 剩余项'])
  })

  it('纯文本内容原样透传（非 HTML 输入零损伤）', () => {
    const frag = renderTemplate('<pre>{{content_text}}</pre>', '- [ ] 甲\n- [x] 乙', null)
    expect(frag.querySelector('pre')!.textContent).toBe('- [ ] 甲\n- [x] 乙')
  })

  it('与 {{content}} 同模板共存展开', () => {
    const frag = renderTemplate('<b>{{content}}</b><i>{{content_text}}</i>', '<p>正文</p>', null)
    expect(frag.querySelector('b')!.textContent).toBe('<p>正文</p>')
    expect(frag.querySelector('i')!.textContent).toBe('正文')
  })
})
