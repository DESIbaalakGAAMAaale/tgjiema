import { defineConfig, devices } from '@playwright/test';

/**
 * R44 G0-4: Playwright E2E 测试配置
 * 
 * 测试目标:
 * - Admin bootstrap 首次初始化
 * - Session 登录/过期/撤销/logout/CSRF
 * - MFA TOTP 启用/验证/break-glass
 * - WCAG 2.2 AA 无障碍（axe-core）
 * 
 * 前置条件:
 * - Admin Web 服务运行在 http://localhost:8080
 * - 数据库已初始化（migration 已执行）
 * - 测试用 admin 账号已 bootstrap
 */
export default defineConfig({
  testDir: '.',
  fullyParallel: false,  // Admin 测试需要串行,避免 session 冲突
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,  // 单 worker,串行执行
  reporter: [
    ['html'],
    ['list'],
  ],
  use: {
    baseURL: process.env.ADMIN_BASE_URL || 'http://localhost:8080',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // 忽略 HTTPS 证书错误（测试环境自签证书）
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // 不启动 webServer,由 CI 或本地手动启动 admin Web
  // 启动命令: python -m uvicorn admin:app --host 127.0.0.1 --port 8080
  webServer: process.env.CI ? {
    command: 'python -m uvicorn admin:app --host 127.0.0.1 --port 8080',
    port: 8080,
    timeout: 30_000,
    reuseExistingServer: true,
  } : undefined,
});
