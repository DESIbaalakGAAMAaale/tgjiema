import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import * as fs from 'fs';
import * as path from 'path';

/**
 * R44 6.4 / R55: Admin Web 无障碍 WCAG 2.2 A/AA 门禁测试
 *
 * R55 整改:
 * - 扫描 ALL Admin 路由(非仅 Login / Dashboard)
 * - 阻断 ANY A/AA 违规(非仅 critical/serious)
 * - 每个路由生成独立 axe JSON artifact 供 CI 收集
 *
 * 覆盖规则集(wcag2a / wcag2aa / wcag21a / wcag21aa / wcag22aa):
 * - WCAG 2.2 AA 2.4.1 Bypass Blocks(skip-link)
 * - WCAG 2.2 AA 2.4.7 / 2.4.11 Focus Visible / Focus Not Obscured
 * - WCAG 2.2 AA 1.4.1 Use of Color(状态不仅用颜色)
 * - WCAG 2.2 AA 1.4.3 Contrast (Minimum) — 4.5:1
 * - WCAG 2.2 AA 3.3.2 Labels or Instructions
 * - WCAG 2.2 AA 4.1.2 Name, Role, Value
 * - WCAG 2.2 AA 4.1.3 Status Messages(aria-live)
 * - WCAG 2.2 AA 1.3.1 Info and Relationships(table scope / caption)
 * - 键盘可操作性 / label 绑定 / heading 顺序
 */

// R47/R48 P0-3: axe JSON artifact 输出目录
// e2e.yml upload-artifact 期望 tests/e2e/e2e-results/axe-*.json
const AXE_RESULTS_DIR = path.join(__dirname, 'e2e-results');

// R55: WCAG 2.2 A/AA 完整规则标签集
const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

// R56 §6: 测试用登录密码必须从环境变量获取,缺失则立即失败
if (!process.env.ADMIN_TEST_PASSWORD) {
  throw new Error(
    'ADMIN_TEST_PASSWORD 环境变量必须设置(R56 §6: 禁止固定默认值)'
  );
}
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD;

// R55: 所有需要认证的 Admin 路由(与 base.html 导航链接 + /mfa/setup 一致)
const AUTHENTICATED_ROUTES: ReadonlyArray<{ path: string; name: string }> = [
  { path: '/', name: 'dashboard' },
  { path: '/users', name: 'users' },
  { path: '/files', name: 'files' },
  { path: '/logs', name: 'logs' },
  { path: '/health-page', name: 'health' },
  { path: '/tasks', name: 'tasks' },
  { path: '/reports', name: 'reports' },
  { path: '/collections', name: 'collections' },
  { path: '/notifications', name: 'notifications' },
  { path: '/approvals', name: 'approvals' },
  { path: '/rbac', name: 'rbac' },
  { path: '/repair-console', name: 'repair-console' },
  { path: '/topology', name: 'topology' },
  { path: '/ru-cost', name: 'ru-cost' },
  { path: '/maintenance', name: 'maintenance' },
  { path: '/disaster-recovery', name: 'disaster-recovery' },
  { path: '/mfa/setup', name: 'mfa-setup' },
];

/** R47 P0-3: 将 axe 扫描结果序列化为 JSON 并保存到 e2e-results/axe-{pageName}.json */
function saveAxeResults(pageName: string, results: any): void {
  if (!fs.existsSync(AXE_RESULTS_DIR)) {
    fs.mkdirSync(AXE_RESULTS_DIR, { recursive: true });
  }
  const filePath = path.join(AXE_RESULTS_DIR, `axe-${pageName}.json`);
  const artifact = {
    pageName,
    timestamp: new Date().toISOString(),
    url: results.url || '',
    violations: results.violations || [],
    passes: results.passes || [],
    incomplete: results.incomplete || [],
    inapplicable: results.inapplicable || [],
    summary: {
      violationCount: (results.violations || []).length,
      passCount: (results.passes || []).length,
    },
  };
  fs.writeFileSync(filePath, JSON.stringify(artifact, null, 2), 'utf-8');
}

/** R55: 打印所有违规详情到 stderr 便于 CI 日志排查 */
function logViolations(pageName: string, violations: any[]): void {
  if (violations.length === 0) return;
  console.error(`\n❌ ${pageName} — WCAG 2.2 A/AA 违规 (${violations.length} 项):`);
  for (const v of violations) {
    console.error(`  [${v.impact || 'unknown'}] ${v.id}: ${v.description}`);
    console.error(`    帮助: ${v.helpUrl}`);
    for (const node of v.nodes) {
      console.error(`    元素: ${JSON.stringify(node.target)}`);
    }
  }
}

/** 登录辅助:填写表单并提交,等待重定向离开 /login */
async function login(page: Page): Promise<void> {
  await page.goto('/login');
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
  await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 });
}

/** 对指定路由执行 axe 扫描,保存 artifact,返回结果 */
async function scanRoute(page: Page, routePath: string, pageName: string): Promise<any> {
  await page.goto(routePath);
  await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
  const results = await new AxeBuilder({ page })
    .withTags(WCAG_TAGS)
    .analyze();
  saveAxeResults(pageName, results);
  logViolations(pageName, results.violations);
  return results;
}

test.describe('Accessibility (WCAG 2.2 A/AA)', () => {
  // ── 未认证路由:登录页 ──────────────────────────────────────
  test('登录页符合 WCAG 2.2 A/AA', async ({ page }: { page: Page }) => {
    const results = await scanRoute(page, '/login', 'login');
    // R55: 阻断 ANY A/AA 违规(非仅 critical/serious)
    expect(results.violations).toEqual([]);
  });

  // ── 所有需要认证的 Admin 路由 ───────────────────────────────
  for (const route of AUTHENTICATED_ROUTES) {
    test(`${route.name} 页符合 WCAG 2.2 A/AA (${route.path})`, async ({ page }: { page: Page }) => {
      await login(page);
      const results = await scanRoute(page, route.path, route.name);
      // R55: 阻断 ANY A/AA 违规(非仅 critical/serious)
      expect(results.violations).toEqual([]);
    });
  }
});
