/**
 * 用户旅程 11：附件上传与预览
 *
 * 覆盖场景：
 * 1. 上传文本附件 → 待发送预览区显示文件名（验证上传链路）
 * 2. 发送带附件的消息 → 用户气泡内渲染附件卡片（验证渲染改动）
 * 3. 点击附件卡片 → 触发 openAttachment，工作区新增预览标签（验证预览打开）
 *
 * 对应需求：能解析出文本的文件都能发送，附件在消息中可见且可点击预览。
 *
 * 断言方式：DOM 元素断言（toBeVisible / toHaveCount）
 */

import { test, expect } from '@playwright/test';
import { loginAndWaitReady } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';

test.describe.configure({ timeout: 120_000 });

/** Windows 下 setInputFiles 后文件句柄可能延迟释放，清理需容错 */
function safeUnlink(filePath: string): void {
  try {
    fs.unlinkSync(filePath);
  } catch {
    // 文件被占用或已删除，忽略
  }
}

/** 创建临时文本文件并返回路径 */
function createTempTextFile(prefix: string, content: string): string {
  const tmpDir = path.join(os.tmpdir(), 'e2e-attachment');
  fs.mkdirSync(tmpDir, { recursive: true });
  const filePath = path.join(tmpDir, `${prefix}-${Date.now()}.txt`);
  fs.writeFileSync(filePath, content, 'utf-8');
  return filePath;
}

test.describe('旅程11：附件上传与预览', () => {
  test('11.1 上传文本附件后预览区显示文件名', async ({ page }) => {
    await loginAndWaitReady(page);

    const filePath = createTempTextFile('e2e-notes', '这是一个测试文本文件的内容');

    // 通过隐藏的 file input 设置文件（触发上传）
    const fileInput = page.locator('input[type="file"]').first();
    await fileInput.setInputFiles(filePath);

    // 等待待发送预览区出现文件名
    const preview = page.locator('text=e2e-notes-').first();
    await expect(preview, '待发送预览区应显示文件名').toBeVisible({ timeout: 15_000 });

    safeUnlink(filePath);
  });

  test('11.2 发送带附件的消息后用户气泡渲染附件卡片', async ({ page }) => {
    await loginAndWaitReady(page);

    const filePath = createTempTextFile('e2e-doc', '文件内容供分析');

    const fileInput = page.locator('input[type="file"]').first();
    await fileInput.setInputFiles(filePath);

    // 等待附件出现在预览区（确认上传流程启动）
    await expect(page.locator('text=e2e-doc-').first()).toBeVisible({ timeout: 15_000 });

    // 等待上传完成（loading spinner 消失），上传通常 < 2s
    await page.waitForTimeout(3000);

    await sendChatMessage(page, '请分析这个文件');

    // 用户消息内应出现附件卡片（button 含文件名）
    const attachmentCard = page.locator('button:has-text("e2e-doc-")').first();
    await expect(attachmentCard, '附件卡片应在用户气泡内渲染').toBeVisible({ timeout: 15_000 });

    safeUnlink(filePath);
  });

  test('11.3 点击附件卡片触发预览标签打开', async ({ page }) => {
    await loginAndWaitReady(page);

    const filePath = createTempTextFile('e2e-preview', '预览测试内容');

    const fileInput = page.locator('input[type="file"]').first();
    await fileInput.setInputFiles(filePath);
    await expect(page.locator('text=e2e-preview-').first()).toBeVisible({ timeout: 15_000 });

    await page.waitForTimeout(3000);
    await sendChatMessage(page, '看下这个文件');

    // 等待附件卡片渲染
    const attachmentCard = page.locator('button:has-text("e2e-preview-")').first();
    await expect(attachmentCard, '附件卡片应可见').toBeVisible({ timeout: 15_000 });

    // 点击附件卡片 → 触发 openAttachment（注册 fileEditor + 新增工作区 Tab）
    await attachmentCard.click();

    // 验证工作区新增了预览标签：标签标题含文件名
    // openAttachment 会 addWorkspaceTab(moduleId=__file_editor__, title=文件名)
    const previewTab = page.locator('[role="tab"]:has-text("e2e-preview-"), button:has-text("e2e-preview-")').last();
    await expect(previewTab, '点击后应出现文件预览标签').toBeVisible({ timeout: 15_000 });

    safeUnlink(filePath);
  });
});
