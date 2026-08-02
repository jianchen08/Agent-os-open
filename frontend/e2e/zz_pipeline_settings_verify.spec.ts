/**
 * PipelineSettingsPage 功能验证 E2E spec
 *
 * 验证策略：
 * - 真实 vite dev server（http://localhost:5290）+ 真实 chromium 浏览器
 * - 后端内核二进制缺失（kernel/target/release/agentos-kernel 不存在），
 *   故用 page.route() 拦截 /api/v1/config/pipelines/** 模拟后端响应，
 *   前端页面、路由、交互、状态渲染均为真实行为。
 * - 覆盖 6 个功能场景（AC1 入口 / AC2 读取展示 / AC3 修改保存 / AC4 路由 / AC5 tabs / AC6 异常路径）
 *
 * 运行：node node_modules/@playwright/test/cli.js test e2e/zz_pipeline_settings_verify.spec.ts --reporter=list
 */
import { test, expect, Page, Route } from '@playwright/test'

// ── 使用 headless_shell-1234（内存占用小，避免受限容器 OOM 导致 Page crashed）──
test.use({
  launchOptions: {
    executablePath:
      '/opt/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  } as any,
})



// vite.e2e.config.ts 禁用 optimizeDeps 后首次加载需按需转换依赖，放宽超时
test.setTimeout(180_000)

const BASE = 'http://localhost:5290'
const NAV_TIMEOUT = 120_000
const EXPECT_TIMEOUT = 30_000

/** 样例管道配置（对齐测试文件 samplePipeline，0.1 扁平格式） */
const samplePipeline = {
  name: 'agentos_agent',
  task_worker: { pipeline_timeout: 7200 },
  input_routes: [
    {
      name: 'tool_execute',
      condition: "core_type == 'tool_execute'",
      target: 'core',
      plugins: ['tool_schema', 'param_inject'],
      priority: 10,
    },
  ],
  output_routes: [],
  plugins: [],
  core_plugins: {},
}

/** l1-main 管道配置 */
const l1MainPipeline = {
  name: 'l1_main_agent',
  input_routes: [{ name: 'router', target: 'core', plugins: ['l1_router'], priority: 5 }],
  output_routes: [],
  plugins: [],
  core_plugins: {},
}

/** 拦截 API 请求的辅助：按管道名返回配置，记录 PUT 请求体 */
function mockPipelinesApi(page: Page, log: { puts: any[]; gets: string[] }) {
  return page.route('**/api/v1/config/pipelines/**', async (route: Route) => {
    const url = route.request().url()
    const method = route.request().method()
    const name = url.split('/').pop() || ''
    log.gets.push(`${method}:${name}`)
    if (method === 'GET') {
      const data = name === 'l1-main' ? l1MainPipeline : samplePipeline
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name, data, etag: `etag-${name}` }),
      })
    } else if (method === 'PUT') {
      log.puts.push(JSON.parse(route.request().postData() || '{}'))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name, etag: 'etag-new' }),
      })
    } else {
      await route.continue()
    }
  })
}

/** 拦截 schema 请求（SettingsPage 会加载插件面板 schema，失败时静默不影响内置项） */
async function mockSchema(page: Page) {
  await page.route('**/api/v1/schema**', (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
  )
  await page.route('**/ext/channel_api/config/**', (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
  )
}

test.describe('场景1+2：AC1 入口 + AC2 读取展示（/settings 内联）', () => {
  test('左侧「内核设置」分组出现「管道配置」，点击后右侧内联渲染标题+tabs+字段', async ({ page }) => {
    const log: { puts: any[]; gets: string[] } = { puts: [], gets: [] }
    await mockSchema(page)
    await mockPipelinesApi(page, log)
    const errors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text())
    })

    await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })

    // AC1: 左侧「内核设置」分组 + 「管道配置」设置栏
    await expect(page.getByText('内核设置', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })
    await expect(page.getByText('管道配置', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })

    // 点击「管道配置」
    await page.getByText('管道配置', { exact: true }).click()

    // 右侧内联显示标题「管道配置」+ tabs
    await expect(page.locator('h2', { hasText: '管道配置' })).toBeVisible({ timeout: EXPECT_TIMEOUT })
    await expect(page.getByRole('tab', { name: '默认' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'L1 主 Agent' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'L2 评估' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'L2 子任务' })).toBeVisible()

    // AC2: 默认 tab 加载 → 调用 getPipelineConfig('default') → 展示字段
    await expect(page.getByDisplayValue('agentos_agent')).toBeVisible({ timeout: EXPECT_TIMEOUT })
    // input_routes 数组渲染 JSON textarea
    await expect(page.getByDisplayValue(/tool_schema/)).toBeVisible({ timeout: EXPECT_TIMEOUT })
    // 保存按钮
    await expect(page.getByText('保存配置', { exact: true })).toBeVisible()

    // 状态传递：确认 GET default 被调用
    expect(log.gets).toContain('GET:default')
    expect(errors.filter((e) => e.includes('Failed to fetch') || e.includes('NetworkError'))).toEqual([])
  })
})

test.describe('场景3：AC3 修改保存', () => {
  test('修改字段 → 保存 → PUT {data} → 显示「已保存」', async ({ page }) => {
    const log: { puts: any[]; gets: string[] } = { puts: [], gets: [] }
    await mockSchema(page)
    await mockPipelinesApi(page, log)

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
    await expect(page.getByDisplayValue('agentos_agent')).toBeVisible({ timeout: EXPECT_TIMEOUT })

    // 修改 name 字段
    const nameInput = page.getByDisplayValue('agentos_agent')
    await nameInput.fill('agentos_agent_modified')

    // 点击保存
    await page.getByText('保存配置', { exact: true }).click()

    // 显示「已保存」
    await expect(page.getByText('已保存', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })

    // PUT 请求体为 { data: config }，且 data.name 为修改后的值
    expect(log.puts.length).toBe(1)
    expect(log.puts[0]).toHaveProperty('data')
    expect(log.puts[0].data.name).toBe('agentos_agent_modified')
  })

  test('保存失败 → 显示「保存失败」+ toast 错误', async ({ page }) => {
    const log: { puts: any[]; gets: string[] } = { puts: [], gets: [] }
    await mockSchema(page)
    // 覆盖 PUT 为失败
    await page.route('**/api/v1/config/pipelines/**', async (route: Route) => {
      const method = route.request().method()
      if (method === 'PUT') {
        log.puts.push(JSON.parse(route.request().postData() || '{}'))
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ message: 'save failed' }) })
      } else if (method === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ name: 'default', data: samplePipeline, etag: 'e1' }),
        })
      } else {
        await route.continue()
      }
    })

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
    await expect(page.getByDisplayValue('agentos_agent')).toBeVisible({ timeout: EXPECT_TIMEOUT })

    await page.getByText('保存配置', { exact: true }).click()

    // 显示「保存失败」
    await expect(page.getByText('保存失败', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })
    // toast 错误
    await expect(page.getByText('配置保存失败')).toBeVisible({ timeout: EXPECT_TIMEOUT })
  })
})

test.describe('场景4：AC4 路由直达', () => {
  test('直接访问 /settings/pipeline → 独立模式含「← 返回设置」头', async ({ page }) => {
    const log: { puts: any[]; gets: string[] } = { puts: [], gets: [] }
    await mockSchema(page)
    await mockPipelinesApi(page, log)

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })

    // 独立模式：包含「← 返回设置」链接 + h1 标题「管道配置」
    await expect(page.getByText('← 返回设置', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })
    await expect(page.locator('h1', { hasText: '管道配置' })).toBeVisible()
    await expect(page.getByRole('tab', { name: '默认' })).toBeVisible()
    await expect(page.getByDisplayValue('agentos_agent')).toBeVisible({ timeout: EXPECT_TIMEOUT })
  })
})

test.describe('场景5：AC5 tabs 切换', () => {
  test('点击「L1 主 Agent」tab → 调用 getPipelineConfig(\'l1-main\') 并加载对应管道', async ({ page }) => {
    const log: { puts: any[]; gets: string[] } = { puts: [], gets: [] }
    await mockSchema(page)
    await mockPipelinesApi(page, log)

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
    await expect(page.getByRole('tab', { name: 'L1 主 Agent' })).toBeVisible({ timeout: EXPECT_TIMEOUT })

    await page.getByRole('tab', { name: 'L1 主 Agent' }).click()

    // 加载 l1-main 管道：显示 l1_main_agent 字段
    await expect(page.getByDisplayValue('l1_main_agent')).toBeVisible({ timeout: EXPECT_TIMEOUT })
    expect(log.gets).toContain('GET:l1-main')
  })
})

test.describe('场景6：AC6 异常路径', () => {
  test('加载失败 → 显示「无法加载配置」错误提示', async ({ page }) => {
    await mockSchema(page)
    await page.route('**/api/v1/config/pipelines/**', (route: Route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ message: 'boom' }) }),
    )

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })

    await expect(page.getByText('无法加载配置', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })
  })

  test('空配置 → 表单显示「该配置暂无字段」且保存按钮可用', async ({ page }) => {
    await mockSchema(page)
    await page.route('**/api/v1/config/pipelines/**', (route: Route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ name: 'default', data: {}, etag: 'e-empty' }) }),
    )

    await page.goto(`${BASE}/settings/pipeline`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })

    await expect(page.getByText('该配置暂无字段', { exact: true })).toBeVisible({ timeout: EXPECT_TIMEOUT })
    const saveBtn = page.getByText('保存配置', { exact: true })
    await expect(saveBtn).toBeVisible()
    // 保存按钮可用（未被禁用）
    await expect(saveBtn).toBeEnabled()
  })
})
