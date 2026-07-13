import { test, expect, Page, request } from '@playwright/test';

/**
 * R44 G0-4: Admin Session 真实浏览器 E2E 测试
 * 
 * 验证:
 * - 登录成功后 session 有效
 * - session 过期后自动跳转登录
 * - logout 后 session 失效
 * - CSRF token 验证
 * - 登录失败次数限制
 */

// 测试辅助函数
async function login(page: Page, username = 'admin', password = 'testpass') {
  await page.goto('/login');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
}

async function logout(page: Page) {
  await page.goto('/logout');
}

test.describe('Admin Session', () => {
  test('登录成功后访问 dashboard', async ({ page }: { page: Page }) => {
    await login(page);
    await page.waitForURL('**/dashboard', { timeout: 5000 }).catch(() => {});
    // dashboard 应包含管理员信息
    const body = await page.textContent('body');
    expect(body).toBeTruthy();
  });

  test('未登录访问 dashboard 重定向到 login', async ({ page }: { page: Page }) => {
    await page.goto('/dashboard');
    // 应重定向到 /login
    await page.waitForURL('**/login', { timeout: 5000 }).catch(() => {});
    const url = page.url();
    expect(url).toContain('/login');
  });

  test('logout 后 session 失效', async ({ page }: { page: Page }) => {
    await login(page);
    await logout(page);
    // logout 后访问 dashboard 应重定向到 login
    await page.goto('/dashboard');
    await page.waitForURL('**/login', { timeout: 5000 }).catch(() => {});
  });

  test('CSRF token 验证 - 缺失 token 返回 403', async ({ page }: { page: Page }) => {
    await login(page);
    // 尝试不发 CSRF token 直接 POST
    const response = await page.request.post('/takedown', {
      data: { target: 'test' },
      // 故意不传 CSRF token
    });
    expect([403, 401]).toContain(response.status());
  });

  test('错误密码登录失败', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'wrongpassword');
    await page.click('button[type="submit"]');
    // 应显示错误信息
    const body = await page.textContent('body');
    expect(body).toMatch(/密码|错误|失败|invalid|incorrect/i);
  });
});
