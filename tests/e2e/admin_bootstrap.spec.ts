import { test, expect, Page } from '@playwright/test';

/**
 * R44 G0-4 / R47 P0-3: Admin Bootstrap 真实浏览器 E2E 测试
 *
 * 验证:
 * - /readiness 端点返回 200(服务就绪: DB 已初始化 + bootstrap 完成)
 * - bootstrap 后管理员可用 bootstrap 密码登录
 * - 登录成功后重定向到首页(dashboard)
 * - 所有测试使用真实临时 SQLite,不 mock 关键流程
 */

// R47 P0-3: 测试用登录密码(与 ADMIN_BOOTSTRAP_PASSWORD 环境变量对应)
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD || 'test_bootstrap_pw';

test.describe('Admin Bootstrap', () => {
  test('/readiness 返回 200 表示服务就绪', async ({ request }) => {
    // R47 P0-3: readiness 端点真实检查 DB 初始化 + bootstrap 完成
    const response = await request.get('/readiness');
    expect(response.status()).toBe(200);
    const body = await response.json();
    // 测试模式下检查 db_initialized + admin_bootstrap
    expect(body.ready).toBe(true);
    expect(body.checks.db_initialized).toBe(true);
    expect(body.checks.admin_bootstrap).toBe(true);
  });

  test('登录页可访问且包含表单', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    // 登录页应包含用户名/密码输入框和提交按钮
    await expect(page.locator('input[name="username"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
    // CSRF token 隐藏字段应存在
    await expect(page.locator('input[name="csrf_token"]')).toHaveValue(/.+/);
  });

  test('bootstrap 后管理员可用 bootstrap 密码登录', async ({ page }: { page: Page }) => {
    // 使用 bootstrap 密码登录
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');
    // 登录成功后应重定向到首页(/)
    await page.waitForURL('**/', { timeout: 10_000 });
    // 首页应加载成功(不是 /login)
    expect(page.url()).not.toContain('/login');
  });

  test('错误密码登录失败返回 401', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'wrong_password_123');
    await page.click('button[type="submit"]');
    // 应停留在登录页或显示错误(不重定向到 /)
    await page.waitForTimeout(1000);
    expect(page.url()).toContain('/login');
  });
});
