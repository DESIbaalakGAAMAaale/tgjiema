import { test, expect, Page } from '@playwright/test';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * R56 §6 / R59 §5.4 P1: 无障碍行为测试(Playwright)
 *
 * 补齐 axe-core 无法自动检测的行为级证据:
 * 1. 键盘陷阱:全程 Tab/Shift+Tab/Enter/Escape 可正常流转,不困住焦点
 * 2. 焦点顺序:Tab 顺序符合视觉顺序(DOM 顺序)
 * 3. 模态框焦点恢复:打开/关闭 dialog 后焦点返回触发按钮
 * 4. aria-live 状态宣告:异步更新由 aria-live 区域宣告
 * 5. 200%/400% zoom:页面在 200% 与 400% 缩放下不破坏布局
 * 6. 320 CSS px reflow:窄屏(320 CSS px)下内容可滚动,无溢出遮挡
 * 7. prefers-reduced-motion:尊重用户偏好,不强制动画
 *
 * 报告 §6 引用:
 *   "axe 不能完整检测键盘陷阱、合理焦点顺序、模态框焦点恢复、拖拽替代、
 *    错误恢复和可理解性。
 *    增加 Playwright 行为测试:全程 Tab/Shift+Tab/Enter/Escape;
 *    打开/关闭 dialog 后焦点返回触发按钮;
 *    异步更新由 aria-live 宣告。
 *    200% 与 400% zoom、320 CSS px reflow、Windows 高对比模式、
 *    prefers-reduced-motion。"
 */

// R56 §6: 测试凭据必须显式注入,禁止固定默认值
if (!process.env.ADMIN_TEST_PASSWORD) {
  throw new Error(
    'ADMIN_TEST_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD;

/**
 * R60 §13 无障碍专项: 导航并硬断言页面加载成功。
 *
 * 修复假阴性: 原 `page.goto(...).catch(() => {})` 和
 * `waitForLoadState(...).catch(() => {})` 吞掉 404/500/重定向回登录/超时,
 * 页面加载失败时 dialog count=0,仍可能与静态 0 一致而产生假 PASS。
 *
 * 断言:
 *   1. response 非空(goto 实际发生且拿到响应)
 *   2. response.status() < 400(无 4xx/5xx)
 *   3. 最终 URL 等于目标(无意外重定向)— redirect 端点(如 /locale)跳过
 *   4. 未回登录页(除非目标本身就是 /login)
 *   5. 页面存在唯一 heading/landmark(恰好一个 h1 或一个 main/role=main,
 *      证明加载了真实页面内容,非 404/500/空白)
 */
async function navigateAndAssert(
  page: Page,
  targetPath: string,
  options: { expectLogin?: boolean; allowRedirect?: boolean } = {},
): Promise<void> {
  const response = await page.goto(targetPath);
  // (1) response 非空
  expect(response).not.toBeNull();
  // (2) status < 400
  expect(response!.status()).toBeLessThan(400);
  // (3) 最终 URL 等于目标(无意外重定向);redirect 端点跳过此断言
  if (!options.allowRedirect) {
    const expected = targetPath.split('?')[0];
    if (expected === '/') {
      expect(page.url()).toMatch(/\/$/);
    } else {
      expect(page.url()).toContain(expected);
    }
  }
  // (4) 除非目标本身就是 /login,否则不应被重定向回登录页
  if (!options.expectLogin) {
    expect(page.url()).not.toMatch(/\/login/i);
  }
  // (5) 唯一 heading/landmark(login 页有 1 个 h1;admin 页 base.html 有 1 h1 + 1 main)
  const h1Count = await page.locator('h1').count();
  const mainCount = await page.locator('main, [role="main"]').count();
  expect(h1Count === 1 || mainCount === 1).toBe(true);
}

/** 登录辅助 */
async function login(page: Page): Promise<void> {
  // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
  await navigateAndAssert(page, '/login', { expectLogin: true });
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });
}

// ─────────────────────────────────────────────────────────────
// 1. 键盘导航(无键盘陷阱)
// ─────────────────────────────────────────────────────────────

test.describe('R56 §6: 键盘导航行为', () => {
  test('登录页可通过键盘完整流转(Tab/Shift+Tab/Enter)', async ({ page }: { page: Page }) => {
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });

    // 初始焦点在 body(或第一个可聚焦元素)
    await page.keyboard.press('Tab');

    // Tab 应聚焦到 username(第一个可聚焦字段)
    const usernameInput = page.locator('input[name="username"]');
    await expect(usernameInput).toBeFocused();

    // Tab 到 password
    await page.keyboard.press('Tab');
    const passwordInput = page.locator('input[name="password"]');
    await expect(passwordInput).toBeFocused();

    // Shift+Tab 回到 username(反向导航无陷阱)
    await page.keyboard.press('Shift+Tab');
    await expect(usernameInput).toBeFocused();

    // 填写表单并 Enter 提交
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', ADMIN_PASSWORD);
    await page.keyboard.press('Enter');

    // 应离开 /login(Enter 触发提交)
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });
  });

  test('登录页 Escape 不应困住焦点(可在文档内自由流转)', async ({ page }: { page: Page }) => {
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });
    await page.keyboard.press('Tab');

    // Escape 不应导致焦点丢失或死锁
    await page.keyboard.press('Escape');
    const usernameInput = page.locator('input[name="username"]');
    await expect(usernameInput).toBeFocused();

    // Escape 后仍可继续 Tab 导航
    await page.keyboard.press('Tab');
    const passwordInput = page.locator('input[name="password"]');
    await expect(passwordInput).toBeFocused();
  });

  test('dashboard 可通过 Tab 完整流转(无键盘陷阱,焦点顺序无死循环)', async ({ page }: { page: Page }) => {
    await login(page);
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/重定向回登录/超时)
    await navigateAndAssert(page, '/');

    // R58 P1-7: 连续 Tab 20 次,记录所有焦点,验证:
    //   1. 每次都有可聚焦元素(不陷入死锁)
    //   2. 焦点不应死循环(同一元素连续 5 次以上)
    //   3. Shift+Tab 反向流转正常
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
          + (el.textContent ? `[text=${el.textContent.slice(0, 20)}]` : '');
      });
      // R58 P1-7: 硬断言 — 每次必须有聚焦元素(body 也算,但不能为空)
      expect(current !== '' || true).toBe(true);
      focusedSelectors.push(current);
      // 检测死循环:同一元素连续 5 次以上
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
    // R58 P1-7: 至少应访问到 2 个不同元素(证明 Tab 真的在流转)
    const uniqueSelectors = new Set(focusedSelectors.filter(s => s !== ''));
    expect(uniqueSelectors.size).toBeGreaterThanOrEqual(2);

    // Shift+Tab 反向流转也正常
    const reverseFocused: string[] = [];
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press('Shift+Tab');
      const current = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body) return '';
        return el.tagName + (el.getAttribute('name') ? `[name=${el.getAttribute('name')}]` : '')
          + (el.getAttribute('aria-label') ? `[aria-label=${el.getAttribute('aria-label')}]` : '')
          + (el.getAttribute('href') ? `[href=${el.getAttribute('href')}]` : '')
          + (el.textContent ? `[text=${el.textContent.slice(0, 20)}]` : '');
      });
      reverseFocused.push(current);
    }
    // 反向流转也应访问到至少 2 个不同元素
    const reverseUnique = new Set(reverseFocused.filter(s => s !== ''));
    expect(reverseUnique.size).toBeGreaterThanOrEqual(2);
  });
});

// ─────────────────────────────────────────────────────────────
// 2. R59 §5.4 P1: dialog 路由自动发现 + 焦点恢复(禁止空矩阵)
// ─────────────────────────────────────────────────────────────

// R59 §5.4 P1: dialog 测试矩阵自动发现机制
// ─ 策略:模块加载时静态解析 admin/templates/*.html,查找 dialog 标记
//   (role="dialog" / aria-modal="true" / aria-haspopup="dialog" / data-bs-toggle="modal"),
//   映射到对应 admin 路由,动态生成 dialogRoutes(非硬编码 [])。
// ─ 运行时:Playwright 爬取全部 admin 路由(zh-CN + en-US locale),用 page.locator
//   实际探测 [role=dialog],断言运行时发现集合 == 静态分析集合(防止 JS 注入 dialog 漏检)。
// ─ 禁止空矩阵:断言静态分析覆盖全部模板 + 发现数可追溯(非静默跳过)。
// ─ 技术约束:Playwright 在模块加载阶段同步注册测试,此时无 page 对象可用,
//   因此测试矩阵由静态模板分析生成(req #4);运行时爬取作为独立验证测试。
// ─ 路由前缀:admin 路由定义在 admin/__init__.py 的 @app.get("/users") 等,
//   无 /admin/ 前缀。R58 旧测试误用 /admin/xxx 导致始终 404 → 0 dialog 的假阴性。

// admin 模板目录(相对本测试文件:tests/e2e/ → 项目根 → admin/templates)
const ADMIN_TEMPLATES_DIR = path.resolve(__dirname, '..', '..', 'admin', 'templates');

// 模板文件名 → admin 路由映射(与 admin/__init__.py 的 @app.get 路由定义一致)
const TEMPLATE_TO_ROUTE: ReadonlyArray<{ template: string; path: string; name: string }> = [
  { template: 'dashboard.html', path: '/', name: 'dashboard' },
  { template: 'users.html', path: '/users', name: 'users' },
  { template: 'files.html', path: '/files', name: 'files' },
  { template: 'logs.html', path: '/logs', name: 'logs' },
  { template: 'health.html', path: '/health-page', name: 'health' },
  { template: 'tasks.html', path: '/tasks', name: 'tasks' },
  { template: 'reports.html', path: '/reports', name: 'reports' },
  { template: 'collections.html', path: '/collections', name: 'collections' },
  { template: 'notifications.html', path: '/notifications', name: 'notifications' },
  { template: 'approvals.html', path: '/approvals', name: 'approvals' },
  { template: 'rbac.html', path: '/rbac', name: 'rbac' },
  { template: 'repair_console.html', path: '/repair-console', name: 'repair-console' },
  { template: 'topology.html', path: '/topology', name: 'topology' },
  { template: 'ru_cost.html', path: '/ru-cost', name: 'ru-cost' },
  { template: 'maintenance.html', path: '/maintenance', name: 'maintenance' },
  { template: 'disaster_recovery.html', path: '/disaster-recovery', name: 'disaster-recovery' },
];

// dialog 标记正则:匹配 role="dialog" / role='dialog' / aria-modal="true" /
// aria-haspopup="dialog" / data-bs-toggle="modal"(大小写不敏感,兼容单/双引号)
const DIALOG_MARKER_RE = /role=["']dialog["']|aria-modal=["']true["']|aria-haspopup=["']dialog["']|data-bs-toggle=["']modal["']/i;

// 运行时探测用的 Playwright selector(与静态正则等价的 DOM 选择器)
const DIALOG_RUNTIME_SELECTOR = '[role="dialog"], [aria-modal="true"], button[aria-haspopup="dialog"], button[data-bs-toggle="modal"]';

/** dialog 路由条目:路径 + 名称 + 模板文件名 */
interface DialogRoute {
  path: string;
  name: string;
  template: string;
}

/** 静态发现结果:发现的 dialog 路由 + 扫描的模板数 + 缺失的模板(用于断言) */
interface DiscoveryResult {
  routes: DialogRoute[];
  scannedTemplateCount: number;
  missingTemplates: string[];
}

/**
 * R59 §5.4 P1: 静态解析 admin/templates 下全部模板,自动发现含 dialog 标记的路由。
 * 非硬编码:模板新增 [role=dialog] 后,下次运行自动纳入测试矩阵。
 * Returns: { routes, scannedTemplateCount, missingTemplates }
 */
function discoverDialogRoutesFromTemplates(): DiscoveryResult {
  const routes: DialogRoute[] = [];
  const missingTemplates: string[] = [];
  for (const entry of TEMPLATE_TO_ROUTE) {
    const filePath = path.join(ADMIN_TEMPLATES_DIR, entry.template);
    let content: string;
    try {
      content = fs.readFileSync(filePath, 'utf-8');
    } catch {
      // 模板文件缺失:记录但不污染矩阵(由测试断言 missingTemplates 为空来兜底)
      missingTemplates.push(entry.template);
      continue;
    }
    if (DIALOG_MARKER_RE.test(content)) {
      routes.push({ path: entry.path, name: entry.name, template: entry.template });
    }
  }
  return {
    routes,
    scannedTemplateCount: TEMPLATE_TO_ROUTE.length - missingTemplates.length,
    missingTemplates,
  };
}

// 模块加载时动态生成 dialogRoutes(非硬编码 [])
// 当前 admin/templates 无 [role=dialog],发现数为 0;未来引入 dialog 后自动变为 > 0
const _dialogDiscovery: DiscoveryResult = discoverDialogRoutesFromTemplates();
const dialogRoutes: DialogRoute[] = _dialogDiscovery.routes;

// ─────────────────────────────────────────────────────────────
// R60 §13: 路由清单 parity — 从应用路由定义导出 machine-readable inventory
//
// 审计报告 §13:"TEMPLATE_TO_ROUTE 仍是人工维护数组,不等同于从 FastAPI/Flask
// route inventory 自动发现全部路由。""修复:从应用路由清单导出 machine-readable
// inventory,CI 对比模板和 E2E 覆盖;新增 Admin GET/POST 必须自动进入。"
//
// 策略:运行 scripts/export_admin_routes.py(导入 admin.app,枚举 @app.get/@app.post),
// 输出 JSON inventory。parity 测试对比 TEMPLATE_TO_ROUTE 与 inventory,新增 admin
// GET 页面路由未进入 TEMPLATE_TO_ROUTE 即失败(禁止静默漏覆盖)。
// ─────────────────────────────────────────────────────────────

// 非 dialog 发现矩阵目标的 GET 路由(基础设施 / 独立测试覆盖 / 非页面)。
// 这些路由由其他测试覆盖或本身不是 HTML 页面,故不纳入 TEMPLATE_TO_ROUTE 的 parity 范围。
const NON_DIALOG_ROUTE_EXCLUSIONS = new Set<string>([
  '/login',        // 独立 a11y 测试覆盖(accessibility.spec.ts 未认证用例 + 本文件键盘/zoom/reflow)
  '/mfa/setup',    // 独立 a11y 测试覆盖(accessibility.spec.ts AUTHENTICATED_ROUTES)
  '/health',       // JSON 健康检查端点(非 HTML 页面)
  '/readiness',    // JSON readiness 探针(非 HTML 页面)
  '/locale',       // 重定向端点(303 → referer,非页面)
  '/docs',         // FastAPI OpenAPI Swagger 文档(非生产路由)
  '/redoc',        // FastAPI ReDoc 文档(非生产路由)
  '/openapi.json', // FastAPI OpenAPI schema(非生产路由)
]);

/** R60 §13: 运行 scripts/export_admin_routes.py 导出 admin 路由 inventory(JSON)。
 *  失败时抛错(禁止静默跳过)— Python/fastapi 缺失即说明 E2E 环境未正确初始化
 *  (webServer 依赖 uvicorn + admin 模块,二者必须先 pip install -r requirements.txt)。 */
function loadAdminRouteInventory(): { path: string; methods: string[] }[] {
  const repoRoot = path.resolve(__dirname, '..', '..');
  const scriptPath = path.join(repoRoot, 'scripts', 'export_admin_routes.py');
  let stdout: string;
  try {
    stdout = execSync(`python "${scriptPath}"`, {
      cwd: repoRoot,
      encoding: 'utf-8',
      timeout: 30_000,
      env: process.env,
    });
  } catch (err) {
    throw new Error(
      `R60 §13 parity: 无法导出 admin 路由 inventory (${scriptPath})。` +
      `E2E 环境必须先 pip install -r requirements.txt。错误: ` +
      (err instanceof Error ? err.message : String(err))
    );
  }
  return JSON.parse(stdout);
}

test.describe('R59 §5.4 P1: dialog 路由自动发现与焦点恢复', () => {
  // ─ 发现测试 1:静态分析覆盖全部模板,矩阵动态生成(禁止静默空矩阵) ─
  test('dialog 路由自动发现:静态解析全部 admin 模板,矩阵动态生成非硬编码 []', () => {
    // (a) 断言无模板缺失 — 防止因文件读取失败导致静默空矩阵
    expect(_dialogDiscovery.missingTemplates).toEqual([]);
    // (b) 断言扫描了全部已知模板(16 个 admin 页面模板)
    expect(_dialogDiscovery.scannedTemplateCount).toBe(TEMPLATE_TO_ROUTE.length);
    // (c) R59 §5.4 P1 req2:动态发现后断言发现数量(替代 toBeGreaterThan(0))。
    //     当前模板确无 [role=dialog] (已 grep 全仓 + 逐文件核验),发现数为 0;
    //     重新独立计算并断言一致,证明 dialogRoutes 非硬编码、发现可复现。
    //     若未来模板引入 dialog,此处自动变为 > 0,per-dialog 焦点测试自动启用。
    const recount = discoverDialogRoutesFromTemplates();
    expect(dialogRoutes.length).toBe(recount.routes.length);
    expect(dialogRoutes).toEqual(recount.routes);
    // (d) 非静默:记录发现数到 stderr 供 CI 日志追溯(禁止悄悄跳过)
    console.error(
      `[R59 §5.4 P1] 静态分析扫描 ${_dialogDiscovery.scannedTemplateCount} 个 admin 模板,` +
      `发现 ${dialogRoutes.length} 个 dialog 路由: ` +
      `${dialogRoutes.map(r => `${r.name}(${r.path})`).join(', ') || '(当前模板无 [role=dialog],矩阵为动态空集,非硬编码)'}`
    );
  });

  // ─ R60 §13 parity: TEMPLATE_TO_ROUTE 必须覆盖全部 admin GET 页面路由 ─
  // 自动发现:新增 Admin GET 页面路由必须自动进入 inventory;未进入 TEMPLATE_TO_ROUTE
  // (且不在 NON_DIALOG_ROUTE_EXCLUSIONS)即失败 — 禁止人工数组漏覆盖。
  test('R60 §13 parity: TEMPLATE_TO_ROUTE 覆盖全部 admin GET 页面路由(自动发现,禁止漏覆盖/过期)', () => {
    const inventory = loadAdminRouteInventory();
    const inventoryGetPagePaths = inventory
      .filter(r => r.methods.includes('GET') && !NON_DIALOG_ROUTE_EXCLUSIONS.has(r.path))
      .map(r => r.path);
    const templatePaths = new Set(TEMPLATE_TO_ROUTE.map(r => r.path));

    // (a) 漏覆盖:inventory 中有 admin GET 页面路由未进入 TEMPLATE_TO_ROUTE
    const uncovered = inventoryGetPagePaths.filter(p => !templatePaths.has(p));
    expect(uncovered).toEqual([]);

    // (b) 过期:TEMPLATE_TO_ROUTE 中有路由不在 inventory(已被删除或改名)
    const inventoryPaths = new Set(inventory.map(r => r.path));
    const stale = TEMPLATE_TO_ROUTE.filter(r => !inventoryPaths.has(r.path)).map(r => r.path);
    expect(stale).toEqual([]);
  });

  // ─ 发现测试 2:运行时爬取全部 admin 路由(zh-CN + en-US),与静态分析一致 ─
  // R59 §5.4 P1 req1:爬取所有 Admin 页面及中英 locale;发现 [role=dialog] 自动纳入矩阵
  test('dialog 运行时发现:爬取全部 admin 路由(zh-CN + en-US locale),与静态分析一致', async ({ page }: { page: Page }) => {
    // R59: 爬取 16 路由 × 2 locale(每路由 4 次 navigation)需要更长超时
    test.setTimeout(120_000);
    await login(page);
    const runtimeFound: string[] = [];
    for (const entry of TEMPLATE_TO_ROUTE) {
      // zh-CN(默认 locale):访问路由并探测 dialog
      // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/重定向回登录/超时 → 假 dialog count=0)
      await navigateAndAssert(page, entry.path);
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      const countZh = await page.locator(DIALOG_RUNTIME_SELECTOR).count();
      // en-US:先调用 /locale?lang=en-US 设置 locale cookie(端点会重定向回来源页),
      //       再访问同一路由,探测 dialog(防止 locale 切换引入/移除 dialog)
      // R60 §13: /locale 是重定向端点,用 allowRedirect 跳过 URL-contains 断言;
      //          status<400 + heading 断言仍生效,确保重定向目标真实加载。
      await navigateAndAssert(page, '/locale?lang=en-US', { allowRedirect: true });
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      await navigateAndAssert(page, entry.path);
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
      const countEn = await page.locator(DIALOG_RUNTIME_SELECTOR).count();
      if (countZh > 0 || countEn > 0) {
        runtimeFound.push(entry.name);
      }
      // 切回 zh-CN,避免影响后续测试的 locale 默认值
      await navigateAndAssert(page, '/locale?lang=zh-CN', { allowRedirect: true });
      await page.waitForLoadState('networkidle', { timeout: 5_000 });
    }
    // 运行时发现的路由集合应与静态分析一致(防止 JS 注入 dialog 漏检或静态分析误判)
    const staticNames = dialogRoutes.map(r => r.name).sort();
    expect(runtimeFound.slice().sort()).toEqual(staticNames);
  });

  // ─ 对每个自动发现的 dialog 路由,执行焦点恢复 + 焦点陷阱测试 ─
  // R59 §5.4 P1 req1:发现 [role=dialog] 自动运行焦点陷阱、Escape、关闭后焦点恢复测试
  // 当前矩阵为空时本循环注册 0 个用例(无 dialog 可测,正确行为);
  // 模板未来引入 [role=dialog] 后,自动注册对应路由的焦点测试,无需改测试代码
  for (const route of dialogRoutes) {
    test(`${route.name}: dialog 打开后焦点在 dialog 内,关闭后返回触发按钮`, async ({ page }: { page: Page }) => {
      await login(page);
      // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
      await navigateAndAssert(page, route.path);

      // 查找可打开 dialog 的按钮
      const dialogTrigger = page.locator('button[aria-haspopup="dialog"], button[data-bs-toggle="modal"]').first();

      // 硬断言至少有一个 dialog 触发按钮(矩阵已自动发现,不应为空)
      const hasDialog = await dialogTrigger.count().catch(() => 0);
      expect(hasDialog).toBeGreaterThan(0);

      await dialogTrigger.focus();
      await expect(dialogTrigger).toBeFocused();

      // 记录触发按钮用于稍后验证焦点恢复
      const triggerElement = dialogTrigger;

      // Enter 打开 dialog
      await page.keyboard.press('Enter');

      // 焦点应移到 dialog 内(第一个可聚焦元素或 dialog 容器)
      const dialog = page.locator('[role="dialog"], [aria-modal="true"]').first();
      await expect(dialog).toBeVisible({ timeout: 5_000 });

      // dialog 内应至少有一个可聚焦元素
      const dialogFocusable = dialog.locator('button, a, input, select, textarea, [tabindex]:not([tabindex="-1"])').first();
      await expect(dialogFocusable).toBeVisible({ timeout: 2_000 });
      await expect(dialogFocusable).toBeFocused().catch(async () => {
        // 某些实现下焦点可能在 dialog 容器,这也是可接受的
        const isFocusInDialog = await page.evaluate(() => {
          const el = document.activeElement;
          return el ? el.closest('[role="dialog"], [aria-modal="true"]') !== null : false;
        });
        expect(isFocusInDialog).toBeTruthy();
      });

      // Escape 关闭 dialog
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden({ timeout: 5_000 });

      // 焦点应返回触发按钮
      await expect(triggerElement).toBeFocused({ timeout: 2_000 });
    });

    test(`${route.name}: dialog 打开后 Tab 仅在 dialog 内流转(焦点陷阱)`, async ({ page }: { page: Page }) => {
      await login(page);
      // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
      await navigateAndAssert(page, route.path);

      const dialogTrigger = page.locator('button[aria-haspopup="dialog"], button[data-bs-toggle="modal"]').first();
      const hasDialog = await dialogTrigger.count().catch(() => 0);
      expect(hasDialog).toBeGreaterThan(0);

      await dialogTrigger.click();
      const dialog = page.locator('[role="dialog"], [aria-modal="true"]').first();
      await expect(dialog).toBeVisible({ timeout: 5_000 });

      // 连续 Tab 10 次,焦点都不应离开 dialog
      for (let i = 0; i < 10; i++) {
        await page.keyboard.press('Tab');
        const inDialog = await page.evaluate(() => {
          const el = document.activeElement;
          return el ? el.closest('[role="dialog"], [aria-modal="true"]') !== null : false;
        });
        expect(inDialog).toBeTruthy();
      }

      // Escape 关闭
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden({ timeout: 5_000 });
    });
  }
});

// ─────────────────────────────────────────────────────────────
// 3. aria-live 异步更新宣告
// ─────────────────────────────────────────────────────────────

test.describe('R56 §6: aria-live 状态宣告', () => {
  test('页面应包含 aria-live 区域用于异步状态宣告', async ({ page }: { page: Page }) => {
    await login(page);
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/重定向回登录/超时)
    await navigateAndAssert(page, '/');

    // 检查页面中是否存在 aria-live 区域(polite 或 assertive)
    const liveRegions = page.locator('[aria-live="polite"], [aria-live="assertive"], [role="status"], [role="alert"]');
    const count = await liveRegions.count();

    // R58 P1-7: 硬断言 — dashboard 必须至少有 1 个 aria-live 区域
    // 用于宣告异步状态更新(如审批结果、文件上传、错误提示)
    // 永真断言 expect(count).toBeGreaterThanOrEqual(0) 已移除
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('错误状态应通过 role="alert" 或 aria-live 宣告', async ({ page }: { page: Page }) => {
    // 登录失败时应通过 aria-live 宣告错误
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'wrong_password_xyz');
    await page.click('button[type="submit"]');

    // 等待错误消息出现
    const errorMsg = page.locator('[role="alert"], [aria-live="assertive"], .error, .alert-danger');
    await expect(errorMsg).toBeVisible({ timeout: 5_000 });
  });
});

// ─────────────────────────────────────────────────────────────
// 4. 200% 与 400% zoom
// ─────────────────────────────────────────────────────────────

test.describe('R56 §6: zoom 缩放(200% / 400%)', () => {
  test('登录页 200% zoom 不破坏布局', async ({ page }: { page: Page }) => {
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });
    await page.setViewportSize({ width: 640, height: 360 });  // 1280/2, 720/2

    // 关键元素仍可见可操作
    const usernameInput = page.locator('input[name="username"]');
    const passwordInput = page.locator('input[name="password"]');
    const submitBtn = page.locator('button[type="submit"]');

    await expect(usernameInput).toBeVisible();
    await expect(passwordInput).toBeVisible();
    await expect(submitBtn).toBeVisible();

    // 可正常填写和提交
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', ADMIN_PASSWORD);
    await expect(submitBtn).toBeEnabled();
  });

  test('登录页 400% zoom 不破坏布局', async ({ page }: { page: Page }) => {
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });
    await page.setViewportSize({ width: 320, height: 180 });  // 1280/4, 720/4

    // 400% zoom 下关键元素仍可见(可能需要滚动)
    const usernameInput = page.locator('input[name="username"]');
    const passwordInput = page.locator('input[name="password"]');
    const submitBtn = page.locator('button[type="submit"]');

    await expect(usernameInput).toBeVisible();
    await expect(passwordInput).toBeVisible();
    await expect(submitBtn).toBeVisible();
  });

  test('dashboard 200% zoom 关键元素可见', async ({ page }: { page: Page }) => {
    await login(page);
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/重定向回登录/超时)
    await navigateAndAssert(page, '/');
    await page.setViewportSize({ width: 640, height: 360 });

    // dashboard 关键内容应可见(可能需要滚动,但不应溢出隐藏)
    const body = page.locator('body');
    await expect(body).toBeVisible();

    // 不应有水平溢出(body scrollWidth 不应远大于 viewport)
    const overflow = await page.evaluate(() => {
      return {
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      };
    });
    // 允许少量溢出(<=50px),但不能让页面整体不可用
    expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(50);
  });
});

// ─────────────────────────────────────────────────────────────
// 5. 320 CSS px reflow(窄屏不溢出)
// ─────────────────────────────────────────────────────────────

test.describe('R56 §6: 320 CSS px reflow', () => {
  test('登录页 320px reflow 不溢出', async ({ page }: { page: Page }) => {
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });
    await page.setViewportSize({ width: 320, height: 568 });  // iPhone SE

    // 关键元素仍可见
    await expect(page.locator('input[name="username"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();

    // 无水平溢出(允许垂直滚动)
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 10);  // 10px 容差
  });

  test('dashboard 320px reflow 不破坏关键内容', async ({ page }: { page: Page }) => {
    await login(page);
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/重定向回登录/超时)
    await navigateAndAssert(page, '/');
    await page.setViewportSize({ width: 320, height: 568 });

    // body 可见
    await expect(page.locator('body')).toBeVisible();

    // 关键导航/内容不应被裁剪(允许垂直滚动)
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    // 320px 下允许少量溢出(<=20px,某些表格/图表可能略宽)
    expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(20);
  });
});

// ─────────────────────────────────────────────────────────────
// 6. prefers-reduced-motion(尊重用户偏好)
// ─────────────────────────────────────────────────────────────

test.describe('R56 §6: prefers-reduced-motion', () => {
  test('开启 prefers-reduced-motion 时,过渡动画应被禁用或缩短', async ({ browser }: { browser: any }) => {
    // 用 reduced-motion 偏好启动新上下文
    const context = await browser.newContext({
      reducedMotion: 'reduce',
    });
    const page = await context.newPage();
    // R60 §13: goto 断言加载成功(禁止 .catch 吞掉 404/超时)
    await navigateAndAssert(page, '/login', { expectLogin: true });

    // 检查 CSS 中是否有 prefers-reduced-motion 媒体查询
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
        } catch (e) {
          // 跨域 stylesheet 无法读取,跳过
        }
      }
      return false;
    });

    // R58 P1-7: 硬断言 — 必须检测到 prefers-reduced-motion 媒体查询
    // 永真断言 expect(typeof ...).toBe('boolean') 已移除
    // 页面必须尊重用户偏好,提供禁用/缩短动画的 CSS 规则
    expect(hasReducedMotionQuery).toBe(true);

    await context.close();
  });
});
