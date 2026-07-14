import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import * as fs from 'fs';
import * as path from 'path';

/**
 * R44 6.4: Admin Web 无障碍 WCAG 2.2 AA 门禁测试
 * R47 P0-3: axe 扫描结果序列化为 JSON artifact,保存到 e2e-results/axe-{page}.json
 *
 * 使用 @axe-core/playwright 自动检测:
 * - WCAG 2.2 AA 违规
 * - 键盘可操作性
 * - label 与表单绑定
 * - aria-live 播报结果
 * - 颜色对比度
 * - 表格 caption / 语义 heading
 *
 * 阻断规则: 任何 critical/serious violation 都会导致测试失败
 * axe JSON artifact 保存到 e2e-results/ 目录,供 CI upload-artifact 收集
 */

// R47/R48 P0-3: axe JSON artifact 输出目录
// R48 修复: 原路径 path.join(__dirname, '..', 'e2e-results') 解析为 tests/e2e-results/,
//           但 e2e.yml upload-artifact 期望 tests/e2e/e2e-results/axe-*.json。
//           改为 path.join(__dirname, 'e2e-results') → tests/e2e/e2e-results/
const AXE_RESULTS_DIR = path.join(__dirname, 'e2e-results');

/** R47 P0-3: 将 axe 扫描结果序列化为 JSON 并保存到 e2e-results/axe-{pageName}.json */
function saveAxeResults(pageName: string, results: any): void {
  if (!fs.existsSync(AXE_RESULTS_DIR)) {
    fs.mkdirSync(AXE_RESULTS_DIR, { recursive: true });
  }
  const filePath = path.join(AXE_RESULTS_DIR, `axe-${pageName}.json`);
  // 序列化完整 axe 结果(violations / passes / incomplete / inapplicable)
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
      criticalSeriousCount: (results.violations || []).filter(
        (v: any) => v.impact === 'critical' || v.impact === 'serious'
      ).length,
    },
  };
  fs.writeFileSync(filePath, JSON.stringify(artifact, null, 2), 'utf-8');
}

// R47 P0-3: 测试用登录密码(从环境变量读取,与 ADMIN_BOOTSTRAP_PASSWORD 对应)
const ADMIN_PASSWORD = process.env.ADMIN_TEST_PASSWORD || 'test_bootstrap_pw';

test.describe('Accessibility (WCAG 2.2 AA)', () => {
  test('登录页符合 WCAG 2.2 AA', async ({ page }: { page: Page }) => {
    await page.goto('/login');

    const accessibilityScanResults = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .analyze();

    // R47 P0-3: 保存 axe JSON artifact
    saveAxeResults('login', accessibilityScanResults);

    // 阻断 critical 和 serious 违规
    const criticalViolations = accessibilityScanResults.violations.filter(
      v => v.impact === 'critical' || v.impact === 'serious'
    );

    if (criticalViolations.length > 0) {
      console.error('❌ 登录页 WCAG 2.2 AA 违规:');
      for (const v of criticalViolations) {
        console.error(`  [${v.impact}] ${v.id}: ${v.description}`);
        console.error(`    帮助: ${v.helpUrl}`);
        for (const node of v.nodes) {
          console.error(`    元素: ${node.target}`);
        }
      }
    }

    expect(criticalViolations).toEqual([]);
  });

  test('dashboard 页符合 WCAG 2.2 AA', async ({ page }: { page: Page }) => {
    // 先登录(使用 bootstrap 密码)
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');
    // R48: 等待重定向到首页(dashboard),使用 function matcher 替代 glob
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 10_000 }).catch(() => {});

    const accessibilityScanResults = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .analyze();

    // R47 P0-3: 保存 axe JSON artifact
    saveAxeResults('dashboard', accessibilityScanResults);

    const criticalViolations = accessibilityScanResults.violations.filter(
      v => v.impact === 'critical' || v.impact === 'serious'
    );

    if (criticalViolations.length > 0) {
      console.error('❌ Dashboard WCAG 2.2 AA 违规:');
      for (const v of criticalViolations) {
        console.error(`  [${v.impact}] ${v.id}: ${v.description}`);
      }
    }

    expect(criticalViolations).toEqual([]);
  });

  test('所有表单元素有 label 关联', async ({ page }: { page: Page }) => {
    await page.goto('/login');

    const accessibilityScanResults = await new AxeBuilder({ page })
      .withRules(['label', 'form-field-multiple-labels', 'duplicate-id-active'])
      .analyze();

    // R47 P0-3: 保存 axe JSON artifact(仅 label 规则子集)
    saveAxeResults('login-labels', accessibilityScanResults);

    expect(accessibilityScanResults.violations).toEqual([]);
  });

  test('颜色对比度满足 AA 标准', async ({ page }: { page: Page }) => {
    await page.goto('/login');

    const accessibilityScanResults = await new AxeBuilder({ page })
      .withRules(['color-contrast'])
      .analyze();

    // R47 P0-3: 保存 axe JSON artifact(仅 color-contrast 规则)
    saveAxeResults('login-color-contrast', accessibilityScanResults);

    const contrastViolations = accessibilityScanResults.violations.filter(
      v => v.id === 'color-contrast'
    );

    if (contrastViolations.length > 0) {
      console.error('❌ 颜色对比度不足:');
      for (const v of contrastViolations) {
        for (const node of v.nodes) {
          console.error(`  元素: ${node.target}, 影响: ${v.impact}`);
        }
      }
    }

    // 仅阻断 serious+critical 级别的对比度问题
    const criticalContrast = contrastViolations.filter(v => v.impact === 'critical' || v.impact === 'serious');
    expect(criticalContrast).toEqual([]);
  });
});
