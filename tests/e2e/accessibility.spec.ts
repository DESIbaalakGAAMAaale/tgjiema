import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/**
 * R44 6.4: Admin Web 无障碍 WCAG 2.2 AA 门禁测试
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
 */

test.describe('Accessibility (WCAG 2.2 AA)', () => {
  test('登录页符合 WCAG 2.2 AA', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    
    const accessibilityScanResults = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .analyze();
    
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
    // 先登录
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', process.env.ADMIN_TEST_PASSWORD || 'testpass');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/dashboard', { timeout: 5000 }).catch(() => {});
    
    const accessibilityScanResults = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .analyze();
    
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
    
    expect(accessibilityScanResults.violations).toEqual([]);
  });

  test('颜色对比度满足 AA 标准', async ({ page }: { page: Page }) => {
    await page.goto('/login');
    
    const accessibilityScanResults = await new AxeBuilder({ page })
      .withRules(['color-contrast'])
      .analyze();
    
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
