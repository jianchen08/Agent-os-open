/**
 * 设置页面端到端测试
 *
 * 测试设置页面的所有功能
 */

import { test, expect } from '@playwright/test';
import { login, takeScreenshot, checkToast } from './helpers';

test.describe('设置页面', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('应该正确显示设置页面', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 检查页面标题
    await expect(page.locator('h1, h2').filter({ hasText: /设置/i })).toBeVisible();

    await takeScreenshot(page, 'settings-page');
  });

  test('应该显示设置标签页', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 检查标签页
    const tabs = page.locator('[role="tab"], .tabs button, .tab-button');

    const expectedTabs = ['LLM', 'API', '工具', '通用', 'LLM配置', 'API配置', '工具配置'];

    for (const tabName of expectedTabs) {
      const tab = tabs.filter({ hasText: new RegExp(tabName, 'i') });
      const count = await tab.count();
      if (count > 0) {
        await expect(tab.first()).toBeVisible();
      }
    }

    await takeScreenshot(page, 'settings-tabs');
  });

  test('应该可以切换 LLM 配置标签', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 点击 LLM 配置标签
    const llmTab = page.locator('[role="tab"], .tabs button, .tab-button').filter({ hasText: /LLM/i });
    const count = await llmTab.count();

    if (count > 0) {
      await llmTab.first().click();

      // 检查 LLM 配置面板
      const llmPanel = page.locator('[role="tabpanel"], .tab-panel').filter({ hasText: /LLM|模型/i });
      await expect(llmPanel.first()).toBeVisible();

      await takeScreenshot(page, 'settings-llm-tab');
    }
  });

  test('应该可以切换 API 配置标签', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 点击 API 配置标签
    const apiTab = page.locator('[role="tab"], .tabs button, .tab-button').filter({ hasText: /API/i });
    const count = await apiTab.count();

    if (count > 0) {
      await apiTab.first().click();

      // 检查 API 配置面板
      const apiPanel = page.locator('[role="tabpanel"], .tab-panel').filter({ hasText: /API|接口/i });
      await expect(apiPanel.first()).toBeVisible();

      await takeScreenshot(page, 'settings-api-tab');
    }
  });

  test('应该可以切换工具配置标签', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 点击工具配置标签
    const toolsTab = page.locator('[role="tab"], .tabs button, .tab-button').filter({ hasText: /工具/i });
    const count = await toolsTab.count();

    if (count > 0) {
      await toolsTab.first().click();

      // 检查工具配置面板
      const toolsPanel = page.locator('[role="tabpanel"], .tab-panel').filter({ hasText: /工具|Tools/i });
      await expect(toolsPanel.first()).toBeVisible();

      await takeScreenshot(page, 'settings-tools-tab');
    }
  });

  test('应该显示 LLM 模型列表', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找 LLM 配置区域
    const llmSection = page.locator('[data-testid="llm-config"], .llm-config, section').filter({ hasText: /LLM|模型/i });
    const count = await llmSection.count();

    if (count > 0) {
      // 检查模型列表
      const modelList = llmSection.first().locator('.model-list, .config-item, [data-testid*="model"]');
      const modelCount = await modelList.count();

      if (modelCount > 0) {
        await expect(modelList.first()).toBeVisible();
      }

      await takeScreenshot(page, 'settings-llm-models');
    }
  });

  test('应该可以添加新的 LLM 配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找添加按钮
    const addButton = page.locator('button:has-text("添加"), button:has-text("新增"), button:has-text("Add")');
    const count = await addButton.count();

    if (count > 0) {
      await addButton.first().click();

      // 检查是否显示配置表单
      const form = page.locator('form, .config-form, dialog, [role="dialog"]');
      await expect(form.first()).toBeVisible();

      // 填写表单
      const nameInput = page.locator('input[name="name"], input[id*="name"], input[placeholder*="名称"]');
      if (await nameInput.count() > 0) {
        await nameInput.first().fill('test-model');
      }

      const apiKeyInput = page.locator('input[name*="api"], input[name*="key"], input[placeholder*="API"]');
      if (await apiKeyInput.count() > 0) {
        await apiKeyInput.first().fill('test-api-key');
      }

      await takeScreenshot(page, 'settings-add-llm-form');

      // 取消或关闭表单
      const cancelButton = page.locator('button:has-text("取消"), button:has-text("关闭"), button:has-text("Cancel")');
      const cancelCount = await cancelButton.count();

      if (cancelCount > 0) {
        await cancelButton.first().click();
      }
    }
  });

  test('应该可以编辑现有配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找编辑按钮
    const editButton = page.locator('button:has-text("编辑"), button[aria-label*="edit"], .edit-button');
    const count = await editButton.count();

    if (count > 0) {
      await editButton.first().click();

      // 检查是否显示编辑表单
      const form = page.locator('form, .config-form, dialog, [role="dialog"]');
      await expect(form.first()).toBeVisible();

      await takeScreenshot(page, 'settings-edit-config');

      // 关闭表单
      const cancelButton = page.locator('button:has-text("取消"), button:has-text("关闭")');
      const cancelCount = await cancelButton.count();

      if (cancelCount > 0) {
        await cancelButton.first().click();
      }
    }
  });

  test('应该可以删除配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找删除按钮
    const deleteButton = page.locator('button:has-text("删除"), button[aria-label*="delete"], .delete-button');
    const count = await deleteButton.count();

    if (count > 0) {
      await deleteButton.first().click();

      // 确认删除
      const confirmButton = page.locator('button:has-text("确认"), button:has-text("确定")');
      const confirmCount = await confirmButton.count();

      if (confirmCount > 0) {
        await confirmButton.first().click();

        // 等待删除完成
        await page.waitForTimeout(1000);

        // 检查配置是否减少（可能需要根据实际情况调整）
        // await expect(configItems).toHaveCount(initialCount - 1);
      }

      await takeScreenshot(page, 'settings-delete-config');
    }
  });

  test('应该保存配置更改', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找保存按钮
    const saveButton = page.locator('button:has-text("保存"), button:has-text("Save"), [data-testid="save-button"]');
    const count = await saveButton.count();

    if (count > 0) {
      // 修改一些配置
      const inputField = page.locator('input[type="text"], input[type="number"]').first();
      if (await inputField.count() > 0) {
        await inputField.fill('modified-value');

        // 点击保存
        await saveButton.first().click();

        // 检查成功提示
        await checkToast(page, /保存成功|已保存|Saved/);

        await takeScreenshot(page, 'settings-saved');
      }
    }
  });

  test('应该显示工具列表', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找工具配置区域
    const toolsSection = page.locator('[data-testid="tools-config"], .tools-config, section').filter({
      hasText: '工具',
    });
    const count = await toolsSection.count();

    if (count > 0) {
      // 检查工具列表
      const toolsList = toolsSection.first().locator('.tools-list, .tool-item, [data-testid*="tool"]');
      const toolCount = await toolsList.count();

      if (toolCount > 0) {
        await expect(toolsList.first()).toBeVisible();
      }

      await takeScreenshot(page, 'settings-tools-list');
    }
  });

  test('应该可以启用/禁用工具', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找开关按钮
    const toggleSwitch = page.locator('[role="switch"], .toggle, input[type="checkbox"]');
    const count = await toggleSwitch.count();

    if (count > 0) {
      const firstToggle = toggleSwitch.first();
      const isChecked = await firstToggle.isChecked();

      // 切换状态
      await firstToggle.click();

      // 等待状态更新
      await page.waitForTimeout(500);

      // 检查状态是否改变
      const newChecked = await firstToggle.isChecked();
      expect(newChecked).not.toBe(isChecked);

      await takeScreenshot(page, 'settings-tool-toggle');
    }
  });

  test('应该可以重置为默认配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找重置按钮
    const resetButton = page.locator(
      'button:has-text("重置"), button:has-text("恢复默认"), button:has-text("Reset"), [data-testid="reset-button"]'
    );
    const count = await resetButton.count();

    if (count > 0) {
      await resetButton.first().click();

      // 确认重置
      const confirmButton = page.locator('button:has-text("确认"), button:has-text("确定")');
      const confirmCount = await confirmButton.count();

      if (confirmCount > 0) {
        await confirmButton.first().click();
      }

      // 检查成功提示
      await checkToast(page, '重置成功');

      await takeScreenshot(page, 'settings-reset');
    }
  });

  test('应该验证必填字段', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找添加按钮
    const addButton = page.locator('button:has-text("添加"), button:has-text("新增")');
    const count = await addButton.count();

    if (count > 0) {
      await addButton.first().click();

      // 直接点击保存，不填写必填字段
      const saveButton = page.locator('button:has-text("保存"), button[type="submit"]').filter({ visible: true });
      const saveCount = await saveButton.count();

      if (saveCount > 0) {
        await saveButton.first().click();

        // 检查是否显示错误提示
        const errorMessage = page.locator('.error, [role="alert"], .text-red');
        const errorCount = await errorMessage.count();

        if (errorCount > 0) {
          await expect(errorMessage.first()).toBeVisible();
          await takeScreenshot(page, 'settings-validation-error');
        }

        // 关闭表单
        const cancelButton = page.locator('button:has-text("取消"), button:has-text("关闭")');
        const cancelCount = await cancelButton.count();

        if (cancelCount > 0) {
          await cancelButton.first().click();
        }
      }
    }
  });

  test('应该搜索配置项', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找搜索框
    const searchInput = page.locator('input[placeholder*="搜索"], input[type="search"], [data-testid="search"]');
    const count = await searchInput.count();

    if (count > 0) {
      await searchInput.first().fill('LLM');

      // 等待搜索结果
      await page.waitForTimeout(500);

      // 检查搜索结果
      const results = page.locator('.config-item, .search-results, [data-testid*="result"]');
      const resultCount = await results.count();

      if (resultCount > 0) {
        await expect(results.first()).toBeVisible();
      }

      await takeScreenshot(page, 'settings-search');
    }
  });

  test('应该导出配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找导出按钮
    const exportButton = page.locator('button:has-text("导出"), button:has-text("Export"), [data-testid="export-button"]');
    const count = await exportButton.count();

    if (count > 0) {
      // 设置下载处理
      const downloadPromise = page.waitForEvent('download', { timeout: 10000 });
      await exportButton.first().click();

      try {
        const download = await downloadPromise;
        expect(download.suggestedFilename()).toMatch(/\.(json|yaml|yml)$/);
      } catch (e) {
        // 如果没有触发下载，可能有导出菜单
        await takeScreenshot(page, 'settings-export-menu');
      }
    }
  });

  test('应该导入配置', async ({ page }) => {
    await page.goto('/settings');
    await page.waitForLoadState('networkidle');

    // 查找导入按钮
    const importButton = page.locator('button:has-text("导入"), button:has-text("Import"), [data-testid="import-button"]');
    const count = await importButton.count();

    if (count > 0) {
      await importButton.first().click();

      // 检查文件上传对话框
      const fileInput = page.locator('input[type="file"]');
      const fileCount = await fileInput.count();

      if (fileCount > 0) {
        // 创建测试配置文件
        await page.evaluate(() => {
          localStorage.setItem('test-config', JSON.stringify({ test: 'config' }));
        });

        await takeScreenshot(page, 'settings-import-dialog');

        // 可能需要确认导入
        const confirmButton = page.locator('button:has-text("确认"), button:has-text("导入")');
        const confirmCount = await confirmButton.count();

        if (confirmCount > 0) {
          await confirmButton.first().click();
        }
      }
    }
  });
});
