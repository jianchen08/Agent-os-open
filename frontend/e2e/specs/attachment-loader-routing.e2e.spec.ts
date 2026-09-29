/**
 * 附件加载器路由 E2E（文件加载器路由设计 §2 用户契约浏览器实测）。
 *
 * 场景：上传 txt/docx → 发送 → 点击消息气泡里的附件链接。
 * 修复前：整窗导航 → 内核 /uploads 白名单 404 纯文本死页；
 * 修复后：docx（无加载器）→ 工作区「无对应的加载器」卡；
 *         txt（text loader）→ 鉴权内容端点取流 + 工作区显示内容；
 *         全程主文档 URL 不变、无裸 not found 文本。
 *
 * 运行前提：内核 9100（dev）+ AGENTOS_ADMIN_PASSWORD 环境变量。
 */
import { test, expect } from '@playwright/test'
import * as fs from 'fs'
import * as os from 'os'
import * as path from 'path'
import { loginAndWaitReady, ADMIN_USER } from '../helpers/auth'

const MARKER_TEXT = 'e2e附件内容验证-20260928'

test.describe('附件加载器路由（死页根治）', () => {
  let filesDir: string

  test.beforeAll(() => {
    filesDir = fs.mkdtempSync(path.join(os.tmpdir(), 'attach-e2e-'))
    fs.writeFileSync(path.join(filesDir, '说明.txt'), `${MARKER_TEXT}\n`, 'utf-8')
    // docx 形（PK 头 + 填充字节）——加载器路由按扩展名裁决，内容真伪无关
    fs.writeFileSync(path.join(filesDir, '报告.docx'), Buffer.from('PK\x03\x04 fake-docx-bytes-for-e2e'))
  })

  test('上传→发送→点击消息内附件：工作区打开而非整窗导航', async ({ page }) => {
    test.setTimeout(180_000)
    const shotDir = path.join('reports', 'attach-e2e')
    fs.mkdirSync(shotDir, { recursive: true })

    await loginAndWaitReady(page, ADMIN_USER)
    const urlBefore = page.url()

    // ── 上传两个附件（txt=有加载器 / docx=无加载器）──
    await page.setInputFiles('input[type="file"]', [
      path.join(filesDir, '说明.txt'),
      path.join(filesDir, '报告.docx'),
    ])
    await expect(page.getByText('说明.txt').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('报告.docx').first()).toBeVisible({ timeout: 30_000 })

    // ── 发送 ──
    await page.locator('[data-testid="chat-input-textarea"]').fill('附件打开测试，请忽略本条内容')
    await page.locator('[data-testid="chat-send-button"]').click()

    // 用户气泡 markdown 链接出现（隔离容器内的 <a>）
    const docxLink = page.locator('.lobe-chat-isolated a', { hasText: '报告.docx' }).first()
    await expect(docxLink).toBeVisible({ timeout: 90_000 })
    await page.screenshot({ path: path.join(shotDir, '1-bubble.png') })

    // ── 断言1：docx（无加载器）→ 工作区「无对应的加载器」卡，不整窗导航 ──
    await docxLink.click()
    expect(page.url()).toBe(urlBefore)
    await expect(page.getByText('暂无支持 .docx 类型的加载器')).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('body')).not.toContainText('not found')
    await page.screenshot({ path: path.join(shotDir, '2-docx-noloader.png') })

    // ── 断言2：txt（text loader）→ 鉴权端点取流 200 + 工作区显示内容 ──
    const txtRespPromise = page.waitForResponse(
      (r) => r.url().includes('/ext/artifacts/files/') && r.request().method() === 'GET',
      { timeout: 20_000 },
    )
    await page.locator('.lobe-chat-isolated a', { hasText: '说明.txt' }).first().click()
    const txtResp = await txtRespPromise
    expect(txtResp.status()).toBe(200)
    expect(page.url()).toBe(urlBefore)
    await expect(page.getByText(MARKER_TEXT).first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('body')).not.toContainText('not found')
    await page.screenshot({ path: path.join(shotDir, '3-txt-loaded.png') })
  })
})
