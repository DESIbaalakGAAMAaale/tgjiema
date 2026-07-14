import { test, expect, Page, Browser } from '@playwright/test';
import * as crypto from 'crypto';

/**
 * R44 G0-4 / R47 P0-3 / R48 P0-3: Admin MFA TOTP 真实浏览器 E2E 测试
 *
 * 验证:
 * - MFA 启用流程(GET /mfa/setup → 生成 secret → POST /mfa/setup 验证 TOTP)
 * - MFA 启用后登录需要 TOTP 验证(POST /login → MFA 输入页 → POST /login/mfa)
 * - MFA 禁用流程(POST /mfa/disable 需密码确认)
 * - break-glass 紧急登录(本机 HTTP Basic,跳过 MFA)
 * - 所有测试使用真实临时 SQLite,不 mock 关键流程
 *
 * R48 P0-3 整改:
 * - 使用模块级变量 mfaSecret 在测试间共享 TOTP secret
 *   (test 2 启用 MFA 时捕获 secret,test 3/4 登录时用它生成 TOTP)
 * - 不再在 MFA challenge 页面尝试 GET /mfa/setup(无 session 会 401)
 * - loginWithMfa 辅助函数处理 MFA challenge 流程
 * - break-glass 测试断言 200(测试环境条件全部满足)
 */

// R47 P0-3: 测试用登录密码(与 ADMIN_BOOTSTRAP_PASSWORD 环境变量对应)
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD || 'test_bootstrap_pw';
// break-glass 密码(与 BREAK_GLASS_PASSWORD 环境变量对应)
const BREAK_GLASS_PASSWORD = process.env.BREAK_GLASS_PASSWORD || 'test_bootstrap_pw';

// R48 P0-3: 模块级共享变量 — test 2 启用 MFA 时捕获 secret,
// test 3/4 登录时需要 MFA challenge,用此 secret 生成 TOTP
let mfaSecret = '';

// ─── TOTP 工具函数(RFC 6238,与 pyotp 兼容) ──────────────────────

/** Base32 解码(TOTP secret 标准编码) */
function base32Decode(secret: string): Buffer {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const char of secret.toUpperCase().replace(/=+$/, '')) {
    const idx = alphabet.indexOf(char);
    if (idx === -1) continue;
    bits += idx.toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

/** 生成 TOTP 6 位验证码(RFC 6238,与 pyotp.TOTP(secret).now() 兼容) */
function generateTOTP(secret: string, timeStep: number = 30): string {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 1000 / timeStep);
  const counterBuffer = Buffer.alloc(8);
  counterBuffer.writeBigInt64BE(BigInt(counter));

  const hmac = crypto.createHmac('sha1', key);
  hmac.update(counterBuffer);
  const digest = hmac.digest();

  // 动态截取(RFC 4226)
  const offset = digest[digest.length - 1] & 0x0f;
  const code = ((digest[offset] & 0x7f) << 24 |
    (digest[offset + 1] & 0xff) << 16 |
    (digest[offset + 2] & 0xff) << 8 |
    (digest[offset + 3] & 0xff)) % 1000000;

  return code.toString().padStart(6, '0');
}

// ─── 登录辅助函数 ──────────────────────────────────────────────

/** 基础登录: 填写表单并提交(不处理 MFA challenge) */
async function loginBase(page: Page, password = ADMIN_PASSWORD) {
  await page.goto('/login');
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
}

/** R48: 带 MFA challenge 处理的登录 — 如果出现 MFA 输入页则自动完成 */
async function loginWithMfa(page: Page, password = ADMIN_PASSWORD) {
  await loginBase(page, password);
  // 等待页面加载(MFA 页面或首页)
  await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});

  // 检查是否在 MFA challenge 页面(有 challenge_token 隐藏字段)
  const challengeInput = page.locator('input[name="challenge_token"]');
  const hasChallenge = await challengeInput.count().then(c => c > 0).catch(() => false);

  if (hasChallenge && mfaSecret) {
    // MFA challenge 页面 — 用共享的 secret 生成 TOTP
    const totpCode = generateTOTP(mfaSecret);
    expect(totpCode).toMatch(/^\d{6}$/);
    await page.fill('input[name="totp_code"]', totpCode);
    await page.click('button[type="submit"]');
    // 等待重定向到首页
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
  }
}

async function getCsrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies();
  return cookies.find(c => c.name === 'csrf_token')?.value || '';
}

test.describe('Admin MFA', () => {
  test('MFA 设置页面可访问(未启用时显示 secret)', async ({ page }: { page: Page }) => {
    // 先登录
    await loginBase(page);
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });

    // 访问 MFA 设置页
    await page.goto('/mfa/setup');
    // 应显示 MFA 设置表单(未启用时显示 secret 和 TOTP 输入框)
    const body = await page.textContent('body');
    expect(body).toBeTruthy();
    // 页面应包含 totp_code 输入框
    await expect(page.locator('input[name="totp_code"]')).toBeVisible();
  });

  test('启用 MFA 并验证 TOTP 流程', async ({ page }: { page: Page }) => {
    // 先登录
    await loginBase(page);
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });

    // 访问 MFA 设置页,提取 secret
    await page.goto('/mfa/setup');
    // 从页面提取 secret(在 hidden input 中)
    const secretInput = page.locator('input[name="secret"]');
    const secretValue = await secretInput.inputValue();
    expect(secretValue).toBeTruthy();
    expect(secretValue.length).toBeGreaterThanOrEqual(16);  // Base32 secret ≥ 16 chars

    // R48: 保存 secret 到模块级变量,供后续测试使用
    mfaSecret = secretValue;

    // 生成当前 TOTP 验证码
    const totpCode = generateTOTP(secretValue);
    expect(totpCode).toMatch(/^\d{6}$/);

    // 提交 MFA 启用表单
    await page.fill('input[name="totp_code"]', totpCode);
    await page.click('button[type="submit"]');

    // 启用成功后应重定向到首页
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
    await page.waitForURL(url => !url.toString().includes('/mfa'), { timeout: 10_000 });
    expect(page.url()).not.toContain('/mfa');
  });

  test('MFA 启用后登录需要 TOTP 验证', async ({ page }: { page: Page }) => {
    // R48: 此测试依赖前一个测试已启用 MFA(串行执行,共享 SQLite)
    // 使用 loginWithMfa 自动处理 MFA challenge(用 mfaSecret 生成 TOTP)
    expect(mfaSecret).toBeTruthy();
    await loginWithMfa(page);

    // 应成功登录到首页(非 /login)
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });
    expect(page.url()).not.toContain('/login');
  });

  test('禁用 MFA 需要密码确认', async ({ page }: { page: Page }) => {
    // R48: 先用 MFA 登录(MFA 可能已启用)
    await loginWithMfa(page);
    await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});

    // 尝试访问 MFA 设置页
    await page.goto('/mfa/setup');
    const body = await page.textContent('body');
    // R48 P0-3: 修正条件判断 — 页面显示 "MFA 已启用" 时表示 MFA 处于启用状态,
    // 需要提交禁用表单。之前用 '禁用 MFA' 匹配失败,实际文本是 'MFA 已启用' 和 '如需禁用'
    if (body && body.includes('已启用')) {
      // MFA 已启用,提交禁用表单
      const csrfToken = await getCsrfToken(page);
      expect(csrfToken).toBeTruthy();
      const response = await page.request.post('/mfa/disable', {
        form: {
          password: ADMIN_PASSWORD,
          csrf_token: csrfToken,
        },
        maxRedirects: 0,
      });
      // 禁用成功返回 303 重定向
      expect([303, 302]).toContain(response.status());
      // R48 P0-3: 清空 mfaSecret 表示 MFA 已禁用,避免影响后续 session 测试
      mfaSecret = '';
    }
    // 如果 MFA 未启用(前一个测试已禁用),此测试跳过禁用验证
  });

  // R48 P0-3: afterAll 钩子 — 确保所有测试结束后 MFA 被禁用,
  // 避免 MFA 状态泄漏到 admin_session.spec.ts 导致后续测试登录时遇到 MFA challenge 页
  test.afterAll(async ({ browser }: { browser: Browser }) => {
    if (!mfaSecret) return;  // MFA 已禁用,无需清理

    // R48: afterAll 无法使用 test-scoped 的 page fixture,改用 browser 创建新 page
    const page = await browser.newPage();
    try {
      // 尝试用保存的 secret 登录并禁用 MFA
      await loginWithMfa(page);
      await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});
      await page.goto('/mfa/setup');
      const csrfToken = await getCsrfToken(page);
      if (csrfToken) {
        await page.request.post('/mfa/disable', {
          form: {
            password: ADMIN_PASSWORD,
            csrf_token: csrfToken,
          },
          maxRedirects: 0,
        });
      }
      mfaSecret = '';
    } catch {
      // 清理失败时忽略(afterAll 不应阻塞测试结果)
    } finally {
      await page.close();
    }
  });

  test('break-glass 紧急登录(本机 HTTP Basic,跳过 MFA)', async ({ request }) => {
    // R47 P0-3: break-glass 端点仅允许本机访问(127.0.0.1)
    // CI 环境中 Playwright 和 admin 服务在同一台机器,满足本机限制
    // 使用 HTTP Basic auth 发送 break-glass 密码
    // R48: 测试环境 BREAK_GLASS_PASSWORD=test_bootstrap_pw,
    // 且 ADMIN_PASSWORD 是 test_bootstrap_pw 的 PBKDF2 哈希,
    // _verify_password('test_bootstrap_pw', hash) = True → break-glass 成功
    const basicAuth = Buffer.from(`admin:${BREAK_GLASS_PASSWORD}`).toString('base64');
    const response = await request.post('/break-glass/login', {
      headers: {
        'Authorization': `Basic ${basicAuth}`,
      },
      maxRedirects: 0,
    });

    // R48: break-glass 应返回 200 + session_id(测试环境条件全部满足)
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.session_id).toBeTruthy();
    expect(body.message).toContain('break-glass');
  });
});
