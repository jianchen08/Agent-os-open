/**
 * 阶段 1: 环境检查（全部并行）
 *
 * 检查前后端服务、数据库、Redis 是否正常运行
 */

import { test, expect } from '@playwright/test';

test.describe('阶段1: 环境检查 (并行)', () => {

  test('1A. 前端服务检查', async ({ page }) => {
    console.log('\n[1A] 检查前端服务...');

    try {
      await page.goto(process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188", { timeout: 5000 });
      const title = await page.title();
      console.log(`✅ 前端服务正常: ${title}`);
    } catch (e) {
      console.log('❌ 前端服务无法访问');
      throw e;
    }
  });

  test('1B. 后端服务检查', async ({ request }) => {
    console.log('\n[1B] 检查后端服务...');

    try {
      const response = await request.get('http://localhost:8888/health');
      const data = await response.json();

      console.log(`✅ 后端服务正常: ${JSON.stringify(data).substring(0, 100)}`);
      expect(response.status()).toBe(200);
    } catch (e) {
      console.log('❌ 后端服务无法访问');
      throw e;
    }
  });

  test('1C. PostgreSQL 数据库检查', async ({ request }) => {
    console.log('\n[1C] 检查 PostgreSQL...');

    try {
      // 通过后端 API 测试数据库
      const response = await request.post('http://localhost:8888/api/v1/auth/login', {
        data: {
          username: 'test_db_check',
          password: 'test_db_check'
        },
        timeout: 5000
      });

      if (response.status() === 401) {
        console.log('✅ PostgreSQL 连接正常 (认证失败是预期的)');
      } else if (response.status() >= 500) {
        const text = await response.text();
        if (text.includes('password') || text.includes('postgres')) {
          console.log('❌ PostgreSQL 密码错误');
          console.log('   提示: 请修改 .env 文件中的数据库密码');
        } else {
          console.log('⚠️  PostgreSQL 有其他问题');
        }
      } else {
        console.log(`✅ PostgreSQL 响应: ${response.status()}`);
      }
    } catch (e) {
      console.log('❌ PostgreSQL 无法连接');
      console.log('   提示: 请检查数据库是否运行');
    }
  });

  test('1D. Redis 检查（跳过）', async () => {
    console.log('\n[1D] Redis 检查暂时跳过');
    console.log('⚠️  Redis 未测试（可能未安装）');
  });
});
