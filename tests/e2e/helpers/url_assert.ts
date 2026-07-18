/**
 * R61 P1-08: 严格 URL 比较 + locale 重定向断言工具。
 *
 * 取代人工 TEMPLATE_TO_ROUTE 数组时代的宽松 ``page.url().toContain(expected)``:
 *   - assertUrlEquals: 用 URL parser 严格比较 pathname + query(set 语义),
 *     拒绝相似路径误判(如 /users 与 /users/1/membership)。
 *   - assertLocaleRedirect: 显式断言 303 / Set-Cookie / Referer 安全 / 最终路径,
 *     取代 allowRedirect 的"放行不校验"语义。
 *
 * 设计约束:
 *   - 不依赖 @playwright/test 的 expect(可在 node 直接运行校验),
 *     但若传入 response 为 Playwright Response 对象,会读取 headers()。
 *   - query 比较使用 set 语义(顺序无关),与 URLSearchParams 规范一致。
 */

/**
 * 期望的 URL 形状:pathname 严格相等,query 作为 set 比较(顺序无关)。
 */
export interface ExpectedUrl {
  /** 必须严格相等的 pathname(区分尾随斜杠 / 大小写)。 */
  pathname: string;
  /** 可选:期望出现的 query 参数(actual 允许有额外的键)。 */
  query?: Record<string, string>;
}

/**
 * R61 P1-08: 用 URL parser 严格比较 actual 与 expected 的 pathname + query。
 *
 * 严格性:
 *   - pathname 必须完全相等(``===``,区分尾随斜杠 / 大小写)。
 *   - query 使用 set 语义:expected 中每个键的值必须出现在 actual 中,
 *     actual 中允许存在 expected 未声明的额外键(由路由附加的 CSRF/分页等)。
 *   - 不比较 hash(测试矩阵不涉及 fragment)。
 *
 * 失败时抛 Error,e2e 测试侧由 Playwright test.fail 捕获。
 *
 * @param actual 实际 URL(字符串或 URL 对象)
 * @param expected 期望的 pathname + 可选 query
 */
export function assertUrlEquals(actual: string | URL, expected: ExpectedUrl): void {
  if (!actual) {
    throw new Error('assertUrlEquals: actual URL 为空');
  }
  const actualUrl = typeof actual === 'string'
    ? new URL(actual, 'http://test.invalid')
    : actual;

  // (1) pathname 严格相等
  if (actualUrl.pathname !== expected.pathname) {
    throw new Error(
      `URL pathname 不匹配:\n  expected: ${expected.pathname}\n  actual:   ${actualUrl.pathname}\n` +
      `(R61 P1-08: 严格比较,不再用 toContain 接受相似路径)`
    );
  }

  // (2) query: expected 中每个键的值必须出现在 actual 中(set 语义,顺序无关)
  if (expected.query) {
    const actualParams = actualUrl.searchParams;
    for (const [key, expectedValue] of Object.entries(expected.query)) {
      const actualValues = actualParams.getAll(key);
      if (actualValues.length === 0) {
        throw new Error(
          `URL query 缺少参数 "${key}"\n  expected: ${expectedValue}\n  actual:   (missing)\n` +
          `完整 actual query: ${actualUrl.search}`
        );
      }
      if (!actualValues.includes(expectedValue)) {
        throw new Error(
          `URL query 参数 "${key}" 值不匹配:\n  expected: ${expectedValue}\n  actual:   ${actualValues.join(', ')}`
        );
      }
    }
  }
}

/**
 * R61 P1-08: 期望的 locale 重定向断言输入。
 */
export interface ExpectedLocaleRedirect {
  /** 重定向最终应到达的路径(/ 或 /dashboard 等,严格相等)。 */
  expectedPath: string;
  /** 期望写入 cookie 的 locale 值(如 zh-CN / en-US)。 */
  expectedLocale: string;
  /** 期望的 cookie 名称(默认 "locale")。admin/__init__.py 的 /locale 写 "locale" cookie。 */
  cookieName?: string;
}

/**
 * R61 P1-08: 严格断言 locale 重定向响应。
 *
 * 取代旧 allowRedirect 的"放行不校验",显式断言:
 *   (1) HTTP 状态码 = 303 See Other(RFC 7231: POST 后重定向必须 303)
 *   (2) Set-Cookie 头存在且包含 locale=expectedLocale
 *   (3) Referer 安全:Referrer-Policy 头存在且为安全值(no-referrer /
 *       same-origin / strict-origin / strict-origin-when-cross-origin 之一),
 *       防止外部 Referer 泄漏
 *   (4) 最终重定向路径严格匹配 expectedPath(用 assertUrlEquals 严格比较)
 *
 * @param response Playwright Response 对象(由 page.goto() 返回,捕获重定向响应)
 * @param finalUrl page.url() 返回的最终 URL(重定向后)
 * @param expected 期望断言
 */
export function assertLocaleRedirect(
  response: { status(): number; headers(): Record<string, string> } | null,
  finalUrl: string | URL,
  expected: ExpectedLocaleRedirect,
): void {
  if (!response) {
    throw new Error('assertLocaleRedirect: response 为空(goto 未发生或被吞掉)');
  }

  // (1) HTTP 303(RFC 7231: POST 后重定向必须 303 See Other)
  const status = response.status();
  if (status !== 303) {
    throw new Error(
      `locale 重定向状态码不匹配:\n  expected: 303\n  actual:   ${status}\n` +
      `(R61 P1-08: locale 切换必须用 303,禁止 302/307)`
    );
  }

  const headers = response.headers() || {};
  // HTTP 头大小写不敏感,统一小写查找
  const getHeader = (name: string): string => {
    const lower = name.toLowerCase();
    for (const [k, v] of Object.entries(headers)) {
      if (k.toLowerCase() === lower) return v;
    }
    return '';
  };

  // (2) Set-Cookie 含 locale=expectedLocale
  const setCookie = getHeader('set-cookie') || '';
  if (!setCookie) {
    throw new Error(
      'locale 重定向响应缺少 Set-Cookie 头(R61 P1-08: locale 切换必须写入 locale cookie)'
    );
  }
  const cookieName = expected.cookieName || 'locale';
  // Set-Cookie 格式: "locale=zh-CN; Path=/; HttpOnly; SameSite=Strict"
  const cookiePattern = new RegExp(
    `\\b${escapeRegex(cookieName)}=${escapeRegex(expected.expectedLocale)}\\b`
  );
  if (!cookiePattern.test(setCookie)) {
    throw new Error(
      `locale cookie 不匹配:\n  expected: ${cookieName}=${expected.expectedLocale}\n  actual:   ${setCookie}\n` +
      `(R61 P1-08: locale 切换必须设置正确 cookie 值)`
    );
  }

  // (3) Referer 安全:Referrer-Policy 必须为安全值之一
  //   admin/__init__.py 的 CSP 中间件已设 Referrer-Policy: strict-origin-when-cross-origin
  const SAFE_REFERRER_POLICIES = new Set([
    'no-referrer',
    'same-origin',
    'strict-origin',
    'strict-origin-when-cross-origin',
  ]);
  const referrerPolicy = getHeader('referrer-policy') || '';
  if (!referrerPolicy) {
    throw new Error(
      'locale 重定向响应缺少 Referrer-Policy 头(R61 P1-08: 防止外部 Referer 泄漏)'
    );
  }
  if (!SAFE_REFERRER_POLICIES.has(referrerPolicy.toLowerCase())) {
    throw new Error(
      `Referrer-Policy 不安全:\n  expected (one of): ${Array.from(SAFE_REFERRER_POLICIES).join(' / ')}\n  actual:   ${referrerPolicy}\n` +
      `(R61 P1-08: 禁止 unsafe-url / no-referrer-when-downgrade 等泄漏完整 URL 的策略)`
    );
  }

  // (4) 最终重定向路径严格匹配 expectedPath(用 assertUrlEquals 严格比较)
  assertUrlEquals(finalUrl, { pathname: expected.expectedPath });
}

/** 转义正则元字符(用于构建字面量正则)。 */
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
