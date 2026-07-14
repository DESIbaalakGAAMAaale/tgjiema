import { defineConfig, devices } from '@playwright/test';

/**
 * R44 G0-4: Playwright E2E 测试配置
 * R47 P0-3: 修复 webServer readiness 检查 + 临时 SQLite + artifact 保留
 *
 * 测试目标:
 * - Admin bootstrap 首次初始化
 * - Session 登录/过期/撤销/logout/CSRF
 * - MFA TOTP 启用/验证/break-glass
 * - WCAG 2.2 AA 无障碍（axe-core）
 *
 * 前置条件:
 * - Admin Web 服务通过 webServer 自动启动
 * - 临时 SQLite 已由 CI bootstrap 步骤初始化(DATABASE_URL 指定)
 * - admin principal 已 bootstrap(super_admin 角色)
 *
 * webServer 轮询 /readiness 端点,返回 200 表示:
 * - SQLite cache_store 已初始化
 * - admin bootstrap 已完成
 */
export default defineConfig({
  testDir: '.',
  fullyParallel: false,  // Admin 测试需要串行,避免 session 冲突
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,  // 单 worker,串行执行
  // R47 P0-3: 测试结果输出目录(trace/video/screenshot/axe JSON)
  outputDir: './test-results',
  reporter: [
    ['html'],
    ['list'],
  ],
  use: {
    baseURL: process.env.ADMIN_BASE_URL || 'http://127.0.0.1:8080',
    // R47 P0-3: 失败时保留 trace/video/screenshot
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // 忽略 HTTPS 证书错误（测试环境自签证书）
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // R47 P0-3: webServer 自动启动 admin 服务,轮询 /readiness 确认就绪
  // reuseExistingServer: false — 每次 run 新启动,避免缓存污染
  webServer: {
    command: 'python -m uvicorn admin:app --host 127.0.0.1 --port 8080',
    url: 'http://127.0.0.1:8080/readiness',
    timeout: 60_000,
    reuseExistingServer: false,
    // 重试次数:readiness 在服务启动初期可能返回 503(bootstrap 进行中)
    // Playwright 会持续轮询直到返回 2xx 或超时
  },
});
