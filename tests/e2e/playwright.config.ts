import { defineConfig, devices } from '@playwright/test';
import * as path from 'path';

/**
 * R44 G0-4: Playwright E2E 测试配置
 * R47 P0-3: 修复 webServer readiness 检查 + 临时 SQLite + artifact 保留
 * R48 P0-3: 显式传递 DATABASE_URL 等环境变量给 webServer,确保 bootstrap 与
 *           webServer 连接同一 SQLite 文件;webServer.url=/readiness 仅在
 *           HTTP 2xx(就绪)时继续测试。
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

// R48 P0-3: webServer 必须显式继承的关键环境变量
// Playwright webServer.env 会覆盖父进程环境,因此需要把 CI 注入的关键变量
// 显式传递,避免 webServer 启动的 uvicorn 进程读到错误的默认值。
// DATABASE_URL 尤为关键 — 决定 bootstrap 与 webServer 是否连接同一 SQLite 文件。
const WEB_SERVER_ENV: Record<string, string> = {
  // 数据库路径(必须与 bootstrap 步骤一致)
  DATABASE_URL: process.env.DATABASE_URL || 'sqlite://tmp/e2e_default.db',
  // 测试环境标识(跳过 CRDB/Bot 心跳)
  ENVIRONMENT: process.env.ENVIRONMENT || 'test',
  SERVICE_ROLE: process.env.SERVICE_ROLE || 'admin',
  // Admin 凭证(必须与 bootstrap 步骤一致)
  ADMIN_USERNAME: process.env.ADMIN_USERNAME || 'admin',
  ADMIN_PASSWORD: process.env.ADMIN_PASSWORD || '',
  ADMIN_PRINCIPAL_ID: process.env.ADMIN_PRINCIPAL_ID || '1',
  ADMIN_PRINCIPAL_USERNAME: process.env.ADMIN_PRINCIPAL_USERNAME || 'admin',
  ADMIN_PRINCIPAL_BOOTSTRAP_ROLES: process.env.ADMIN_PRINCIPAL_BOOTSTRAP_ROLES || 'super_admin',
  // 安全配置
  SECRET_KEY: process.env.SECRET_KEY || 'test_secret_key_for_e2e_only',
  BOT_TOKEN: process.env.BOT_TOKEN || 'test_token',
  CSRF_COOKIE_SECURE: process.env.CSRF_COOKIE_SECURE || 'false',
  BREAK_GLASS_PASSWORD: process.env.BREAK_GLASS_PASSWORD || 'test_bootstrap_pw',
  // Web 监听地址(必须与 baseURL 端口一致)
  ADMIN_WEB_HOST: process.env.ADMIN_WEB_HOST || '127.0.0.1',
  ADMIN_WEB_PORT: process.env.ADMIN_WEB_PORT || '8080',
};

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
    // R48 P0-3: BASE_URL 必须与 ADMIN_WEB_HOST:ADMIN_WEB_PORT 一致
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
  // R47/R48 P0-3: webServer 自动启动 admin 服务,轮询 /readiness 确认就绪
  // reuseExistingServer: false — 每次 run 新启动,避免缓存污染
  // timeout: 60_000 — 给 bootstrap + init_db 足够时间
  // url=/readiness — 在 HTTP 2xx(就绪)时才继续执行测试
  // R48 P0-3: cwd 必须指向项目根目录,否则 uvicorn 无法 import admin 模块
  //           (admin/ 在项目根,不在 tests/e2e/)
  webServer: {
    command: 'python -m uvicorn admin:app --host 127.0.0.1 --port 8080',
    url: 'http://127.0.0.1:8080/readiness',
    timeout: 60_000,
    reuseExistingServer: false,
    // R48 P0-3: cwd 指向项目根目录(playwright.config.ts 在 tests/e2e/,
    //           项目根在两级之上),确保 uvicorn 能 import admin 模块
    cwd: path.resolve(__dirname, '..', '..'),
    // R48 P0-3: 显式传递关键环境变量给 webServer 子进程
    // 确保 uvicorn 读到与 bootstrap 步骤相同的 DATABASE_URL
    // 注意: 必须展开 process.env,否则子进程会丢失 PATH/HOME 等基础变量
    env: { ...process.env, ...WEB_SERVER_ENV } as Record<string, string>,
  },
});
