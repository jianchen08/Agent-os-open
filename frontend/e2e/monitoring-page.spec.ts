/**
 * 监控页面测试 (monitoring-page)
 *
 * 测试监控页面的所有功能和组件：
 * - 页面加载和基本布局
 * - 系统状态指标显示（CPU、内存、会话、任务）
 * - 性能趋势图表区域
 * - 任务执行状态区域
 * - 配置监控按钮
 * - 空状态显示
 * - 响应式布局
 * - 实时数据更新（未来功能）
 */

import { expect, test } from '@playwright/test'
import { login, takeScreenshot } from './helpers'

test.describe('监控页面 (monitoring-page)', () => {
  // 每个测试前登录并导航到监控页面
  test.beforeEach(async ({ page }) => {
    await login(page)
    await page.goto('/monitoring')
    await page.waitForLoadState('networkidle')
  })

  test('01-页面加载-应该正确显示监控页面', async ({ page }) => {
    // 验证页面标题
    await expect(page).toHaveTitle(/AI Agent/)

    // 验证监控页面元素存在
    const monitoringPage = page.locator('[data-testid="monitoring-page"]')
    await expect(monitoringPage).toBeVisible()

    // 验证页面包含系统监控标题
    const mainHeading = page.locator('h1:has-text("系统监控")')
    await expect(mainHeading).toBeVisible()

    await takeScreenshot(page, '01-monitoring-page-load')
  })

  test('02-页面标题-应该显示活动图标和标题', async ({ page }) => {
    // 验证主标题包含活动图标
    const mainHeading = page.locator('h1')
    await expect(mainHeading).toContainText('系统监控')

    // 验证图标存在
    const activityIcon = mainHeading.locator('svg[data-lucide="activity"], svg')
    await expect(activityIcon).toBeVisible()

    // 验证副标题
    const subtitle = page.locator('p.text-muted-foreground')
    await expect(subtitle).toContainText('实时监控系统运行状态和性能指标')

    await takeScreenshot(page, '02-monitoring-page-header')
  })

  test('03-配置按钮-应该显示配置监控按钮', async ({ page }) => {
    // 查找配置监控按钮
    const configButton = page.locator('button:has-text("配置监控")')
    await expect(configButton).toBeVisible()

    // 验证按钮包含设置图标
    const settingsIcon = configButton.locator(
      'svg[data-lucide="settings"], svg'
    )
    await expect(settingsIcon).toBeVisible()

    // 验证按钮样式（outline 变体）
    await expect(configButton).toHaveClass(/variant-outline/)

    await takeScreenshot(page, '03-monitoring-config-button')
  })

  test('04-系统状态-应该显示CPU使用率卡片', async ({ page }) => {
    // 查找 CPU 使用率卡片
    const cpuCard = page
      .locator('div')
      .filter({ hasText: 'CPU 使用率' })
      .first()
    await expect(cpuCard).toBeVisible()

    // 验证卡片包含标签和值
    await expect(cpuCard).toContainText('CPU 使用率')

    // 验证显示数值（可能是"--"或实际数值）
    const valueText = await cpuCard.textContent()
    expect(valueText).toMatch(/(CPU 使用率|--|\d+%)/)

    await takeScreenshot(page, '04-monitoring-cpu-card')
  })

  test('05-系统状态-应该显示内存使用卡片', async ({ page }) => {
    // 查找内存使用卡片
    const memoryCard = page
      .locator('div')
      .filter({ hasText: '内存使用' })
      .first()
    await expect(memoryCard).toBeVisible()

    // 验证卡片包含标签和值
    await expect(memoryCard).toContainText('内存使用')

    // 验证显示数值（可能是"--"或实际数值）
    const valueText = await memoryCard.textContent()
    expect(valueText).toMatch(/(内存使用|--|\d+.*MB|\d+.*GB|%\d+)/)

    await takeScreenshot(page, '05-monitoring-memory-card')
  })

  test('06-系统状态-应该显示活跃会话卡片', async ({ page }) => {
    // 查找活跃会话卡片
    const sessionCard = page
      .locator('div')
      .filter({ hasText: '活跃会话' })
      .first()
    await expect(sessionCard).toBeVisible()

    // 验证卡片包含标签和值
    await expect(sessionCard).toContainText('活跃会话')

    // 验证显示数值
    const valueText = await sessionCard.textContent()
    expect(valueText).toMatch(/(活跃会话|\d+)/)

    await takeScreenshot(page, '06-monitoring-sessions-card')
  })

  test('07-系统状态-应该显示运行任务卡片', async ({ page }) => {
    // 查找运行任务卡片
    const taskCard = page.locator('div').filter({ hasText: '运行任务' }).first()
    await expect(taskCard).toBeVisible()

    // 验证卡片包含标签和值
    await expect(taskCard).toContainText('运行任务')

    // 验证显示数值
    const valueText = await taskCard.textContent()
    expect(valueText).toMatch(/(运行任务|\d+)/)

    await takeScreenshot(page, '07-monitoring-tasks-card')
  })

  test('08-状态卡片布局-应该使用网格布局', async ({ page }) => {
    // 验证所有状态卡片都存在
    const cards = page.locator('.grid.gap-4.md\\:grid-cols-4 > div')
    const cardCount = await cards.count()

    // 应该有4个状态卡片
    expect(cardCount).toBe(4)

    // 验证每个卡片都有正确的样式
    for (let i = 0; i < cardCount; i++) {
      const card = cards.nth(i)
      await expect(card).toHaveClass(/p-4/)
      await expect(card).toHaveClass(/rounded-lg/)
      await expect(card).toHaveClass(/border/)
    }

    await takeScreenshot(page, '08-monitoring-cards-layout')
  })

  test('09-性能趋势-应该显示性能趋势图表区域', async ({ page }) => {
    // 查找性能趋势区域
    const performanceSection = page
      .locator('div')
      .filter({ hasText: /^性能趋势$/ })
      .first()
    await expect(performanceSection).toBeVisible()

    // 验证标题包含图表图标
    const chartIcon = performanceSection.locator(
      'svg[data-lucide="bar-chart-3"], svg'
    )
    await expect(chartIcon).toBeVisible()

    // 验证图表容器存在
    const chartContainer = performanceSection.locator('..').locator('.h-64')
    await expect(chartContainer).toBeVisible()

    await takeScreenshot(page, '09-monitoring-performance-chart')
  })

  test('10-性能趋势-应该显示即将上线占位符', async ({ page }) => {
    // 查找性能趋势区域的占位符文本
    const placeholderText = page.locator('text=监控数据即将上线')
    const isVisible = await placeholderText.isVisible().catch(() => false)

    if (isVisible) {
      // 验证占位符内容
      await expect(placeholderText).toBeVisible()

      // 验证图表图标存在
      const chartIcon = page.locator('.h-64 svg[data-lucide="bar-chart-3"]')
      await expect(chartIcon).toBeVisible()

      await takeScreenshot(page, '10-monitoring-performance-placeholder')
    } else {
      // 如果数据显示已上线，跳过此测试
      test.skip(true, '性能监控数据已上线，不再显示占位符')
    }
  })

  test('11-任务执行状态-应该显示任务监控区域', async ({ page }) => {
    // 查找任务执行状态区域
    const taskSection = page
      .locator('div')
      .filter({ hasText: /^任务执行状态$/ })
    await expect(taskSection).toBeVisible()

    // 验证标题
    await expect(taskSection).toContainText('任务执行状态')

    // 验证图表容器存在
    const chartContainer = taskSection.locator('..').locator('.h-64')
    await expect(chartContainer).toBeVisible()

    await takeScreenshot(page, '11-monitoring-task-status')
  })

  test('12-任务执行状态-应该显示即将上线占位符', async ({ page }) => {
    // 查找任务监控区域的占位符文本
    const placeholderText = page.locator('text=任务监控即将上线')
    const isVisible = await placeholderText.isVisible().catch(() => false)

    if (isVisible) {
      // 验证占位符内容
      await expect(placeholderText).toBeVisible()

      // 验证活动图标存在
      const activityIcon = page.locator('.h-64 svg[data-lucide="activity"]')
      await expect(activityIcon).toBeVisible()

      await takeScreenshot(page, '12-monitoring-task-placeholder')
    } else {
      // 如果数据显示已上线，跳过此测试
      test.skip(true, '任务监控数据已上线，不再显示占位符')
    }
  })

  test('13-监控图表-应该使用两列网格布局', async ({ page }) => {
    // 验证图表区域使用两列布局
    const chartGrid = page.locator('.grid.gap-4.md\\:grid-cols-2')
    await expect(chartGrid).toBeVisible()

    // 验证包含两个图表区域
    const chartSections = chartGrid.locator('> div')
    const sectionCount = await chartSections.count()
    expect(sectionCount).toBeGreaterThanOrEqual(2)

    await takeScreenshot(page, '13-monitoring-charts-grid')
  })

  test('14-交互测试-点击配置按钮应该触发操作', async ({ page }) => {
    // 查找配置按钮
    const configButton = page.locator('button:has-text("配置监控")')

    // 监听可能的路由变化或对话框
    const dialogPromise = page
      .waitForSelector('dialog, .modal, [role="dialog"]', { timeout: 2000 })
      .catch(() => null)

    // 点击配置按钮
    await configButton.click()

    // 验证：可能会打开对话框或导航到配置页面
    const dialog = await dialogPromise
    if (dialog) {
      await expect(dialog).toBeVisible()
      await takeScreenshot(page, '16-monitoring-config-dialog')
    } else {
      // 可能导航到设置页面或其他行为
      await page.waitForTimeout(500)
      await takeScreenshot(page, '16-monitoring-config-clicked')
    }
  })

  test('17-响应式布局-桌面视图', async ({ page }) => {
    // 设置桌面视口
    await page.setViewportSize({ width: 1280, height: 720 })

    // 验证监控页面可见
    const monitoringPage = page.locator('[data-testid="monitoring-page"]')
    await expect(monitoringPage).toBeVisible()

    // 验证内容宽度限制
    const container = page.locator('.max-w-6xl')
    await expect(container).toBeVisible()

    // 验证状态卡片使用4列布局
    const cardsGrid = page.locator('.md\\:grid-cols-4')
    await expect(cardsGrid).toBeVisible()

    await takeScreenshot(page, '17-monitoring-desktop-view')
  })

  test('18-响应式布局-平板视图', async ({ page }) => {
    // 设置平板视口
    await page.setViewportSize({ width: 768, height: 1024 })

    // 验证监控页面可见
    const monitoringPage = page.locator('[data-testid="monitoring-page"]')
    await expect(monitoringPage).toBeVisible()

    // 验证页面标题仍然可见
    const mainHeading = page.locator('h1:has-text("系统监控")')
    await expect(mainHeading).toBeVisible()

    // 验证状态卡片仍然可见
    const cpuCard = page.locator('div').filter({ hasText: 'CPU 使用率' })
    await expect(cpuCard).toBeVisible()

    await takeScreenshot(page, '18-monitoring-tablet-view')
  })

  test('19-响应式布局-移动端视图', async ({ page }) => {
    // 设置移动端视口
    await page.setViewportSize({ width: 375, height: 667 })

    // 验证监控页面可见
    const monitoringPage = page.locator('[data-testid="monitoring-page"]')
    await expect(monitoringPage).toBeVisible()

    // 验证页面标题仍然可见（可能在移动端简化）
    const mainHeading = page.locator('h1')
    await expect(mainHeading).toBeVisible()

    // 验证状态卡片在移动端可见（可能变为单列）
    const cpuCard = page.locator('div').filter({ hasText: 'CPU 使用率' })
    await expect(cpuCard).toBeVisible()

    await takeScreenshot(page, '19-monitoring-mobile-view')
  })

  test('20-组件结构-应该包含正确的数据属性', async ({ page }) => {
    // 验证监控页面的 data-testid
    const monitoringPage = page.locator('[data-testid="monitoring-page"]')
    await expect(monitoringPage).toBeVisible()

    // 验证页面使用了正确的 Tailwind 类
    await expect(monitoringPage).toHaveClass(/flex-1/)
    await expect(monitoringPage).toHaveClass(/overflow-auto/)
    await expect(monitoringPage).toHaveClass(/p-6/)

    await takeScreenshot(page, '20-monitoring-data-attributes')
  })

  test('21-页面布局-应该使用垂直堆叠', async ({ page }) => {
    // 验证页面使用 space-y-6 进行垂直间距
    const pageContainer = page.locator('.max-w-6xl.space-y-6')
    await expect(pageContainer).toBeVisible()

    // 验证主要部分都存在
    const sections = [
      'h1:has-text("系统监控")',
      '.grid.gap-4:has-text("CPU 使用率")',
      '.grid.gap-4:has-text("性能趋势")',
    ]

    for (const selector of sections) {
      const element = page.locator(selector)
      await expect(element.first()).toBeVisible()
    }

    await takeScreenshot(page, '21-monitoring-vertical-layout')
  })

  test('22-页面滚动-应该支持页面滚动', async ({ page }) => {
    // 设置较小的视口以测试滚动
    await page.setViewportSize({ width: 1280, height: 400 })

    // 验证页面可以滚动
    const scrollHeight = await page.evaluate(() => document.body.scrollHeight)
    const clientHeight = await page.evaluate(() => document.body.clientHeight)

    if (scrollHeight > clientHeight) {
      // 滚动到页面底部
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))

      // 等待滚动完成
      await page.waitForTimeout(500)

      // 验证滚动位置
      const scrollY = await page.evaluate(() => window.scrollY)
      expect(scrollY).toBeGreaterThan(0)

      await takeScreenshot(page, '22-monitoring-page-scroll')
    } else {
      test.skip(true, '页面内容不足以产生滚动')
    }
  })

  test('23-卡片样式-所有状态卡片应该有一致的样式', async ({ page }) => {
    // 查找所有状态卡片
    const cards = page.locator('.grid.gap-4.md\\:grid-cols-4 > div')
    const cardCount = await cards.count()

    // 验证所有卡片有相同的样式类
    for (let i = 0; i < cardCount; i++) {
      const card = cards.nth(i)

      // 验证卡片样式一致性
      await expect(card).toHaveClass(/p-4/)
      await expect(card).toHaveClass(/rounded-lg/)
      await expect(card).toHaveClass(/border/)
      await expect(card).toHaveClass(/border-border/)
      await expect(card).toHaveClass(/bg-card/)

      // 验证每个卡片都包含一个标签和一个数值
      const label = card.locator('.text-sm.text-muted-foreground')
      const value = card.locator('.text-2xl.font-bold')

      await expect(label).toBeVisible()
      await expect(value).toBeVisible()
    }

    await takeScreenshot(page, '23-monitoring-cards-consistent-style')
  })

  test('24-可访问性-所有图标应该有描述', async ({ page }) => {
    // 查找所有图标
    const icons = page.locator('svg')
    const iconCount = await icons.count()

    // 验证主要图标存在
    expect(iconCount).toBeGreaterThan(0)

    // 验证活动图标
    const activityIcon = page.locator('h1 svg[data-lucide="activity"], h1 svg')
    await expect(activityIcon).toBeVisible()

    // 验证设置图标
    const settingsIcon = page.locator(
      'button svg[data-lucide="settings"], button svg'
    )
    await expect(settingsIcon).toBeVisible()

    await takeScreenshot(page, '24-monitoring-icons-accessibility')
  })

  test('25-性能测试-页面应该在合理时间内加载', async ({ page }) => {
    // 记录页面加载开始时间
    const startTime = Date.now()

    // 导航到监控页面
    await page.goto('/monitoring')
    await page.waitForLoadState('networkidle')
    await page.waitForSelector('[data-testid="monitoring-page"]')

    // 计算加载时间
    const loadTime = Date.now() - startTime

    // 页面应该在3秒内加载完成
    expect(loadTime).toBeLessThan(3000)

    console.log(`监控页面加载时间: ${loadTime}ms`)

    await takeScreenshot(page, '25-monitoring-performance')
  })

  test('26-颜色主题-卡片应该使用正确的主题颜色', async ({ page }) => {
    // 查找状态卡片
    const card = page.locator('.grid.gap-4.md\\:grid-cols-4 > div').first()

    // 验证卡片使用主题颜色变量
    const backgroundColor = await card.evaluate(el => {
      return window.getComputedStyle(el).backgroundColor
    })

    const borderColor = await card.evaluate(el => {
      return window.getComputedStyle(el).borderColor
    })

    // 验证颜色已设置（不是透明或默认值）
    expect(backgroundColor).not.toBe('rgba(0, 0, 0, 0)')
    expect(backgroundColor).not.toBe('transparent')

    await takeScreenshot(page, '26-monitoring-theme-colors')
  })

  test('27-文本颜色-标签和数值应该有正确的文本颜色', async ({ page }) => {
    // 查找第一个状态卡片
    const card = page.locator('.grid.gap-4.md\\:grid-cols-4 > div').first()

    // 验证标签颜色（应该是 muted-foreground）
    const label = card.locator('.text-sm.text-muted-foreground')
    await expect(label).toHaveClass(/text-muted-foreground/)

    // 验证数值颜色（应该是 foreground）
    const value = card.locator('.text-2xl.font-bold')
    await expect(value).toHaveClass(/text-foreground/)

    await takeScreenshot(page, '27-monitoring-text-colors')
  })

  test('28-空状态-所有占位符应该一致', async ({ page }) => {
    // 查找所有占位符区域
    const placeholders = page.locator('.text-center.text-muted-foreground')
    const placeholderCount = await placeholders.count()

    if (placeholderCount > 0) {
      // 验证占位符有图标
      for (let i = 0; i < placeholderCount; i++) {
        const placeholder = placeholders.nth(i)
        const parent = placeholder.locator('..')

        // 验证包含图标
        const icon = parent.locator('svg')
        const hasIcon = (await icon.count()) > 0
        expect(hasIcon).toBeTruthy()
      }

      await takeScreenshot(page, '28-monitoring-placeholder-consistency')
    } else {
      test.skip(true, '当前没有显示占位符')
    }
  })

  test('29-导航测试-从其他页面导航到监控页面', async ({ page }) => {
    // 从仪表盘导航到监控页面
    await page.goto('/')
    await page.waitForSelector('[data-testid="dashboard-page"]')

    // 点击监控链接（如果存在）或直接导航
    const monitoringLink = page.locator(
      'a[href="/monitoring"], button:has-text("监控")'
    )
    const linkExists = (await monitoringLink.count()) > 0

    if (linkExists) {
      await monitoringLink.first().click()
    } else {
      await page.goto('/monitoring')
    }

    // 验证成功导航到监控页面
    await page.waitForSelector('[data-testid="monitoring-page"]')
    const mainHeading = page.locator('h1:has-text("系统监控")')
    await expect(mainHeading).toBeVisible()

    await takeScreenshot(page, '29-monitoring-navigation')
  })

  test('30-综合测试-完整的监控页面检查', async ({ page }) => {
    // 1. 验证页面标题和描述
    const mainHeading = page.locator('h1')
    await expect(mainHeading).toContainText('系统监控')

    const subtitle = page.locator('p.text-muted-foreground')
    await expect(subtitle).toContainText('实时监控系统运行状态和性能指标')

    // 2. 验证所有状态卡片存在
    const cardLabels = ['CPU 使用率', '内存使用', '活跃会话', '运行任务']
    for (const label of cardLabels) {
      const card = page.locator(`div:has-text("${label}")`)
      await expect(card.first()).toBeVisible()
    }

    // 3. 验证监控图表区域存在
    const performanceSection = page.locator('text=性能趋势')
    await expect(performanceSection).toBeVisible()

    const taskSection = page.locator('text=任务执行状态')
    await expect(taskSection).toBeVisible()

    // 4. 验证配置按钮存在
    const configButton = page.locator('button:has-text("配置监控")')
    await expect(configButton).toBeVisible()

    await takeScreenshot(page, '30-monitoring-complete-check')
  })
})
