/**
 * 测试全局设置
 *
 * 在所有测试运行前执行，用于启动测试服务器
 */

import { FullConfig } from '@playwright/test';

async function globalSetup(config: FullConfig) {
  console.log('🚀 开始全局设置...');

  // 设置环境变量
  process.env.NODE_ENV = 'test';
  process.env.VITE_API_BASE_URL = 'http://localhost:8000/api';

  // 可以在这里启动后端测试服务器
  // await startTestServer();

  console.log('✅ 全局设置完成');
}

export default globalSetup;
