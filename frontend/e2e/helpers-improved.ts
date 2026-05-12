/**
 * 改进的测试辅助函数
 *
 * 针对登录超时、认证失败问题进行了优化
 * 增加了重试机制、自动注册和更健壮的错误处理
 *
 * 第二轮修复主要改进：
 * 1. 增加登录超时时间（10s -> 30s）
 * 2. 优化注册逻辑，先检查用户是否存在
 * 3. 增强认证错误处理和 token 验证
 * 4. 添加更详细的调试日志
 */

import { Page } from '@playwright/test';

/**
 * 测试用户凭据
 */
export const testUser = {
  username: 'admin',
  password: 'admin123456',
  email: 'admin@example.com',
};

/**
 * 登录配置
 */
const loginConfig = {
  apiBaseUrl: 'http://localhost:8888/api/v1',
  timeout: 30000, // 增加到 30 秒
  retryDelay: 2000,
  defaultMaxRetries: 5, // 增加重试次数
};

/**
 * 登录结果接口
 */
interface LoginResult {
  success: boolean;
  attempts: number;
  error?: string;
  registered?: boolean; // 是否进行了注册
}

/**
 * 改进的快速登录函数（带重试机制和自动注册）
 *
 * @param page Playwright Page 对象
 * @param username 用户名（可选，默认使用测试账号）
 * @param password 密码（可选，默认使用测试账号）
 * @param maxRetries 最大重试次数（默认 5 次）
 * @returns Promise<LoginResult> 登录结果
 *
 * @example
 * // 使用默认测试账号登录
 * const result = await quickLoginImproved(page);
 * if (!result.success) {
 *   console.error('登录失败:', result.error);
 * }
 *
 * // 使用自定义账号登录
 * await quickLoginImproved(page, 'myuser', 'mypass', 5);
 */
export async function quickLoginImproved(
  page: Page,
  username = testUser.username,
  password = testUser.password,
  maxRetries = loginConfig.defaultMaxRetries
): Promise<LoginResult> {
  console.log(`\n========== 开始登录流程 ==========`);
  console.log(`用户: ${username}`);
  console.log(`最大重试次数: ${maxRetries}`);
  console.log(`超时时间: ${loginConfig.timeout}ms`);

  let userRegistered = false;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      console.log(`\n[尝试 ${attempt}/${maxRetries}] 开始登录...`);
      console.log(`当前时间: ${new Date().toISOString()}`);

      // 步骤 1: 确保在正确的页面上
      await ensurePageContext(page);

      // 步骤 2: 检查是否已登录
      const isLoggedIn = await checkLoginStatus(page);
      if (isLoggedIn) {
        console.log('✓ 已登录，跳过登录步骤');
        return { success: true, attempts: attempt, registered: userRegistered };
      }

      // 步骤 3: 确保测试用户存在（首次尝试或之前注册失败时）
      if (attempt === 1 || !userRegistered) {
        console.log('→ 确保测试用户存在...');
        const ensureResult = await ensureTestUser(page, username, password, testUser.email);
        if (ensureResult.registered) {
          userRegistered = true;
          console.log('✓ 测试用户已创建/已存在');
        }
      }

      // 步骤 4: 使用 API 登录（更快更稳定）
      console.log('→ 使用 API 登录...');
      const loginResult = await loginViaAPIImproved(page, username, password);

      if (!loginResult.success) {
        throw new Error(loginResult.error || 'API 登录失败');
      }

      // 步骤 5: 验证登录状态
      console.log('→ 验证登录状态...');
      const isVerified = await verifyLoginSuccess(page);

      if (!isVerified) {
        throw new Error('登录验证失败：Token 未保存到 localStorage');
      }

      // 步骤 6: 等待页面稳定
      console.log('→ 等待页面稳定...');
      await page.waitForTimeout(1000);

      console.log(`✓ 登录成功（尝试 ${attempt} 次）`);
      console.log(`========== 登录完成 ==========\n`);

      return { success: true, attempts: attempt, registered: userRegistered };

    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      console.error(`✗ 尝试 ${attempt} 失败:`, errorMessage);

      // 如果是最后一次尝试，抛出错误
      if (attempt === maxRetries) {
        console.error(`\n========== 登录失败 ==========`);
        console.error(`已重试 ${maxRetries} 次，仍然失败`);
        console.error(`最后错误: ${errorMessage}`);
        console.error(`============================\n`);

        return {
          success: false,
          attempts: maxRetries,
          error: errorMessage,
          registered: userRegistered
        };
      }

      // 等待后重试
      console.log(`→ 等待 ${loginConfig.retryDelay / 1000} 秒后重试...`);
      await page.waitForTimeout(loginConfig.retryDelay);

      // 清理状态后重试
      try {
        await cleanupLoginState(page);
      } catch (e) {
        console.warn('清理登录状态时出错:', e);
      }
    }
  }

  // 理论上不会到达这里，但 TypeScript 需要返回
  return {
    success: false,
    attempts: maxRetries,
    error: '未知错误',
    registered: userRegistered
  };
}

/**
 * 确保页面在正确的上下文中
 *
 * 如果在 about:blank 或其他无效页面，导航到首页
 */
async function ensurePageContext(page: Page): Promise<void> {
  try {
    const currentUrl = page.url();
    if (currentUrl === 'about:blank' || !currentUrl.includes('localhost')) {
      console.log('  → 页面上下文无效，导航到首页...');
      await page.goto('/', { timeout: loginConfig.timeout, waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('domcontentloaded', { timeout: loginConfig.timeout }).catch(() => {
        console.warn('  ⚠ 等待页面加载超时，继续执行');
      });
    }
  } catch (error) {
    console.warn('  ⚠ 确保页面上下文时出错:', error);
  }
}

/**
 * 检查登录状态
 *
 * 修复：确保在正确的页面上下文中访问 localStorage，避免 SecurityError
 */
async function checkLoginStatus(page: Page): Promise<boolean> {
  try {
    // 确保不在 about:blank 页面上
    await ensurePageContext(page);

    return await page.evaluate(() => {
      try {
        const token = localStorage.getItem('access_token');
        const user = localStorage.getItem('user');
        return !!(token && user);
      } catch (error) {
        // localStorage 访问失败（例如跨域）
        console.warn('localStorage 访问失败:', error);
        return false;
      }
    });
  } catch (error) {
    console.warn('检查登录状态时出错:', error);
    return false;
  }
}

/**
 * 确保测试用户存在
 *
 * 如果用户不存在则注册，如果已存在则跳过
 * 使用更健壮的错误处理逻辑
 */
async function ensureTestUser(
  page: Page,
  username: string,
  password: string,
  email: string
): Promise<{ registered: boolean; exists: boolean }> {
  console.log('  → 检查测试用户是否存在...');

  try {
    await ensurePageContext(page);

    const result = await page.evaluate(
      async ({ username, password, email, apiBaseUrl, timeout }) => {
        try {
          // 首先尝试登录来检查用户是否存在
          const loginResponse = await fetch(`${apiBaseUrl}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
          });

          // 如果登录成功，用户已存在
          if (loginResponse.ok) {
            console.log('  → 用户已存在（登录成功）');
            return { registered: false, exists: true };
          }

          // 如果是 401 错误，用户可能不存在
          if (loginResponse.status === 401) {
            console.log('  → 用户不存在，尝试注册...');

            // 尝试注册
            const registerResponse = await fetch(`${apiBaseUrl}/auth/register`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username, password, email }),
            });

            if (registerResponse.ok) {
              console.log('  → 注册成功');
              return { registered: true, exists: true };
            }

            // 如果返回 400，可能是用户已存在
            if (registerResponse.status === 400) {
              const text = await registerResponse.text();
              let errorData;
              try {
                errorData = JSON.parse(text);
              } catch {
                errorData = { detail: text };
              }

              // 检查是否是"用户已存在"错误
              if (errorData.detail?.includes('已存在') || errorData.detail?.includes('already exists')) {
                console.log('  → 用户已存在（注册返回 400）');
                return { registered: false, exists: true };
              }

              return {
                registered: false,
                exists: false,
                error: errorData.detail || '注册失败'
              };
            }

            const regText = await registerResponse.text();
            return {
              registered: false,
              exists: false,
              error: `注册失败: ${registerResponse.status} - ${regText}`
            };
          }

          const loginText = await loginResponse.text();
          return {
            registered: false,
            exists: false,
            error: `登录检查失败: ${loginResponse.status} - ${loginText}`
          };

        } catch (error) {
          return {
            registered: false,
            exists: false,
            error: error instanceof Error ? error.message : String(error)
          };
        }
      },
      { username, password, email, apiBaseUrl: loginConfig.apiBaseUrl, timeout: loginConfig.timeout }
    );

    if (result.error) {
      console.warn('  ⚠ 确保测试用户时出错:', result.error);
      // 不抛出错误，允许继续尝试登录
    }

    return { registered: result.registered || false, exists: result.exists || false };

  } catch (error) {
    console.warn('  ⚠ 确保测试用户时出错:', error);
    // 不抛出错误，允许继续尝试登录
    return { registered: false, exists: false };
  }
}

/**
 * 通过 API 登录（改进版 - 增强认证错误处理）
 */
async function loginViaAPIImproved(
  page: Page,
  username: string,
  password: string
): Promise<{ success: boolean; error?: string }> {
  try {
    await ensurePageContext(page);

    // 使用浏览器上下文执行登录
    console.log('  → 发送登录请求...');
    console.log(`     用户: ${username}`);
    console.log(`     API: ${loginConfig.apiBaseUrl}/auth/login`);

    const response = await page.evaluate(
      async ({ username, password, email, apiBaseUrl, timeout }) => {
        try {
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), timeout);

          try {
            // 尝试登录
            let res = await fetch(`${apiBaseUrl}/auth/login`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username, password }),
              signal: controller.signal,
            });

            clearTimeout(timeoutId);

            // 如果登录失败（401），尝试注册
            if (res.status === 401 || res.status === 422) {
              console.log('  → 登录失败 (401/422)，尝试注册用户...');

              const regRes = await fetch(`${apiBaseUrl}/auth/register`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, email }),
              });

              if (regRes.ok) {
                console.log('  → 注册成功，重新登录...');
                res = await fetch(`${apiBaseUrl}/auth/login`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ username, password }),
                });
              } else if (regRes.status === 400) {
                // 可能是用户已存在，继续尝试登录
                console.log('  → 注册返回 400（可能用户已存在），继续尝试登录...');
              }
            }

            const text = await res.text();

            if (!text) {
              return { success: false, error: `空响应 (status: ${res.status})` };
            }

            let data;
            try {
              data = JSON.parse(text);
            } catch {
              return { success: false, error: `无效的 JSON: ${text.substring(0, 200)}` };
            }

            if (res.ok && data.access_token) {
              // 保存认证信息
              localStorage.setItem('access_token', data.access_token);
              localStorage.setItem('refresh_token', data.refresh_token || '');
              localStorage.setItem('token', data.access_token);

              // 计算过期时间
              const expiryTime = Date.now() + (data.expires_in || 7200) * 1000;
              localStorage.setItem('access_token_expiry', expiryTime.toString());

              if (data.user) {
                localStorage.setItem('user', JSON.stringify(data.user));
                localStorage.setItem('auth_user', JSON.stringify(data.user));
              }

              // 验证存储是否成功
              const storedToken = localStorage.getItem('access_token');
              const storedUser = localStorage.getItem('user');

              if (!storedToken || !storedUser) {
                return { success: false, error: 'Token 保存失败' };
              }

              console.log('  → 认证信息已保存到 localStorage');
              return { success: true, data };
            }

            return {
              success: false,
              error: data.detail || data.message || `登录失败 (status: ${res.status})`
            };

          } catch (fetchError) {
            clearTimeout(timeoutId);
            if (fetchError.name === 'AbortError') {
              throw new Error(`请求超时 (${timeout}ms)`);
            }
            throw fetchError;
          }

        } catch (error) {
          return {
            success: false,
            error: error instanceof Error ? error.message : String(error)
          };
        }
      },
      { username, password, email: testUser.email, apiBaseUrl: loginConfig.apiBaseUrl, timeout: loginConfig.timeout }
    );

    if (!response.success) {
      console.error('  ✗ API 登录失败:', response.error);
      return {
        success: false,
        error: response.error || '登录请求失败'
      };
    }

    console.log('  ✓ API 登录成功');

    // 导航到首页以应用登录状态
    console.log('  → 导航到首页应用登录状态...');
    await page.goto('/', { timeout: loginConfig.timeout, waitUntil: 'domcontentloaded' });

    // 等待页面更新
    try {
      await page.waitForLoadState('domcontentloaded', { timeout: loginConfig.timeout });
    } catch {
      console.warn('  ⚠ 等待页面加载超时，继续执行');
    }

    return { success: true };

  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    console.error('  ✗ 登录过程出错:', errorMessage);
    return {
      success: false,
      error: errorMessage
    };
  }
}

/**
 * 验证登录成功（增强版 - 更全面的 token 验证）
 *
 * 修复：确保在正确的页面上下文中访问 localStorage
 * 增强：验证 token 有效性、用户信息完整性
 */
async function verifyLoginSuccess(page: Page): Promise<boolean> {
  try {
    await ensurePageContext(page);

    // 等待页面加载完成
    try {
      await page.waitForLoadState('domcontentloaded', { timeout: loginConfig.timeout });
    } catch {
      console.warn('  ⚠ 等待页面加载超时，继续验证');
    }

    // 详细验证 token 和用户信息
    const verification = await page.evaluate(() => {
      try {
        const token = localStorage.getItem('access_token');
        const userStr = localStorage.getItem('user');
        const tokenExpiry = localStorage.getItem('access_token_expiry');

        // 检查 token 是否存在
        if (!token) {
          return { valid: false, reason: 'access_token 不存在' };
        }

        // 检查用户信息是否存在
        if (!userStr) {
          return { valid: false, reason: 'user 信息不存在' };
        }

        // 验证用户信息是否有效 JSON
        let user;
        try {
          user = JSON.parse(userStr);
        } catch {
          return { valid: false, reason: 'user 信息不是有效的 JSON' };
        }

        // 检查用户信息必需字段
        if (!user.id && !user.username) {
          return { valid: false, reason: 'user 信息缺少必需字段' };
        }

        // 检查 token 是否过期
        if (tokenExpiry) {
          const expiryTime = parseInt(tokenExpiry, 10);
          if (Date.now() >= expiryTime) {
            return { valid: false, reason: 'token 已过期' };
          }
        }

        return {
          valid: true,
          token: token.substring(0, 20) + '...',
          username: user.username,
          reason: '验证通过'
        };
      } catch (error) {
        return {
          valid: false,
          reason: error instanceof Error ? error.message : String(error)
        };
      }
    });

    if (!verification.valid) {
      console.warn(`  ⚠ 登录验证失败: ${verification.reason}`);
      return false;
    }

    console.log('  ✓ 登录验证成功');
    console.log(`     Token: ${verification.token}`);
    console.log(`     用户: ${verification.username}`);

    return true;

  } catch (error) {
    console.error('  ✗ 验证登录状态时出错:', error);
    return false;
  }
}

/**
 * 清理登录状态（增强版）
 *
 * 修复：确保在正确的页面上下文中访问 localStorage，避免 SecurityError
 * 增强：更彻底的清理，包括所有认证相关的存储项
 */
async function cleanupLoginState(page: Page): Promise<void> {
  try {
    await ensurePageContext(page);

    // 尝试等待页面加载
    try {
      await page.waitForLoadState('domcontentloaded', { timeout: loginConfig.timeout });
    } catch {
      // 继续执行
    }

    await page.evaluate(() => {
      try {
        // 清理所有认证相关的 localStorage 项
        const authKeys = [
          'access_token',
          'refresh_token',
          'token',
          'user',
          'auth_user',
          'access_token_expiry',
          'token_expiry'
        ];

        authKeys.forEach(key => {
          localStorage.removeItem(key);
        });

        // 也清理 sessionStorage
        sessionStorage.clear();
      } catch (error) {
        console.warn('清理存储时出错:', error);
      }
    });
    console.log('  ✓ 清理登录状态完成');
  } catch (error) {
    console.warn('  ⚠ 清理登录状态时出错:', error);
  }
}

/**
 * 增强的等待页面加载函数
 */
export async function waitForPageStable(page: Page, timeout = loginConfig.timeout): Promise<void> {
  console.log('→ 等待页面稳定...');

  try {
    // 等待 DOM 内容加载
    await page.waitForLoadState('domcontentloaded', { timeout });

    // 等待网络空闲（带容错）
    await page.waitForLoadState('networkidle', { timeout }).catch(() => {
      console.warn('  ⚠ 网络未完全空闲，继续执行');
    });

    // 额外等待确保动画完成
    await page.waitForTimeout(500);

    console.log('  ✓ 页面已稳定');
  } catch (error) {
    console.warn('  ⚠ 等待页面稳定时出错:', error);
  }
}

/**
 * 增强的导航函数
 */
export async function navigateTo(
  page: Page,
  url: string,
  options: { timeout?: number; waitForStable?: boolean } = {}
): Promise<void> {
  const { timeout = loginConfig.timeout, waitForStable = true } = options;

  console.log(`→ 导航到: ${url}`);

  await page.goto(url, { timeout, waitUntil: 'domcontentloaded' });

  if (waitForStable) {
    await waitForPageStable(page, timeout);
  }

  console.log(`  ✓ 导航完成`);
}

/**
 * 检查 API 可用性（增强版）
 *
 * 修复：确保在正确的页面上下文中执行 fetch 请求
 * 增强：更详细的错误信息和超时控制
 */
export async function checkAPIAvailability(page: Page): Promise<boolean> {
  try {
    await ensurePageContext(page);

    const response = await page.evaluate(async (apiBaseUrl) => {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);

        try {
          const res = await fetch(`${apiBaseUrl}/themes`, {
            method: 'GET',
            signal: controller.signal,
          });

          clearTimeout(timeoutId);
          return { ok: res.ok, status: res.status };
        } catch (fetchError) {
          clearTimeout(timeoutId);
          if (fetchError.name === 'AbortError') {
            return { ok: false, error: '请求超时' };
          }
          throw fetchError;
        }
      } catch (error) {
        return { ok: false, error: String(error) };
      }
    }, loginConfig.apiBaseUrl);

    if (response.ok) {
      console.log('✓ 后端 API 可用');
      return true;
    } else {
      console.warn('⚠ 后端 API 不可用:', response.error || `status: ${response.status}`);
      return false;
    }
  } catch (error) {
    console.error('✗ 检查 API 可用性时出错:', error);
    return false;
  }
}

/**
 * 完整的测试环境设置（增强版）
 */
export async function setupTestEnvironment(page: Page): Promise<void> {
  console.log('\n========== 设置测试环境 ==========');

  // 1. 检查 API 可用性
  const apiAvailable = await checkAPIAvailability(page);
  if (!apiAvailable) {
    throw new Error('后端 API 不可用，请确保后端服务正在运行');
  }

  // 2. 清理旧状态
  console.log('→ 清理旧状态...');
  await cleanupLoginState(page);

  // 3. 登录
  console.log('→ 执行登录...');
  const loginResult = await quickLoginImproved(page);

  if (!loginResult.success) {
    throw new Error(`登录失败: ${loginResult.error}`);
  }

  console.log('========== 测试环境就绪 ==========\n');
}

/**
 * 执行登录的简化函数（用于测试中的直接调用）
 *
 * 这是一个更简单的登录函数，专门用于测试文件中直接调用
 * 不需要重试机制，适合在单个测试中快速登录
 */
export async function performLogin(page: Page): Promise<boolean> {
  try {
    const result = await quickLoginImproved(page);
    return result.success;
  } catch (error) {
    console.error('登录失败:', error);
    return false;
  }
}

/**
 * 导出原始函数以保持兼容性
 */
export { login, logout, takeScreenshot } from './helpers';

/**
 * 导出改进版本的函数
 */
export const quickLogin = quickLoginImproved; // 使用改进版本

/**
 * 导出类型和常量
 */
export type { LoginResult };
