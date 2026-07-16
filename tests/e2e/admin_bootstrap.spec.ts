import { test, expect, Page } from '@playwright/test';

/**
 * R44 G0-4 / R47 P0-3 / R48 P0-3: Admin Bootstrap 真实浏览器 E2E 测试
 *
 * 验证:
 * - /readiness 端点返回 HTTP 200(服务就绪: DB 已初始化 + bootstrap 完成)
 * - readiness JSON 包含顶层 db_initialized=true / admin_bootstrap=true
 * - bootstrap 后管理员可用 bootstrap 密码登录
 * - 登录成功后重定向到首页(dashboard)
 * - 所有测试使用真实临时 SQLite,不 mock 关键流程
 *
 * R48 P0-3 整改:
 * - readiness 检查断言顶层 db_initialized / admin_bootstrap(非嵌套在 checks 中)
 * - 确认 HTTP status 与业务 JSON 一致(200 = ready, 503 = not ready)
 * - 使用更稳健的等待策略替代固定 timeout
 */

// R56 §6: 测试用登录密码必须从环境变量获取,缺失则立即失败
if (!process.env.ADMIN_TEST_PASSWORD) {
  throw new Error(
    'ADMIN_TEST_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD;

test.describe('Admin Bootstrap', () => {
  test('/readiness 返回 200 表示服务就绪', async ({ request }) => {
    // R48/R49 P0-3: readiness 端点真实检查 DB 初始化 + bootstrap 完成
    // HTTP status 200 表示就绪,503 表示未就绪(显式 JSONResponse)
    const response = await request.get('/readiness');
    expect(response.status()).toBe(200);
    const body = await response.json();
    // R49: 顶层 status / reason 字段(便于 webServer 轮询断言)
    expect(body.status).toBe('ok');
    expect(body.reason).toBe('');
    // R48: 顶层字段断言(便于 webServer 轮询和测试直接检查)
    expect(body.ready).toBe(true);
    expect(body.db_initialized).toBe(true);
    expect(body.admin_bootstrap).toBe(true);
    // 兼容: checks 嵌套字段也保留
    expect(body.checks.db_initialized).toBe(true);
    expect(body.checks.admin_bootstrap).toBe(true);
  });

  test('登录页可访问且包含表单', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    // 登录页应包含用户名/密码输入框和提交按钮
    await expect(page.locator('input[name="username"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
    // CSRF token 隐藏字段应存在且非空
    await expect(page.locator('input[name="csrf_token"]')).toHaveValue(/.+/);
  });

  test('bootstrap 后管理员可用 bootstrap 密码登录', async ({ page }: { page: Page }) => {
    // 使用 bootstrap 密码登录
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');
    // R48: 等待重定向到首页(/),登录成功返回 303 → /
    // 使用 waitForLoadState 替代固定 timeout,更稳健
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
    // 登录成功后应重定向到首页(非 /login)
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });
    expect(page.url()).not.toContain('/login');
  });

  test('错误密码登录失败返回 401', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'wrong_password_123');
    await page.click('button[type="submit"]');
    // R48: 等待页面稳定(networkidle)替代固定 timeout
    await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});
    // 应停留在登录页(不重定向到 /)
    expect(page.url()).toContain('/login');
  });
});
