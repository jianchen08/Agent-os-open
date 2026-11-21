/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * 额度网关页（acp_quota_pool/acp_gateway_console）渲染链回归锁
 *
 * 契约：contributes.pages 声明 widget:'webview' 的额度网关页经工作区页签打开后，
 * 必须走 WebviewWidget iframe（srcDoc 沙箱 + bootstrap 桥）；插件控制台页
 * （生产真值 = acp_console_page.py 的 CONSOLE_HTML，本测试运行时直读、不维护
 * 手抄副本）在「探活成功自导航 / 网关未运行 / 状态检查失败 / 桥不可用 /
 * 宿主错误应答」情形下均有可见内容（卡片+状态行），不得整页空白。
 *
 * 链路：真实 ContributionRegistry → openWorkspacePanelByPath → layoutModeStore 页签
 * → FiveSpaceLayout renderTabContent → widgetRegistry.get('webview') → WebviewWidget。
 * 仅 HTTP 外部依赖打桩（apiClient），渲染链全真。
 */
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import { loadSchemaIntoRegistries } from '@/test/schemaLoader'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { ResizeObserverStub, setViewportWidth } from './helpers/fiveSpaceTestUtils'

vi.mock('@/components/workspace/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="mock-code-editor" />,
}))

vi.mock('@/hooks/queries/useSchemaQuery', () => ({
  useSchemaQuery: () => ({ data: undefined as Record<string, unknown> | undefined }),
}))

vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null,
}))

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}))
vi.mock('@/services/api/client', () => ({
  default: apiMock,
  apiClient: apiMock,
  isNotFoundError: () => false,
}))

// acp_quota_pool/plugin.json contributes.pages 中 acp_gateway_console 原样声明
const ACP_PAGE = {
  type: 'pages',
  id: 'acp_gateway_console',
  title: '额度网关',
  icon: '🏦',
  space: 'workspace',
  slot: 'tab',
  path: '/p/acp_gateway_console',
  widget: 'webview',
  order: 63,
  props: {
    pluginId: 'acp_quota_pool',
    htmlPath: '/page/console',
    widgetId: 'acp_gateway_console',
  },
  description: 'CPA 网关管理控制台（webview 直嵌网关管理页；网关未运行时提供拉起按钮）',
  pluginId: 'acp_quota_pool',
}

/**
 * 生产控制台页直读：锁对象 = 生产文件本身。手抄副本会随生产 HTML 漂移而
 * 静默失锁（兜底分支被删/改文案时测试仍绿），直读使任何生产兜底分支漂移即红。
 */
function loadProductionConsoleHtml(): string {
  const repoRoot = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../../..',
  )
  const pySource = readFileSync(
    join(repoRoot, 'plugins/shared/acp_quota_pool/acp_console_page.py'),
    'utf-8',
  )
  const marker = 'CONSOLE_HTML = """'
  const start = pySource.indexOf(marker)
  if (start < 0) {
    throw new Error('acp_console_page.py 缺少 CONSOLE_HTML 定义（结构变更须同步本抽取逻辑）')
  }
  const bodyStart = start + marker.length
  const end = pySource.indexOf('"""', bodyStart)
  if (end < 0) {
    throw new Error('acp_console_page.py 的 CONSOLE_HTML 闭合三引号缺失（结构变更须同步本抽取逻辑）')
  }
  return pySource.slice(bodyStart, end)
}

const CONSOLE_HTML = loadProductionConsoleHtml()

/**
 * jsdom 内装配页面：骨架（卡片三行）入 DOM + 返回脚本正文供 eval 直跑。
 * 桥（window.agentos）按用例供给或缺席——页面自身对两种形态都有兜底。
 */
function mountConsolePage(): string {
  document.body.innerHTML = CONSOLE_HTML.slice(
    CONSOLE_HTML.indexOf('<div class="card">'),
    CONSOLE_HTML.indexOf('<script>'),
  )
  return CONSOLE_HTML.slice(
    CONSOLE_HTML.indexOf('<script>') + '<script>'.length,
    CONSOLE_HTML.indexOf('</script>'),
  )
}

describe('FiveSpaceLayout — 额度网关页签走 webview iframe（acp_quota_pool 样本）', () => {
  beforeEach(() => {
    globalThis.ResizeObserver = ResizeObserverStub as never
    setViewportWidth(1280)
    initializeWidgets()
    contributionRegistry.clear()
    apiMock.get.mockReset()
    apiMock.get.mockResolvedValue({ data: CONSOLE_HTML })
    apiMock.post.mockReset()
    useLayoutModeStore.setState({
      workspaceTabs: [],
      floatingWindows: [],
      fullscreenActive: false,
      fullscreenTitle: null,
      fullscreenContent: null,
      workspaceDataVersion: 0,
      visitedTabIds: [],
    })
    useUIStore.setState({ sidebarCollapsed: false, workspaceCollapsed: false })
  })

  it('path 直达额度网关页 → 页签内容为 webview iframe，桥注入完整，无任何回退标记', async () => {
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        {
          plugin_id: 'acp_quota_pool',
          plugin_name: 'ACP 额度池',
          contributes: { pages: [ACP_PAGE] },
        },
      ],
      plugin_configs: [],
    })
    expect(openWorkspacePanelByPath('/p/acp_gateway_console')).toBe(true)

    render(
      <MemoryRouter>
        <FiveSpaceLayout
          chatContent={<div data-testid="chat-content" />}
          sidebarContent={<div data-testid="sidebar-content" />}
        />
      </MemoryRouter>,
    )

    expect(screen.getByTestId('workspace-tab-ws-plugin-acp_gateway_console')).toBeInTheDocument()

    // webview 路径：拉插件控制台 HTML 端点（非文件路径、非占位）
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(String(apiMock.get.mock.calls[0][0])).toBe('/ext/acp_quota_pool/page/console')

    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const srcDoc = iframe.getAttribute('srcdoc') ?? ''
    // 沙箱完整：CSP + bootstrap 桥 + 页面骨架卡片（生产真值内容）
    expect(srcDoc).toContain('Content-Security-Policy')
    expect(srcDoc).toContain('window.agentos')
    expect(srcDoc).toContain('__ready')
    expect(srcDoc).toContain('ACP 额度网关控制台')
    expect(iframe.getAttribute('sandbox') ?? '').not.toContain('allow-same-origin')

    // 无回退痕迹：html_preview 降级 / 组件占位 / 桥断错误态均不得出现
    expect(screen.queryByText('模块内容不可用')).not.toBeInTheDocument()
    expect(screen.queryByText(/组件 webview 未注册/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Webview 加载失败/)).not.toBeInTheDocument()
    expect(document.querySelector('[data-fallback]')).toBeNull()
  })

  it('生产控制台页含五种情形兜底渲染路径——不因后端数据缺失整页空白', () => {
    // ① 探活成功：显式运行态卡片 + 「新窗口打开管理页」按钮（2026-11-12 修订：
    //    不再 location.replace 自导航——网关管理页是 React SPA，强依赖
    //    localStorage/sessionStorage（实证 34 处引用），iframe 自导航后沙箱旗标
    //    随浏览上下文继承、SPA 仍在 opaque origin 内运行，访问 storage 抛
    //    SecurityError 初始化崩溃 → 整页空白（用户实测装机白屏根因）。
    //    新窗口（window.open，沙箱 allow-popups）是真实 origin，SPA 正常。
    expect(CONSOLE_HTML).toContain('网关运行中')
    expect(CONSOLE_HTML).toContain('新窗口打开管理页')
    // 白屏根因防复发锁：生产页不得再出现任何 iframe 自导航
    expect(CONSOLE_HTML).not.toContain('location.replace')
    // ② 网关未运行：显式状态 + 重新检查按钮（2026-11-21 修订：拉起职责移交
    //    llm_provider_gateway 插件托管，页内只留重探动作）
    expect(CONSOLE_HTML).toContain('网关未运行')
    expect(CONSOLE_HTML).toContain('拉起由 llm_provider_gateway 插件托管')
    expect(CONSOLE_HTML).toContain('重新检查')
    // ③ 状态检查失败：catch 分支落可见错误详情（escapeHtml 防注入）
    expect(CONSOLE_HTML).toContain('状态检查失败')
    // ④ 桥不可用（window.agentos 缺失）：agentosFetch reject → boot catch 落 renderStopped
    expect(CONSOLE_HTML).toContain('window.agentos 不可用')
    // ⑤ 宿主错误应答（.error 协议 → reject；端点 5xx 语义兜底）
    expect(CONSOLE_HTML).toContain('.error')
    // boot 与 catch 分支真实接线（页面装载即探活，失败有兜底）；
    // 主题令牌桥在位 = 生产真值特征（区别于任何手抄副本）
    expect(CONSOLE_HTML).toContain('boot();')
    expect(CONSOLE_HTML).toContain('theme.sync')
  })

  it('桥不可用（window.agentos 缺失）→ 页面落 renderStopped 可见态，不留「正在检查」假加载', async () => {
    const scriptBody = mountConsolePage()
    delete (window as unknown as { agentos?: unknown }).agentos
    // 生产脚本正文直跑：boot() 探活 → agentosFetch 因桥缺席 reject → catch → renderStopped
    ;(0, eval)(scriptBody)
    await waitFor(() =>
      expect(document.getElementById('state')?.textContent).toContain('网关未运行'),
    )
    // 错误详情落 #detail 行（renderStopped 契约：状态行=主态、detail=归因），
    // 不因「正在检查网关状态...」假加载停留，也不整页空白
    expect(document.getElementById('detail')?.textContent).toContain('状态检查失败')
    expect(document.getElementById('detail')?.textContent).toContain('window.agentos 不可用')
    // 重新检查按钮可见（兜底动作面不缺失；拉起已移交 llm_provider_gateway 托管）
    expect(document.getElementById('btn_recheck')).not.toBeNull()
  })

  it('宿主回错误应答（端点 500 语义，.error 分支）→ 页面落 renderStopped 可见态并透出归因', async () => {
    const scriptBody = mountConsolePage()
    ;(window as unknown as { agentos?: unknown }).agentos = {
      postMessage: () => 'req-err-case',
    }
    // 生产脚本正文直跑：boot() 探活；桥监听在 eval 同步期即注册
    //（agentosFetch 的 Promise executor 同步执行 addEventListener）
    ;(0, eval)(scriptBody)
    // 宿主按 .error 协议回错误应答 → agentosFetch reject → boot catch → renderStopped
    window.dispatchEvent(
      new MessageEvent('message', {
        data: {
          __agentos_webview: true,
          id: 'req-err-case',
          method: 'http.handle.error',
          params: { error: 'HTTP 500' },
        },
      }),
    )
    await waitFor(() =>
      expect(document.getElementById('state')?.textContent).toContain('网关未运行'),
    )
    expect(document.getElementById('detail')?.textContent).toContain('状态检查失败')
    expect(document.getElementById('detail')?.textContent).toContain('HTTP 500')
    // 重新检查按钮可见（兜底动作面不缺失；拉起已移交 llm_provider_gateway 托管）
    expect(document.getElementById('btn_recheck')).not.toBeNull()
  })
})
