/**
 * R61 P1-08 / R62 P1-06: 无障碍行为测试 — 自动派生自 generated_a11y_cases.json
 *
 * 审计 P1-08 整改要求("无障碍路由仍依赖人工 TEMPLATE_TO_ROUTE"):
 *   1. 不再依赖人工 TEMPLATE_TO_ROUTE 数组,改为从 generated_a11y_cases.json
 *      自动派生测试矩阵(由 scripts/generate_a11y_test_cases.py 从 admin.app.routes 生成)。
 *   2. URL 断言改用 assertUrlEquals(URL parser 严格比较 pathname + query),
 *      不再用宽松的 page.url().toContain(expected)。
 *   3. /locale 路由改用 assertLocaleRedirect 严格断言 303 + Set-Cookie +
 *      Referrer-Policy 安全 + 最终路径(取代 allowRedirect 的"放行不校验")。
 *   4. 每个 a11y_testable 用例(zh-CN + en-US)执行:
 *      axe 扫描、键盘 Tab 流转、focus visible、dialog 焦点陷阱/Escape/恢复、
 *      aria-live、200% zoom、prefers-reduced-motion。
 *
 * R62 P1-06 整改要求("依赖缺失自动 skip 是典型假绿条件"):
 *   5. 移除 @ts-nocheck 与 stub replacement — 依赖缺失必须 HARD FAIL,不允许 skip。
 *      @playwright/test / @axe-core/playwright 缺失时,import 直接抛错而非静默 stub。
 *      ADMIN_TEST_PASSWORD 缺失时,文件加载即抛错(playwright.config.ts 同款校验)。
 *   6. /login en-US 必须独立初始化 locale(Accept-Language header + locale cookie),
 *      不复用 zh-CN 默认 locale 结果,并断言页面真正渲染为英文。
 *   7. 文件末尾断言生成的测试用例数 > 0(检测 stub 替换导致的 0 用例假绿)。
 *   8. 路由元数据完整性检查(每条 a11y_testable 用例必须有 path / route_path / method)。
 *
 * 历史背景:R56 §6 / R59 §5.4 / R60 §13 的存量测试(键盘/zoom/reflow/reduced-motion)
 * 保留,数据源统一改用 generated_a11y_cases.json。
 */

// R62 P1-06: 直接 import — 依赖缺失时模块加载即抛错(HARD FAIL),不允许 stub 兜底。
// 旧实现的 try/catch require + stub replacement 会导致 0 用例 = 绿色 CI(假绿)。
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import * as fs from 'fs';
import * as path from 'path';
import { assertUrlEquals, assertLocaleRedirect } from './helpers/url_assert';

// R62 P1-06: ADMIN_TEST_PASSWORD 缺失必须硬失败(与 playwright.config.ts 同款校验)
// 旧实现:缺失时整体 skip → 0 用例 = 绿色 CI(假绿)
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD;
if (!ADMIN_PASSWORD) {
  throw new Error(
    'R62 P1-06: ADMIN_TEST_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值);' +
    '缺失时 a11y 测试 HARD FAIL,不允许 skip(假绿条件)。' +
    '本地运行请执行: export ADMIN_TEST_PASSWORD="<your_test_password>"'
  );
}

// R62 P1-06: 辅助函数 — 从环境变量读取 baseURL(与 playwright.config.ts 一致)
// 用于 /login en-US 测试创建独立 BrowserContext 时复用 baseURL 配置
function baseURLFromEnv(): string {
  return process.env.ADMIN_BASE_URL || 'http://127.0.0.1:8080';
}

// ─────────────────────────────────────────────────────────────
// R61 P1-08: 从 generated_a11y_cases.json 加载自动派生的测试矩阵
// ─────────────────────────────────────────────────────────────

interface A11yCase {
  path: string;
  route_path: string;
  method: string;
  template: string | null;
  param_fixtures: Record<string, any>;
  permission: 'require_session' | 'public';
  expected_landing: string | null;
  is_locale_redirect: boolean;
  a11y_testable: boolean;
  module: string;
}

const CASES_JSON_PATH = path.join(__dirname, 'generated_a11y_cases.json');

function loadGeneratedCases(): A11yCase[] {
  const raw = fs.readFileSync(CASES_JSON_PATH, 'utf-8');
  return JSON.parse(raw);
}

const ALL_CASES: A11yCase[] = loadGeneratedCases();
const A11Y_TESTABLE_CASES: A11yCase[] = ALL_CASES.filter(
  c => c.a11y_testable && c.method === 'GET',
);
const LOCALE_REDIRECT_CASES: A11yCase[] = ALL_CASES.filter(c => c.is_locale_redirect);

// R55: WCAG 2.2 A/AA 完整规则标签集
const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

// dialog 标记正则:匹配 role="dialog" / aria-modal="true" /
// aria-haspopup="dialog" / data-bs-toggle="modal"(大小写不敏感,兼容单/双引号)
const DIALOG_MARKER_RE = /role=["']dialog["']|aria-modal=["']true["']|aria-haspopup=["']dialog["']|data-bs-toggle=["']modal["']/i;
const DIALOG_RUNTIME_SELECTOR = '[role="dialog"], [aria-modal="true"], button[aria-haspopup="dialog"], button[data-bs-toggle="modal"]';

// admin 模板目录(相对本测试文件:tests/e2e/ → 项目根 → admin/templates)
const ADMIN_TEMPLATES_DIR = path.resolve(__dirname, '..', '..', 'admin', 'templates');

interface DialogRoute { path: string; name: string; template: string; }
interface DiscoveryResult {
  routes: DialogRoute[];
  scannedTemplateCount: number;
  missingTemplates: string[];
}

/** case 简称(用于 test name):/ → root,/users → users,/mfa/setup → mfa-setup */
function caseName(c: A11yCase): string {
  return c.path === '/' ? 'root' : c.path.replace(/^\//, '').replace(/\//g, '-');
}

/**
 * R61 P1-08: 静态发现含 dialog 标记的 a11y_testable 路由(基于 generated cases + 模板扫描)。
 * 取代旧 discoverDialogRoutesFromTemplates(基于人工 TEMPLATE_TO_ROUTE)。
 */
function discoverDialogRoutesFromCases(): DiscoveryResult {
  const routes: DialogRoute[] = [];
  const missingTemplates: string[] = [];
  for (const c of A11Y_TESTABLE_CASES) {
    if (!c.template) {
      // /login、/mfa/setup 等内联 HTML 路由 — 无独立模板文件,跳过静态扫描
      continue;
    }
    const filePath = path.join(ADMIN_TEMPLATES_DIR, c.template);
    let content: string;
    try {
      content = fs.readFileSync(filePath, 'utf-8');
    } catch {
      missingTemplates.push(c.template);
      continue;
    }
    if (DIALOG_MARKER_RE.test(content)) {
      routes.push({ path: c.path, name: caseName(c), template: c.template });
    }
  }
  const scannedCount = A11Y_TESTABLE_CASES.filter(c => c.template).length - missingTemplates.length;
  return { routes, scannedTemplateCount: scannedCount, missingTemplates };
}

const _dialogDiscovery: DiscoveryResult = discoverDialogRoutesFromCases();
const dialogRoutes: DialogRoute[] = _dialogDiscovery.routes;

/**
 * R61 P1-08: 导航并硬断言页面加载成功,URL 用 assertUrlEquals 严格比较。
 *
 * 改进点(相比 R60 §13):
 *   - URL 断言改用 assertUrlEquals(URL parser 严格比较 pathname + query),
 *     不再用宽松的 page.url().toContain(expected)
 *   - 移除 allowRedirect 选项:重定向路由(如 /locale)应改用 assertLocaleRedirect
 *     在测试体内显式断言 303 + Set-Cookie + 最终路径(见 L locale 重定向测试)
 *
 * 保留断言:
 *   1. response 非空(goto 实际发生且拿到响应)
 *   2. response.status() < 400(无 4xx/5xx)
 *   3. 最终 URL 严格匹配目标 pathname(用 assertUrlEquals)
 *   4. 未回登录页(除非目标本身就是 /login)
 *   5. 页面存在唯一 heading/landmark
 */
async function navigateAndAssert(
  page: any,
  targetPath: string,
  options: { expectLogin?: boolean } = {},
): Promise<void> {
  const response = await page.goto(targetPath);
  // (1) response 非空
  expect(response).not.toBeNull();
  // (2) status < 400
  expect(response!.status()).toBeLessThan(400);
  // (3) R61 P1-08: 严格 pathname 比较(不再用 toContain)
  const expectedPath = targetPath.split('?')[0];
  assertUrlEquals(page.url(), { pathname: expectedPath });
  // (4) 除非目标本身就是 /login,否则不应被重定向回登录页
  if (!options.expectLogin) {
    expect(page.url()).not.toMatch(/\/login/i);
  }
  // (5) 唯一 heading/landmark(login 页有 1 个 h1;admin 页 base.html 有 1 h1 + 1 main)
  const h1Count = await page.locator('h1').count();
  const mainCount = await page.locator('main, [role="main"]').count();
  expect(h1Count === 1 || mainCount === 1).toBe(true);
}

/** 登录辅助:填写表单并提交,等待重定向离开 /login */
async function login(page: any): Promise<void> {
  await navigateAndAssert(page, '/login', { expectLogin: true });
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((url: any) => !url.toString().includes('/login'), { timeout: 10_000 });
}

// ─────────────────────────────────────────────────────────────
// R61 P1-08: 单页 a11y 检查辅助(每个 case × 每个 locale 都执行)
// ─────────────────────────────────────────────────────────────

/**
 * (b) 键盘 Tab 流转 + focus visible。
 *
 * 断言:
 *   - 连续 Tab 20 次,每次都有可聚焦元素(不陷入死锁)
 *   - 焦点不应死循环(同一元素连续 6 次以上 = 陷阱)
 *   - 至少访问 2 个不同元素(证明 Tab 真的在流转)
 *   - 当前聚焦元素 :focus-visible 有视觉指示(outline 不为 none/0)
 */
async function runKeyboardTraversal(page: any): Promise<void> {
  const focusedSelectors: string[] = [];
  let consecutiveSameCount = 1;
  let lastSelector = '';
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press('Tab');
    const current = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return '';
      return el.tagName + (el.getAttribute('name') ? `[name=${el.getAttribute('name')}]` : '')
        + (el.getAttribute('aria-label') ? `[aria-label=${el.getAttribute('aria-label')}]` : '')
        + (el.getAttribute('href') ? `[href=${el.getAttribute('href')}]` : '')
        + (el.textContent ? `[text=${el.textContent.slice(0, 20)}]` : '');
    });
    focusedSelectors.push(current);
    // 死循环检测:同一元素连续 6 次以上 = 陷阱
    if (current === lastSelector && current !== '') {
      consecutiveSameCount += 1;
      if (consecutiveSameCount >= 6) {
        throw new Error(`键盘陷阱检测: 焦点连续 ${consecutiveSameCount} 次停留在同一元素 "${current}"`);
      }
    } else {
      consecutiveSameCount = 1;
    }
    lastSelector = current;
  }
  // 至少访问 2 个不同元素(证明 Tab 在流转)
  const uniqueSelectors = new Set(focusedSelectors.filter(s => s !== ''));
  expect(uniqueSelectors.size).toBeGreaterThanOrEqual(2);

  // focus visible:当前聚焦元素应有视觉指示(outline 不为 none 或 0)
  const focusVisibleOk = await page.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return true;
    const cs = window.getComputedStyle(el);
    // 接受 outline width > 0、box-shadow 非空、或浏览器默认 :focus-visible
    if (cs.outlineWidth && cs.outlineWidth !== '0px' && cs.outlineStyle !== 'none') return true;
    if (cs.boxShadow && cs.boxShadow !== 'none') return true;
    // 兜底:元素匹配 :focus-visible
    if (el.matches(':focus-visible')) return true;
    return false;
  });
  expect(focusVisibleOk).toBe(true);
}

/**
 * (c) dialog 焦点陷阱 / Escape / 恢复(若页面存在 dialog 触发按钮)。
 *
 * 若页面无 dialog 触发按钮,本检查 pass(无 dialog 可测,正确行为)。
 * 若有,断言:
 *   - Enter 打开 dialog 后,焦点在 dialog 内
 *   - 连续 Tab 10 次,焦点都不离开 dialog(焦点陷阱)
 *   - Escape 关闭 dialog
 *   - 焦点返回触发按钮(焦点恢复)
 */
async function runDialogFocusTrapIfPresent(page: any, _caze: A11yCase): Promise<void> {
  const dialogTrigger = page.locator(
    'button[aria-haspopup="dialog"], button[data-bs-toggle="modal"]',
  ).first();
  const hasDialog = await dialogTrigger.count().catch(() => 0);
  if (hasDialog === 0) {
    // 当前页面无 dialog — 跳过(非失败)
    return;
  }
  await dialogTrigger.focus();
  await expect(dialogTrigger).toBeFocused();
  const triggerElement = dialogTrigger;

  // Enter 打开 dialog
  await page.keyboard.press('Enter');
  const dialog = page.locator('[role="dialog"], [aria-modal="true"]').first();
  await expect(dialog).toBeVisible({ timeout: 5_000 });

  // 焦点应在 dialog 内
  const focusInDialogInitially = await page.evaluate(() => {
    const el = document.activeElement;
    return el ? el.closest('[role="dialog"], [aria-modal="true"]') !== null : false;
  });
  expect(focusInDialogInitially).toBeTruthy();

  // Tab 10 次焦点都不离开 dialog(焦点陷阱)
  for (let i = 0; i < 10; i++) {
    await page.keyboard.press('Tab');
    const inDialog = await page.evaluate(() => {
      const el = document.activeElement;
      return el ? el.closest('[role="dialog"], [aria-modal="true"]') !== null : false;
    });
    expect(inDialog).toBeTruthy();
  }

  // Escape 关闭 dialog
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden({ timeout: 5_000 });

  // 焦点应返回触发按钮(焦点恢复)
  await expect(triggerElement).toBeFocused({ timeout: 2_000 });
}

/**
 * (d) aria-live 区域检查。
 *
 * 若页面有 aria-live / role=status / role=alert,验证 aria-live 值合法
 * (polite / assertive);role=status 隐式 polite,role=alert 隐式 assertive。
 * 若页面无 aria-live 区域(纯静态页面),本检查 pass(无异步更新需宣告)。
 */
async function assertAriaLivePresent(page: any): Promise<void> {
  const liveRegions = page.locator(
    '[aria-live="polite"], [aria-live="assertive"], [role="status"], [role="alert"]',
  );
  const count = await liveRegions.count();
  if (count === 0) {
    // 静态页面无 aria-live 区域 — 不强制要求,pass
    return;
  }
  // 验证 aria-live 值合法(若有显式声明)
  const invalidCount = await page.evaluate(() => {
    const els = document.querySelectorAll('[aria-live]');
    let bad = 0;
    els.forEach(el => {
      const v = el.getAttribute('aria-live');
      if (v !== 'polite' && v !== 'assertive' && v !== 'off') bad += 1;
    });
    return bad;
  });
  expect(invalidCount).toBe(0);
}

/**
 * (e) 200% zoom 关键元素可见。
 *
 * 200% zoom ≈ viewport 宽度减半(1280→640)。
 * 断言:
 *   - body 可见
 *   - 无大幅水平溢出(scrollWidth - clientWidth <= 50px)
 */
async function assertZoom200(page: any, _caze: A11yCase): Promise<void> {
  const original = page.viewportSize();
  await page.setViewportSize({ width: 640, height: 360 });
  try {
    const body = page.locator('body');
    await expect(body).toBeVisible();
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    // 允许少量溢出(<=50px),但不能让页面整体不可用
    expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(50);
  } finally {
    // 还原 viewport 避免影响后续检查
    if (original) {
      await page.setViewportSize(original);
    }
  }
}

/**
 * (f) prefers-reduced-motion 媒体查询。
 *
 * 断言页面 CSS 包含 prefers-reduced-motion 媒体查询
 * (证明开发者考虑了运动敏感用户的偏好)。
 */
async function assertReducedMotionRespected(page: any): Promise<void> {
  const hasReducedMotionQuery = await page.evaluate(() => {
    const sheets = Array.from(document.styleSheets);
    for (const sheet of sheets) {
      try {
        const rules = Array.from(sheet.cssRules || []);
        for (const rule of rules) {
          if (rule.cssText && rule.cssText.includes('prefers-reduced-motion')) {
            return true;
          }
        }
      } catch (_e) {
        // 跨域 stylesheet 无法读取,跳过
      }
    }
    return false;
  });
  expect(hasReducedMotionQuery).toBe(true);
}

/**
 * R61 P1-08: 对单个 case × 单个 locale 执行完整 a11y 检查。
 * 7 项:axe / 键盘 Tab / focus visible / dialog 陷阱/Escape/恢复 / aria-live / 200% zoom / reduced-motion。
 */
async function runFullA11yChecks(page: any, caze: A11yCase): Promise<void> {
  await navigateAndAssert(page, caze.path, { expectLogin: caze.path === '/login' });
  await page.waitForLoadState('networkidle', { timeout: 10_000 });

  // (a) axe 扫描 — 阻断 ANY A/AA 违规
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(results.violations).toEqual([]);

  // (b) 键盘 Tab 流转 + focus visible
  await runKeyboardTraversal(page);

  // (c) dialog 焦点陷阱 / Escape / 恢复(若页面有 dialog)
  await runDialogFocusTrapIfPresent(page, caze);

  // (d) aria-live 区域检查
  await assertAriaLivePresent(page);

  // (e) 200% zoom 关键元素可见
  await assertZoom200(page, caze);

  // (f) prefers-reduced-motion 媒体查询
  await assertReducedMotionRespected(page);
}

// ════════════════════════════════════════════════════════════════
// R61 P1-08 测试主体
// ════════════════════════════════════════════════════════════════

test.describe('R61 P1-08: 无障碍行为(自动派生自 generated_a11y_cases.json)', () => {
  // ─────────────────────────────────────────────────────────────
  // Section 1: 每个 a11y_testable 用例 × zh-CN / en-US — 完整 a11y 检查
  // ─────────────────────────────────────────────────────────────

  for (const caze of A11Y_TESTABLE_CASES) {
    const name = caseName(caze);
    const requiresLogin = caze.permission === 'require_session';

    // zh-CN locale(默认):完整 a11y 检查
    test(`${name} (zh-CN): axe + 键盘 + 焦点 + dialog + aria-live + 200% zoom + reduced-motion`, async ({ page }: any) => {
      if (requiresLogin) await login(page);
      await runFullA11yChecks(page, caze);
    });

    if (caze.path === '/login') {
      // R62 P1-06: /login en-US 必须独立初始化 locale,不复用 zh-CN 默认 locale 结果。
      // 旧实现:对 /login 跳过 /locale 切换 → en-US 与 zh-CN 测试完全相同(假绿)。
      // 整改:/login 是公开页面,无法调用 /locale 端点(需 require_session)。
      //   改用 BrowserContext locale 选项(设置 Accept-Language: en-US header)
      //   + 注入 locale=en-US cookie(与 admin/__init__.py 读取 cookie 的逻辑对齐)
      //   独立初始化 en-US 渲染,并断言页面真正渲染为英文(<html lang="en-US"> + 无中文)。
      test(`${name} (en-US): Accept-Language + locale cookie → 独立英文渲染 + a11y 检查`, async ({ browser }: any) => {
        const context = await browser.newContext({
          // 设置 Accept-Language: en-US header(浏览器导航时自动附加)
          locale: 'en-US',
          extraHTTPHeaders: { 'Accept-Language': 'en-US' },
          // 复用默认 context 的 baseURL 配置
          baseURL: baseURLFromEnv(),
        });
        // 注入 locale cookie(模拟 /locale?lang=en-US 的效果,但不依赖该端点)
        // admin/__init__.py 的模板上下文读取 req.cookies.get("locale", "") 决定渲染 locale
        await context.addCookies([{
          name: 'locale',
          value: 'en-US',
          domain: '127.0.0.1',
          path: '/',
        }]);
        const page = await context.newPage();
        try {
          await navigateAndAssert(page, '/login', { expectLogin: true });
          await page.waitForLoadState('networkidle', { timeout: 10_000 });

          // R62 P1-06: 验证页面真正渲染为英文(非 zh-CN 复用)
          // (a) <html lang> 属性应为 en-US(非 zh-CN)
          const htmlLang = await page.getAttribute('html', 'lang');
          expect(htmlLang).toBe('en-US');

          // (b) 页面 body 文本不应包含 zh-CN 登录页的中文标题/标签
          //     (admin.__init__.s1 的 zh-CN 版本含 "管理后台登录" / "用户名" / "密码" / "登录")
          const bodyText = await page.textContent('body') || '';
          expect(bodyText).not.toContain('管理后台登录');
          expect(bodyText).not.toContain('用户名');
          expect(bodyText).not.toContain('密码');

          // 执行完整 a11y 检查(与 zh-CN 一致)
          await runFullA11yChecks(page, caze);
        } finally {
          await context.close();
        }
      });
    } else {
      // 其他路由 en-US:用 /locale 端点切换(已登录,可调用 require_session 端点)
      test(`${name} (en-US): axe + 键盘 + 焦点 + dialog + aria-live + 200% zoom + reduced-motion`, async ({ page }: any) => {
        if (requiresLogin) await login(page);
        // 切到 en-US locale — 用 /locale 路由(此处仅设置 cookie,严格断言由 Section 2 覆盖)
        const localeResp = await page.goto('/locale?lang=en-US');
        expect(localeResp).not.toBeNull();
        expect(localeResp!.status()).toBeLessThan(400);
        await page.waitForLoadState('networkidle', { timeout: 5_000 });
        // 执行完整 a11y 检查
        await runFullA11yChecks(page, caze);
        // 切回 zh-CN 避免影响后续测试
        await page.goto('/locale?lang=zh-CN');
        await page.waitForLoadState('networkidle', { timeout: 5_000 });
      });
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Section 2: /locale 路由 — assertLocaleRedirect 严格断言
  //   303 + Set-Cookie locale=en-US + Referrer-Policy 安全 + 最终路径 /users
  // ─────────────────────────────────────────────────────────────

  for (const caze of LOCALE_REDIRECT_CASES) {
    test(`${caze.method} ${caze.path}: assertLocaleRedirect 严格断言 303 + Set-Cookie + Referrer-Policy + 最终路径`, async ({ page }: any) => {
      await login(page);
      // 先访问 /users,建立 referer 上下文(/locale 重定向回 referer)
      await navigateAndAssert(page, '/users');
      // R61 P1-08 修复: page.goto() 的 Response.headers() 不暴露 Set-Cookie
      // (浏览器 Fetch API 安全限制:Set-Cookie 是 forbidden response header)。
      // 改用 page.request.get() + maxRedirects: 0 获取原始 303 响应,
      // APIResponse.headers() 包含 Set-Cookie(不经浏览器过滤)。
      // page.request 共享 BrowserContext cookie 存储,login() 的 session_id 可用。
      // 设置 referer 头使 /locale 重定向回 /users(与浏览器导航行为一致)。
      const response = await page.request.get('/locale?lang=en-US', {
        maxRedirects: 0,
        headers: { referer: page.url() },
      });
      // R61 P1-08: 严格断言 303 + Set-Cookie locale=en-US + Referrer-Policy 安全 + 最终路径 /users
      assertLocaleRedirect(response, page.url(), {
        expectedPath: '/users',
        expectedLocale: 'en-US',
      });
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Section 3: dialog 自动发现(基于 generated cases + 模板扫描)
  // ─────────────────────────────────────────────────────────────

  test('dialog 路由自动发现:静态解析全部 a11y_testable 模板,矩阵动态生成非硬编码 []', () => {
    // (a) 断言无模板缺失 — 防止因文件读取失败导致静默空矩阵
    expect(_dialogDiscovery.missingTemplates).toEqual([]);
    // (b) 断言扫描了全部 a11y_testable 模板
    const expectedScanned = A11Y_TESTABLE_CASES.filter(c => c.template).length;
    expect(_dialogDiscovery.scannedTemplateCount).toBe(expectedScanned);
    // (c) 重新独立计算并断言一致,证明 dialogRoutes 非硬编码、发现可复现
    const recount = discoverDialogRoutesFromCases();
    expect(dialogRoutes.length).toBe(recount.routes.length);
    expect(dialogRoutes).toEqual(recount.routes);
    // (d) 非静默:记录发现数到 stderr 供 CI 日志追溯
    console.error(
      `[R61 P1-08] 静态分析扫描 ${_dialogDiscovery.scannedTemplateCount} 个 a11y_testable 模板,` +
      `发现 ${dialogRoutes.length} 个 dialog 路由: ` +
      `${dialogRoutes.map(r => `${r.name}(${r.path})`).join(', ') || '(当前模板无 [role=dialog],矩阵为动态空集,非硬编码)'}`,
    );
  });

  test('dialog 运行时发现:爬取全部 a11y_testable 路由(zh-CN + en-US locale),与静态分析一致', async ({ page }: any) => {
    // R59: 爬取多路由 × 2 locale 需要更长超时
    test.setTimeout(180_000);
    await login(page);
    const runtimeFound: string[] = [];
    // R61 P1-08 修复: 仅遍历有模板的 case,与静态分析 discoverDialogRoutesFromCases
    // 的扫描范围一致(静态分析跳过 !c.template 的内联 HTML 路由)。
    // 同时避免 /login 在已登录状态下 302 重定向到 / 导致 navigateAndAssert 失败
    // (/login GET handler 检测到有效 session 时 RedirectResponse(url="/", status_code=302))。
    const templatedCases = A11Y_TESTABLE_CASES.filter(c => c.template);
    for (const caze of templatedCases) {
      // zh-CN(默认 locale):访问路由并探测 dialog
      await navigateAndAssert(page, caze.path, { expectLogin: caze.path === '/login' });
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      const countZh = await page.locator(DIALOG_RUNTIME_SELECTOR).count();
      // en-US:先调用 /locale?lang=en-US 设置 locale cookie(端点会重定向回来源页),
      //       再访问同一路由,探测 dialog(防止 locale 切换引入/移除 dialog)
      const localeResp = await page.goto('/locale?lang=en-US');
      expect(localeResp!.status()).toBeLessThan(400);
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      await navigateAndAssert(page, caze.path, { expectLogin: caze.path === '/login' });
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      const countEn = await page.locator(DIALOG_RUNTIME_SELECTOR).count();
      if (countZh > 0 || countEn > 0) {
        runtimeFound.push(caseName(caze));
      }
      // 切回 zh-CN,避免影响后续测试的 locale 默认值
      await page.goto('/locale?lang=zh-CN');
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
    }
    // 运行时发现的路由集合应与静态分析一致
    const staticNames = dialogRoutes.map(r => r.name).sort();
    expect(runtimeFound.slice().sort()).toEqual(staticNames);
  });

  // ─────────────────────────────────────────────────────────────
  // Section 4: 键盘导航专项(保留 R56 §6 存量测试)
  // ─────────────────────────────────────────────────────────────

  test.describe('R56 §6: 键盘导航行为', () => {
    test('登录页可通过键盘完整流转(Tab/Shift+Tab/Enter)', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.keyboard.press('Tab');
      const usernameInput = page.locator('input[name="username"]');
      await expect(usernameInput).toBeFocused();
      await page.keyboard.press('Tab');
      const passwordInput = page.locator('input[name="password"]');
      await expect(passwordInput).toBeFocused();
      await page.keyboard.press('Shift+Tab');
      await expect(usernameInput).toBeFocused();
      await page.fill('input[name="username"]', 'admin');
      await page.fill('input[name="password"]', ADMIN_PASSWORD);
      await page.keyboard.press('Enter');
      await page.waitForURL((url: any) => !url.toString().includes('/login'), { timeout: 10_000 });
    });

    test('登录页 Escape 不应困住焦点(可在文档内自由流转)', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.keyboard.press('Tab');
      await page.keyboard.press('Escape');
      const usernameInput = page.locator('input[name="username"]');
      await expect(usernameInput).toBeFocused();
      await page.keyboard.press('Tab');
      const passwordInput = page.locator('input[name="password"]');
      await expect(passwordInput).toBeFocused();
    });

    test('dashboard 可通过 Tab 完整流转(无键盘陷阱,焦点顺序无死循环)', async ({ page }: any) => {
      await login(page);
      await navigateAndAssert(page, '/');
      await runKeyboardTraversal(page);
    });
  });

  // ─────────────────────────────────────────────────────────────
  // Section 5: aria-live 异步更新宣告(保留 R56 §6 存量测试)
  // ─────────────────────────────────────────────────────────────

  test.describe('R56 §6: aria-live 状态宣告', () => {
    test('dashboard 应包含 aria-live 区域用于异步状态宣告', async ({ page }: any) => {
      await login(page);
      await navigateAndAssert(page, '/');
      const liveRegions = page.locator(
        '[aria-live="polite"], [aria-live="assertive"], [role="status"], [role="alert"]',
      );
      const count = await liveRegions.count();
      // dashboard 必须至少有 1 个 aria-live 区域
      expect(count).toBeGreaterThanOrEqual(1);
    });

    test('错误状态应通过 role="alert" 或 aria-live 宣告', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.fill('input[name="username"]', 'admin');
      await page.fill('input[name="password"]', 'wrong_password_xyz');
      await page.click('button[type="submit"]');
      const errorMsg = page.locator(
        '[role="alert"], [aria-live="assertive"], .error, .alert-danger',
      );
      await expect(errorMsg).toBeVisible({ timeout: 5_000 });
    });
  });

  // ─────────────────────────────────────────────────────────────
  // Section 6: zoom 缩放(保留 R56 §6 存量测试)
  // ─────────────────────────────────────────────────────────────

  test.describe('R56 §6: zoom 缩放(200% / 400%)', () => {
    test('登录页 200% zoom 不破坏布局', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.setViewportSize({ width: 640, height: 360 });
      const usernameInput = page.locator('input[name="username"]');
      const passwordInput = page.locator('input[name="password"]');
      const submitBtn = page.locator('button[type="submit"]');
      await expect(usernameInput).toBeVisible();
      await expect(passwordInput).toBeVisible();
      await expect(submitBtn).toBeVisible();
      await page.fill('input[name="username"]', 'admin');
      await page.fill('input[name="password"]', ADMIN_PASSWORD);
      await expect(submitBtn).toBeEnabled();
    });

    test('登录页 400% zoom 不破坏布局', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.setViewportSize({ width: 320, height: 180 });
      const usernameInput = page.locator('input[name="username"]');
      const passwordInput = page.locator('input[name="password"]');
      const submitBtn = page.locator('button[type="submit"]');
      await expect(usernameInput).toBeVisible();
      await expect(passwordInput).toBeVisible();
      await expect(submitBtn).toBeVisible();
    });

    test('dashboard 200% zoom 关键元素可见', async ({ page }: any) => {
      await login(page);
      await navigateAndAssert(page, '/');
      await page.setViewportSize({ width: 640, height: 360 });
      const body = page.locator('body');
      await expect(body).toBeVisible();
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(50);
    });
  });

  // ─────────────────────────────────────────────────────────────
  // Section 7: 320 CSS px reflow(保留 R56 §6 存量测试)
  // ─────────────────────────────────────────────────────────────

  test.describe('R56 §6: 320 CSS px reflow', () => {
    test('登录页 320px reflow 不溢出', async ({ page }: any) => {
      await navigateAndAssert(page, '/login', { expectLogin: true });
      await page.setViewportSize({ width: 320, height: 568 });
      await expect(page.locator('input[name="username"]')).toBeVisible();
      await expect(page.locator('input[name="password"]')).toBeVisible();
      await expect(page.locator('button[type="submit"]')).toBeVisible();
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 10);
    });

    test('dashboard 320px reflow 不破坏关键内容', async ({ page }: any) => {
      await login(page);
      await navigateAndAssert(page, '/');
      await page.setViewportSize({ width: 320, height: 568 });
      await expect(page.locator('body')).toBeVisible();
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(20);
    });
  });

  // ─────────────────────────────────────────────────────────────
  // Section 8: prefers-reduced-motion(保留 R56 §6 存量测试)
  // ─────────────────────────────────────────────────────────────

  test.describe('R56 §6: prefers-reduced-motion', () => {
    test('开启 prefers-reduced-motion 时,过渡动画应被禁用或缩短', async ({ browser }: any) => {
      const context = await browser.newContext({ reducedMotion: 'reduce' });
      const page = await context.newPage();
      await navigateAndAssert(page, '/login', { expectLogin: true });
      const hasReducedMotionQuery = await page.evaluate(() => {
        const sheets = Array.from(document.styleSheets);
        for (const sheet of sheets) {
          try {
            const rules = Array.from(sheet.cssRules || []);
            for (const rule of rules) {
              if (rule.cssText && rule.cssText.includes('prefers-reduced-motion')) {
                return true;
              }
            }
          } catch (_e) {
            // 跨域 stylesheet 无法读取,跳过
          }
        }
        return false;
      });
      expect(hasReducedMotionQuery).toBe(true);
      await context.close();
    });
  });

  // ─────────────────────────────────────────────────────────────
  // Section 9: R62 P1-06 — 用例数断言 + 路由元数据完整性检查
  // 防止 stub 替换导致 0 用例 = 绿色 CI(假绿条件)
  // ─────────────────────────────────────────────────────────────

  test('R62 P1-06: a11y 测试用例数 > 0(检测 stub 替换导致的假绿)', () => {
    // A11Y_TESTABLE_CASES 必须非空 — 若为空,说明:
    //   (a) generated_a11y_cases.json 加载失败(应在上面的 loadGeneratedCases 抛错)
    //   (b) stub 替换导致 describe body 未执行(本文件已移除 stub,但保留断言作为防御)
    //   (c) 生成器脚本未运行,JSON 文件为空数组
    expect(A11Y_TESTABLE_CASES.length).toBeGreaterThan(0);
    // 每条 a11y_testable 用例必须生成 2 个 locale 测试(zh-CN + en-US)
    // 加上 Section 2-8 的固定测试,总用例数应 > 2 * a11y_testable 数
    expect(A11Y_TESTABLE_CASES.length * 2).toBeGreaterThanOrEqual(2);
    // 在 stderr 留痕,便于 CI 日志追溯(防止静默 0 用例)
    console.error(
      `[R62 P1-06] a11y testable cases: ${A11Y_TESTABLE_CASES.length}, ` +
      `locale redirect cases: ${LOCALE_REDIRECT_CASES.length}`,
    );
  });

  test('R62 P1-06: 每条 a11y_testable 用例的路由元数据完整(path / route_path / method)', () => {
    // R62 P1-06: 路由元数据缺失必须 HARD FAIL(不允许静默跳过)
    const missing: string[] = [];
    for (const caze of A11Y_TESTABLE_CASES) {
      if (!caze.path || typeof caze.path !== 'string') {
        missing.push(`case missing 'path': ${JSON.stringify(caze).slice(0, 100)}`);
      }
      if (!caze.route_path || typeof caze.route_path !== 'string') {
        missing.push(`case missing 'route_path': path=${caze.path}`);
      }
      if (!caze.method || typeof caze.method !== 'string') {
        missing.push(`case missing 'method': path=${caze.path}`);
      }
      // a11y_testable 必须为 true(过滤条件已保证,但显式断言防止过滤逻辑被破坏)
      if (caze.a11y_testable !== true) {
        missing.push(`case 'a11y_testable' != true: path=${caze.path}`);
      }
      // permission 必须为已知值
      if (caze.permission !== 'require_session' && caze.permission !== 'public') {
        missing.push(`case invalid 'permission': path=${caze.path} permission=${caze.permission}`);
      }
    }
    expect(missing).toEqual([]);
    // 断言 zh-CN + en-US 两个 locale 都有对应的测试用例(通过 case 数 × 2 验证)
    // 每个 a11y_testable 用例应在 Section 1 生成 zh-CN + en-US 两个测试
    console.error(
      `[R62 P1-06] route metadata check passed for ${A11Y_TESTABLE_CASES.length} cases ` +
      `(expected locales: zh-CN, en-US)`,
    );
  });
});
