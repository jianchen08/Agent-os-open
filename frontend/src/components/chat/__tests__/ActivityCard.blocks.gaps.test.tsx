// @feature: FP-T12 ActivityCard blocks 补测 | @ci: frontend-test
/**
 * ActivityCard 区块补测：状态主题/类型图标、file/image/link/log/kv/json 块、
 * 原生卡片块其余分支、partialOutput、actions 确认弹窗双路径。
 *
 * 断行为：用户可见的渲染产物与点击交互（打开文件回调、剪贴板、灯箱、
 * 确认弹窗的取消/确认两条路径），不断言内部实现。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import type { ActivityData, ActivityType } from '@/types/activity'

const { openFileSpy } = vi.hoisted(() => ({ openFileSpy: vi.fn() }))

vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  getGlobalOpenFileCallback: () => openFileSpy,
}))

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-gaps',
    title: 'gaps 工具调用',
    status: 'completed',
    ...overrides,
  }
}

function renderExpanded(activity: ActivityData) {
  return render(<ActivityCard activity={activity} defaultExpanded />)
}

beforeEach(() => {
  openFileSpy.mockReset()
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  })
})

describe('状态主题与类型图标', () => {
  it('running + 自定义颜色：呼吸条采用自定义色（阻塞型工具配色链路）', () => {
    const { container } = renderExpanded(
      makeActivity({ status: 'running', customColor: '#ff8800' }),
    )
    // 运行中卡片可见且带自绘呼吸动画条；标题正常渲染
    expect(screen.getByText('gaps 工具调用')).toBeInTheDocument()
    expect(container.querySelector('[data-activity-status="running"]')).toBeInTheDocument()
    expect(container.querySelector('span[aria-hidden="true"]')).toHaveStyle({
      backgroundColor: '#ff8800',
    })
  })

  it('running 未带自定义颜色：走默认主题色，不抛错', () => {
    const { container } = renderExpanded(makeActivity({ status: 'running' }))
    expect(container.querySelector('[data-activity-status="running"]')).toBeInTheDocument()
  })

  it.each([
    ['task_created', '建任务'],
    ['task_phase', '阶段推进'],
    ['task_completed', '任务完成'],
    ['task_failed', '任务失败'],
    ['agent_thinking', '思考中'],
    ['custom', '自定义活动'],
  ] as [ActivityType, string][])('类型 %s 渲染专属类型图标且卡片可见', (type, title) => {
    renderExpanded(makeActivity({ type, title, id: `act-${type}` }))
    expect(screen.getByText(title)).toBeInTheDocument()
    expect(screen.getByText(title).closest('[data-activity-type]')).toHaveAttribute(
      'data-activity-type',
      type,
    )
  })
})

describe('code/log 块复制按钮', () => {
  it('code 块点击复制 → 剪贴板写入代码原文', async () => {
    renderExpanded(
      makeActivity({
        details: [
          { id: 'c1', label: '脚本', contentType: 'code', content: 'print("hi")', language: 'python' },
        ],
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: '复制内容' }))
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('print("hi")')
    })
  })

  it('log 块渲染日志正文，复制按钮写入完整日志', async () => {
    renderExpanded(
      makeActivity({
        details: [
          { id: 'l1', label: '构建日志', contentType: 'log', content: 'step1 ok\nstep2 ok' },
        ],
      }),
    )
    const logPre = screen.getByText((_, el) => el?.tagName === 'PRE' && el.textContent === 'step1 ok\nstep2 ok')
    expect(logPre).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制内容' }))
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('step1 ok\nstep2 ok')
    })
  })
})

describe('file 块：文件行渲染 + 点击打开回调', () => {
  it('POSIX 路径：显示文件名与全路径，点击调用全局打开回调', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'f1', label: '产物', contentType: 'file', path: '/app/src/main.py' }],
      }),
    )
    expect(screen.getByText('main.py')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('点击打开文件: /app/src/main.py'))
    expect(openFileSpy).toHaveBeenCalledWith('/app/src/main.py')
  })

  it('Windows 反斜杠路径：取末段为文件名，点击回调整样生效', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'f2', label: '报告', contentType: 'file', path: 'D:\\proj\\report.docx' }],
      }),
    )
    expect(screen.getByText('report.docx')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('点击打开文件: D:\\proj\\report.docx'))
    expect(openFileSpy).toHaveBeenCalledWith('D:\\proj\\report.docx')
  })
})

describe('image 块：灯箱预览 + 加载失败降级', () => {
  it('点击缩略图开灯箱，点击遮罩关闭', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'i1', label: '截图', contentType: 'image', path: 'https://example.com/pic.png' }],
      }),
    )
    expect(screen.getByAltText('预览图')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '图片预览' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByAltText('预览图'))
    expect(screen.getByRole('dialog', { name: '图片预览' })).toBeInTheDocument()
    expect(screen.getByAltText('大图预览')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('dialog', { name: '图片预览' }))
    expect(screen.queryByRole('dialog', { name: '图片预览' })).not.toBeInTheDocument()
  })

  it('加载失败（onError）→ 降级为文件行（可点击打开）', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'i2', label: '坏图', contentType: 'image', path: '/tmp/broken.png' }],
      }),
    )
    fireEvent.error(screen.getByAltText('预览图'))
    // 降级后的文件行：文件名 + 打开提示
    expect(screen.getByText('broken.png')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('点击打开文件: /tmp/broken.png'))
    expect(openFileSpy).toHaveBeenCalledWith('/tmp/broken.png')
  })
})

describe('link 块：http(s) 白名单', () => {
  it('http 链接可点击（href 保留）', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'a1', label: '文档', contentType: 'link', url: 'https://example.com/docs' }],
      }),
    )
    const anchor = screen.getByTitle('https://example.com/docs')
    expect(anchor).toHaveAttribute('href', 'https://example.com/docs')
    expect(anchor).toHaveAttribute('target', '_blank')
  })

  it('非 http(s) 协议降级为纯文本（不可点击）', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'a2', label: '本地', contentType: 'link', url: 'file:///etc/hosts' }],
      }),
    )
    expect(screen.getByTitle('file:///etc/hosts')).not.toHaveAttribute('href')
  })
})

describe('form 交互块：点击阻断冒泡', () => {
  it('点击表单字段不误触卡片折叠，表单保持可用', () => {
    const { container } = render(
      <ActivityCard
        activity={makeActivity({
          details: [
            {
              id: 'fm1',
              label: '部署参数',
              contentType: 'form',
              content: {
                formFields: [{ name: 'replicas', type: 'number', label: '副本数' }],
                endpoint: '/ext/deploy',
              },
            },
          ],
        })}
      />,
    )
    fireEvent.click(screen.getByText('gaps 工具调用'))
    expect(screen.getByLabelText('副本数')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('副本数'))
    // 冒泡被阻断：卡片仍展开、表单仍在
    expect(screen.getByLabelText('副本数')).toBeInTheDocument()
    expect(container.querySelector('[data-activity-id="act-gaps"]')).toBeInTheDocument()
  })
})

describe('kv / json 块', () => {
  it('kv 块两列渲染键值对', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'kv1',
            label: '资源信息',
            contentType: 'kv',
            kvItems: [
              { key: '区域', value: 'cn-north-1' },
              { key: '实例', value: 'i-123' },
            ],
          },
        ],
      }),
    )
    expect(screen.getByText('区域')).toBeInTheDocument()
    expect(screen.getByText('cn-north-1')).toBeInTheDocument()
    expect(screen.getByText('实例')).toBeInTheDocument()
    expect(screen.getByText('i-123')).toBeInTheDocument()
  })

  it('json 块内容非法时原文兜底展示（不崩渲染）', () => {
    renderExpanded(
      makeActivity({
        details: [{ id: 'j1', label: '坏JSON', contentType: 'json', content: '{oops' }],
      }),
    )
    expect(screen.getByText('{oops')).toBeInTheDocument()
  })

  it('json 块内容合法时格式化展示', () => {
    const { container } = renderExpanded(
      makeActivity({
        details: [{ id: 'j2', label: '好JSON', contentType: 'json', content: '{"a":1}' }],
      }),
    )
    const pre = container.querySelector('pre')
    expect(pre?.textContent).toContain('"a": 1')
  })
})

describe('原生卡片块分支（render 意图产物）', () => {
  it('diff → 差异对比视图（TextDiffView）', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd1',
            label: 'a.ts',
            contentType: 'diff',
            diffOld: 'old',
            diffNew: 'new',
          },
        ],
      }),
    )
    expect(screen.getByTestId('diff-detail-view')).toBeInTheDocument()
  })

  it('terminal → 命令头 + 输出（退出码可见）', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd2',
            label: '终端',
            contentType: 'terminal',
            terminal: { command: 'npm test', output: 'all green\n', exitCode: 0, running: false },
          },
        ],
      }),
    )
    const block = screen.getByTestId('terminal-block')
    expect(block.textContent).toContain('npm test')
    expect(block.textContent).toContain('all green')
    expect(screen.getByText('exit 0')).toBeInTheDocument()
  })

  it('read → 行号视图 + 窗口计数', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd2b',
            label: 'src/a.ts',
            contentType: 'read',
            read: { lines: [{ number: 1, text: 'line one' }, { number: 2, text: 'line two' }], totalLines: 50 },
          },
        ],
      }),
    )
    expect(screen.getByTestId('read-block')).toBeInTheDocument()
    expect(screen.getByText('显示 2 / 50 行')).toBeInTheDocument()
    expect(screen.getByText('line one')).toBeInTheDocument()
  })

  it('search matches → 文件分组行（可折叠）', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd3',
            label: '搜索结果',
            contentType: 'search',
            search: {
              kind: 'matches',
              files: [{ path: 'src/a.ts', matches: [{ lineNumber: 3, line: 'export foo' }] }],
              total: 1,
              truncated: false,
            },
          },
        ],
      }),
    )
    expect(screen.getByText('src/a.ts')).toBeInTheDocument()
    expect(screen.getByText('export foo')).toBeInTheDocument()
  })

  it('search paths → 平铺路径 + 计数', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd3b',
            label: '搜索结果',
            contentType: 'search',
            search: { kind: 'paths', paths: ['a.ts', 'b.ts'], total: 2, truncated: false },
          },
        ],
      }),
    )
    expect(screen.getByText('2 个路径')).toBeInTheDocument()
    expect(screen.getByText('a.ts')).toBeInTheDocument()
  })

  it('web fetch → URL + 状态码', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd4',
            label: '网页',
            contentType: 'web',
            web: { kind: 'fetch', url: 'https://example.com', statusCode: 200, truncated: false },
          },
        ],
      }),
    )
    expect(screen.getByText('200')).toBeInTheDocument()
    expect(screen.getByText('https://example.com')).toBeInTheDocument()
  })

  it('web search → 来源列表（标题可点）', () => {
    renderExpanded(
      makeActivity({
        details: [
          {
            id: 'd5',
            label: '搜索结果',
            contentType: 'web',
            web: {
              kind: 'search',
              sources: [{ url: 'https://a.com', title: 'A 站点' }],
              truncated: false,
            },
          },
        ],
      }),
    )
    expect(screen.getByText('A 站点')).toBeInTheDocument()
  })

  it('卡片块缺结构化字段 → 降级 JSON 预览（不崩渲染）', () => {
    renderExpanded(
      makeActivity({
        details: [
          // read 缺 read 字段 = 坏数据，应降级而非白屏
          { id: 'd6', label: '坏卡', contentType: 'read' },
        ],
      }),
    )
    expect(screen.getByText('坏卡')).toBeInTheDocument()
  })
})

describe('partialOutput 实时输出', () => {
  it('展开时逐条渲染流式中间输出', () => {
    renderExpanded(makeActivity({ partialOutput: ['chunk-one', 'chunk-two'] }))
    expect(screen.getByText('实时输出')).toBeInTheDocument()
    expect(screen.getByText('chunk-one')).toBeInTheDocument()
    expect(screen.getByText('chunk-two')).toBeInTheDocument()
  })
})

describe('actions 确认弹窗', () => {
  const confirmActivity = (onClick: () => void): ActivityData =>
    makeActivity({
      actions: [
        {
          id: 'wipe',
          icon: null,
          label: '清空缓存',
          type: 'custom',
          confirmMessage: '确定要清空吗？',
          onClick,
        },
      ],
    })

  it('确认路径：弹窗点「确认」→ 动作执行', async () => {
    const onClick = vi.fn()
    renderExpanded(confirmActivity(onClick))
    fireEvent.click(screen.getByRole('button', { name: '清空缓存' }))
    expect(screen.getByRole('dialog', { name: '确认操作' })).toBeInTheDocument()
    expect(screen.getByText('确定要清空吗？')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() => {
      expect(onClick).toHaveBeenCalledTimes(1)
    })
    expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
  })

  it('取消路径：弹窗点「取消」→ 动作不执行', async () => {
    const onClick = vi.fn()
    renderExpanded(confirmActivity(onClick))
    fireEvent.click(screen.getByRole('button', { name: '清空缓存' }))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
    })
    expect(onClick).not.toHaveBeenCalled()
  })

  it('点遮罩等同取消：动作不执行', async () => {
    const onClick = vi.fn()
    const { container } = renderExpanded(confirmActivity(onClick))
    fireEvent.click(screen.getByRole('button', { name: '清空缓存' }))
    const overlay = screen.getByRole('dialog', { name: '确认操作' }).firstElementChild as HTMLElement
    fireEvent.click(overlay)
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
    })
    expect(onClick).not.toHaveBeenCalled()
    expect(container).toBeInTheDocument()
  })

  it('无确认语的动作直接执行（对比组）', async () => {
    const onClick = vi.fn()
    renderExpanded(
      makeActivity({
        actions: [{ id: 'go', icon: null, label: '直接执行', type: 'custom', onClick }],
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: '直接执行' }))
    await waitFor(() => {
      expect(onClick).toHaveBeenCalledTimes(1)
    })
    expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument()
  })
})
