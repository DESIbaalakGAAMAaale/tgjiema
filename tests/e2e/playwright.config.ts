import { defineConfig, devices } from '@playwright/test';
import * as path from 'path';
import * as crypto from 'crypto';

/**
 * R44 G0-4 / R47-R49 P0-3: Playwright E2E 测试配置
 *
 * R49 P0-3 整改:
 * - 添加 generateDefaultPasswordHash() 用于本地测试(无 CI 注入 ADMIN_PASSWORD 时)
 * - 确保 ADMIN_PASSWORD 以 PBKDF2 hash 格式传递给 webServer 子进程
 * - admin startup() 在 test 环境自动 bootstrap,webServer 可直接启动
 *
 * 测试目标:
 * - Admin bootstrap 首次初始化
 * - Session 登录/过期/撤销/logout/CSRF
 * - MFA TOTP 启用/验证/break-glass
 * - WCAG 2.2 AA 无障碍（axe-core）
 *
 * webServer 轮询 /readiness 端点,返回 200 表示:
 * - SQLite cache_store 已初始化
 * - admin bootstrap 已完成(R49: startup 自动 bootstrap 兜底)
 */

// R49 P0-3: 生成 PBKDF2 hash(仅由调用方提供 password,不内置默认值)
// 格式: $pbkdf2-sha256$200000$<salt_hex>$<hash_hex>
// 与 admin._verify_password / generate_password_hash 兼容
function generatePasswordHash(password: string): string {
  const salt = crypto.randomBytes(16);
  const hash = crypto.pbkdf2Sync(password, salt, 200000, 32, 'sha256');
  return `$pbkdf2-sha256$200000$${salt.toString('hex')}$${hash.toString('hex')}`;
}

// R56 §6: 测试凭据必须从环境变量获取,禁止固定默认值
// 本地运行请先设置以下环境变量:
//   $env:ADMIN_TEST_PASSWORD="<your_password>"
//   $env:BREAK_GLASS_PASSWORD="<your_password>"
//   $env:SECRET_KEY="<your_secret>"
//   $env:BOT_TOKEN="<your_test_token>"
if (!process.env.ADMIN_TEST_PASSWORD) {
  throw new Error(
    'ADMIN_TEST_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值);' +
    '本地运行请执行: $env:ADMIN_TEST_PASSWORD="<your_test_password>"'
  );
}
if (!process.env.BREAK_GLASS_PASSWORD) {
  throw new Error(
    'BREAK_GLASS_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}
if (!process.env.SECRET_KEY) {
  throw new Error(
    'SECRET_KEY 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}
if (!process.env.BOT_TOKEN) {
  throw new Error(
    'BOT_TOKEN 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}

// R48 P0-3: webServer 必须显式继承的关键环境变量
// R56 §6: 移除所有固定默认密码,缺失即抛错
const WEB_SERVER_ENV: Record<string, string> = {
  // 数据库路径(必须与 bootstrap 步骤一致)
  DATABASE_URL: process.env.DATABASE_URL || 'sqlite://tmp/e2e_default.db',
  // 测试环境标识(跳过 CRDB/Bot 心跳;R49: startup 自动 bootstrap)
  ENVIRONMENT: process.env.ENVIRONMENT || 'test',
  SERVICE_ROLE: process.env.SERVICE_ROLE || 'admin',
  // Admin 凭证(必须与 bootstrap 步骤一致)
  ADMIN_USERNAME: process.env.ADMIN_USERNAME || 'admin',
  // R49 P0-3: ADMIN_PASSWORD 必须是 PBKDF2 hash 格式
  // R56 §6: 若未提供 hash,则从 ADMIN_TEST_PASSWORD 生成 hash(仍是显式输入,无固定默认)
  ADMIN_PASSWORD: process.env.ADMIN_PASSWORD || generatePasswordHash(process.env.ADMIN_TEST_PASSWORD),
  ADMIN_PRINCIPAL_ID: process.env.ADMIN_PRINCIPAL_ID || '1',
  ADMIN_PRINCIPAL_USERNAME: process.env.ADMIN_PRINCIPAL_USERNAME || 'admin',
  ADMIN_PRINCIPAL_BOOTSTRAP_ROLES: process.env.ADMIN_PRINCIPAL_BOOTSTRAP_ROLES || 'super_admin',
  // 安全配置(R56 §6: 必须显式设置)
  SECRET_KEY: process.env.SECRET_KEY,
  BOT_TOKEN: process.env.BOT_TOKEN,
  CSRF_COOKIE_SECURE: process.env.CSRF_COOKIE_SECURE || 'false',
  BREAK_GLASS_PASSWORD: process.env.BREAK_GLASS_PASSWORD,
  // Web 监听地址(必须与 baseURL 端口一致)
  ADMIN_WEB_HOST: process.env.ADMIN_WEB_HOST || '127.0.0.1',
  ADMIN_WEB_PORT: process.env.ADMIN_WEB_PORT || '8080',
};

export default defineConfig({
  // R65 P1-02: testDir 设为 '..'(tests/ 目录),同时包含:
  //   - tests/e2e/*.spec.ts(admin/session/mfa/a11y 行为测试)
  //   - tests/a11y/*.spec.ts(64 个矩阵 stub 测试,确保 executed==64)
  // check_a11y_matrix_enforcement.py 通过 file 路径过滤只计 tests/a11y/ 的测试。
  testDir: '..',
  fullyParallel: false,  // Admin 测试需要串行,避免 session 冲突
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,  // 单 worker,串行执行
  // R47 P0-3: 测试结果输出目录(trace/video/screenshot/axe JSON)
  outputDir: './test-results',
  // R65 P1-02: 启用 JSON reporter 输出固定 artifact
  // - tests/e2e/test-results/a11y-report.json(相对于 playwright.config.ts)
  // - check_a11y_matrix_enforcement.py --test-report 消费此文件做执行对等校验
  // - CI 中 Scanner 9 在 Playwright 运行后强制校验 executed==64, passed==64, skipped==0
  reporter: [
    ['html'],
    ['json', { outputFile: 'test-results/a11y-report.json' }],
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
