/**
 * chat_card form 块 + actions on_click 协议测试（widget 化 T2/T3）
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import {
  interpretChatCard,
  type ChatCardDeclaration,
  type ToolCallContext,
} from '@/utils/chatCardInterpreter'
import {
  registerGlobalImagePreviewCallback,
  registerGlobalOpenFileCallback,
} from '@/utils/toolCardRegistry'

const ctx = (over: Partial<ToolCallContext> = {}): ToolCallContext => ({
  args: { path: '/tmp/a.txt', content: 'hello' },
  result: { status: 'ok' },
  ...over,
})

beforeEach(() => {
  registerGlobalOpenFileCallback(null as never)
  registerGlobalImagePreviewCallback(null)
  vi.restoreAllMocks()
})

afterEach(() => {
  // open_url 测试经 stubGlobal 换 window.open——不 unstub 会泄漏到后续用例，
  // 污染其他用例 spy 的调用历史（负断言 not.toHaveBeenCalled 误报）
  vi.unstubAllGlobals()
})

describe('T2：form 块翻译', () => {
  const decl: ChatCardDeclaration = {
    title: '部署确认',
    blocks: [
      {
        type: 'form',
        label: '部署参数',
        form: {
          fields: [
            { name: 'env', type: 'select', label: '环境', options: [{ label: '生产', value: 'prod' }] },
            { name: 'replicas', type: 'number', label: '副本数' },
          ],
          endpoint: '/ext/deploy/confirm',
          submitLabel: '确认部署',
          valuesSource: 'result.form_values',
        },
      },
    ],
  }

  it('form 声明 → contentType=form 块（formFields/endpoint/values 透传）', () => {
    const out = interpretChatCard(decl, ctx({ result: { form_values: { env: 'prod', replicas: 3 } } }))
    expect(out.details).toHaveLength(1)
    const block = out.details[0]
    expect(block.contentType).toBe('form')
    expect(block.content).toMatchObject({
      endpoint: '/ext/deploy/confirm',
      submitLabel: '确认部署',
      values: { env: 'prod', replicas: 3 },
    })
    expect((block.content as Record<string, unknown>).formFields).toHaveLength(2)
  })

  it('无 form 声明 / fields 空数组 → 块不渲染', () => {
    expect(interpretChatCard({ blocks: [{ type: 'form', label: 'x' }] }, ctx()).details).toHaveLength(0)
    expect(
      interpretChatCard({ blocks: [{ type: 'form', form: { fields: [] } }] }, ctx()).details,
    ).toHaveLength(0)
  })

  it('valuesSource 求值非对象（标量/缺失）→ values 缺省', () => {
    const out = interpretChatCard(decl, ctx({ result: { form_values: 'oops' } }))
    expect((out.details[0].content as Record<string, unknown>).values).toBeUndefined()
    const out2 = interpretChatCard(decl, ctx())
    expect((out2.details[0].content as Record<string, unknown>).values).toBeUndefined()
  })
})

describe('T3：actions on_click 协议接线', () => {
  it('open_file：value 模板渲染 → 全局文件打开回调（container_task_id 一并透传）', () => {
    const openFile = vi.fn()
    registerGlobalOpenFileCallback(openFile)
    const out = interpretChatCard(
      { actions: [{ id: 'a', label: '打开', onClick: { action: 'open_file', value: '{{args.path}}' } }] },
      ctx(),
    )
    expect(out.actions[0].disabled).toBeFalsy()
    out.actions[0].onClick!()
    expect(openFile).toHaveBeenCalledWith('/tmp/a.txt', undefined)
  })

  it('open_file：ctx 带 container_task_id 时透传给回调（任务工作空间定位）', () => {
    const openFile = vi.fn()
    registerGlobalOpenFileCallback(openFile)
    const out = interpretChatCard(
      { actions: [{ id: 'a', label: '打开', onClick: { action: 'open_file', value: '{{args.path}}' } }] },
      { ...ctx(), container_task_id: 'task-7' },
    )
    out.actions[0].onClick!()
    expect(openFile).toHaveBeenCalledWith('/tmp/a.txt', 'task-7')
  })

  it('open_url：新标签打开', () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const out = interpretChatCard(
      { actions: [{ id: 'a', label: '文档', onClick: { action: 'open_url', value: '{{result.url}}' } }] },
      ctx({ result: { url: 'https://example.com' } }),
    )
    out.actions[0].onClick!()
    expect(open).toHaveBeenCalledWith('https://example.com', '_blank', 'noopener,noreferrer')
  })

  it('preview_image：全局图片预览回调', () => {
    const preview = vi.fn()
    registerGlobalImagePreviewCallback(preview)
    const out = interpretChatCard(
      { actions: [{ id: 'a', label: '预览', onClick: { action: 'preview_image', value: '/x.png' } }] },
      ctx(),
    )
    out.actions[0].onClick!()
    expect(preview).toHaveBeenCalledWith('/x.png')
  })

  it('copy：写剪贴板 + toast', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const out = interpretChatCard(
      { actions: [{ id: 'a', label: '复制', onClick: { action: 'copy', value: '{{result.stdout}}' } }] },
      ctx({ result: { stdout: 'line1\nline2' } }),
    )
    await out.actions[0].onClick!()
    expect(writeText).toHaveBeenCalledWith('line1\nline2')
  })

  it('run_action：commandDispatcher.executeCommand（value=命令 id，args 透传；仅限已声明命令）', () => {
    const exec = vi.spyOn(commandDispatcher, 'executeCommand').mockResolvedValue()
    contributionRegistry.register({ type: 'commands', id: 'deploy.restart', title: '重启' })
    const out = interpretChatCard(
      {
        actions: [
          {
            id: 'a',
            label: '执行',
            onClick: { action: 'run_action', value: 'deploy.restart', args: { svc: 'api' } },
          },
        ],
      },
      ctx(),
    )
    out.actions[0].onClick!()
    expect(exec).toHaveBeenCalledWith('deploy.restart', { svc: 'api' })
  })

  it('未声明 on_click / 未知协议 / value 求值缺失 → 按钮禁用不抛错', () => {
    const out = interpretChatCard(
      {
        actions: [
          { id: 'a', label: '无协议' },
          { id: 'b', label: '未知协议', onClick: { action: 'magic', value: 'x' } },
          { id: 'c', label: 'value 缺失', onClick: { action: 'open_file', value: '{{args.missing}}' } },
        ],
      },
      ctx(),
    )
    for (const a of out.actions) {
      expect(a.disabled).toBe(true)
      expect(a.onClick).toBeUndefined()
    }
  })

  it('confirm 文案透传 confirmMessage', () => {
    const out = interpretChatCard(
      {
        actions: [
          { id: 'a', label: '危险', onClick: { action: 'copy', value: 'x', confirm: '确认复制？' } },
        ],
      },
      ctx(),
    )
    expect(out.actions[0].confirmMessage).toBe('确认复制？')
  })
})

describe('U9：on_click 白名单（open_url 协议 / run_action 声明命令）', () => {
  it('open_url：http/https 绝对地址与应用内相对路径放行，点击 window.open 原样打开', () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const urls = [
      'https://example.com/docs?page=1',
      'http://localhost:5173/settings',
      '/workspace/files/a.txt',
      './relative/page',
    ]
    const out = interpretChatCard(
      {
        actions: urls.map((u, i) => ({ id: `u${i}`, label: u, onClick: { action: 'open_url', value: u } })),
      },
      ctx(),
    )
    urls.forEach((u, i) => {
      expect(out.actions[i].disabled).toBe(false)
      out.actions[i].onClick!()
      expect(openSpy).toHaveBeenCalledWith(u, '_blank', 'noopener,noreferrer')
    })
  })

  it('open_url：javascript:/data:/file:/vbscript: 等危险协议一律拒绝（按钮禁用，window.open 不触发；协议大小写不豁免）', () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const urls = [
      'javascript:alert(1)',
      'JaVaScRiPt:alert(2)',
      'data:text/html;base64,PHNjcmlwdD4=',
      'file:///C:/Windows/System32',
      'vbscript:msgbox(1)',
    ]
    const out = interpretChatCard(
      {
        actions: urls.map((u, i) => ({ id: `u${i}`, label: u, onClick: { action: 'open_url', value: u } })),
      },
      ctx(),
    )
    for (const [i, a] of out.actions.entries()) {
      expect(a.disabled, urls[i]).toBe(true)
      expect(a.onClick, urls[i]).toBeUndefined()
    }
    expect(openSpy).not.toHaveBeenCalled()
  })

  it('run_action：contributionRegistry 已声明的命令放行（多命令逐一执行）', () => {
    const exec = vi.spyOn(commandDispatcher, 'executeCommand').mockResolvedValue()
    contributionRegistry.register({ type: 'commands', id: 'cost.showReport', title: '成本报告' })
    contributionRegistry.register({ type: 'commands', id: 'deploy.restart', title: '重启' })
    const out = interpretChatCard(
      {
        actions: [
          { id: 'a', label: '报告', onClick: { action: 'run_action', value: 'cost.showReport', args: { range: '7d' } } },
          { id: 'b', label: '重启', onClick: { action: 'run_action', value: 'deploy.restart' } },
        ],
      },
      ctx(),
    )
    expect(out.actions[0].disabled).toBe(false)
    out.actions[0].onClick!()
    expect(exec).toHaveBeenCalledWith('cost.showReport', { range: '7d' })
    expect(out.actions[1].disabled).toBe(false)
    out.actions[1].onClick!()
    expect(exec).toHaveBeenCalledWith('deploy.restart', undefined)
  })

  it('run_action：未声明命令拒绝（按钮禁用 + console.warn），executeCommand 不被调用', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const exec = vi.spyOn(commandDispatcher, 'executeCommand').mockResolvedValue()
    // 同卡片混排：已声明 + 未声明，只有未声明被拒
    contributionRegistry.register({ type: 'commands', id: 'deploy.restart', title: '重启' })
    const out = interpretChatCard(
      {
        actions: [
          { id: 'a', label: '重启', onClick: { action: 'run_action', value: 'deploy.restart' } },
          { id: 'b', label: '幽灵命令', onClick: { action: 'run_action', value: 'ghost.notDeclared' } },
        ],
      },
      ctx(),
    )
    expect(out.actions[0].disabled).toBe(false)
    expect(out.actions[1].disabled).toBe(true)
    expect(out.actions[1].onClick).toBeUndefined()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('ghost.notDeclared'))
    expect(exec).not.toHaveBeenCalledWith('ghost.notDeclared', undefined)
  })
})
