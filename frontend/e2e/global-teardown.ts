/**
 * 测试全局清理
 *
 * 在所有测试运行后执行，用于清理资源
 */

import { FullConfig } from '@playwright/test';

async function globalTeardown(config: FullConfig) {
  console.log('🧹 开始全局清理...');

  // 可以在这里停止测试服务器
  // await stopTestServer();

  console.log('✅ 全局清理完成');
}

export default globalTeardown;
