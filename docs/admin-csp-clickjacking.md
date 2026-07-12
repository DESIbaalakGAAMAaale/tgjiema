# R39 P2-8: Admin CSP Nonce + 点击劫持防护

## 背景

R39 终审指出: "对 Admin 模板增加 CSP nonce、点击劫持和内容安全测试。"

原 Admin 后台未设置 Content-Security-Policy 头,
存在风险:
- **XSS 注入**: 攻击者注入 inline script 可窃取 Basic Auth 凭证
- **点击劫持**: 恶意网站通过 iframe 嵌入 Admin 页面,诱导用户点击
- **MIME 嗅探**: 浏览器可能将非脚本响应解析为脚本执行

## 1. 整改方案

在 `admin/__init__.py` 添加 HTTP 中间件,为每个响应注入安全头:

```python
@app.middleware("http")
async def _csp_and_clickjacking_middleware(request: Request, call_next):
    """R39 P2-8: 为每个响应添加安全头。"""
    import secrets as _secrets_mod
    # 生成 per-request CSP nonce (16 字节 base64)
    csp_nonce = _secrets_mod.token_urlsafe(16)
    request.state.csp_nonce = csp_nonce

    response = await call_next(request)

    # CSP 头 — 只允许 nonce 匹配的 inline script/style
    csp_header = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{csp_nonce}'; "
        f"style-src 'self' 'nonce-{csp_nonce}'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Content-Security-Policy"] = csp_header
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
```

## 2. CSP 头详解

| 指令 | 值 | 用途 |
| ---- | -- | ---- |
| `default-src` | `'self'` | 默认只允许同源资源 |
| `script-src` | `'self' 'nonce-<nonce>'` | 只允许同源 + nonce 匹配的 inline script |
| `style-src` | `'self' 'nonce-<nonce>'` | 只允许同源 + nonce 匹配的 inline style |
| `img-src` | `'self' data:` | 允许同源图片 + data URI |
| `frame-ancestors` | `'none'` | 禁止任何页面通过 iframe 嵌入(防点击劫持) |
| `base-uri` | `'self'` | 防止 `<base>` 标签劫持 |
| `form-action` | `'self'` | 表单只能提交到同源 |

## 3. 模板适配

Admin 模板中的 inline script/style 需添加 nonce 属性:

```html
<!-- 修改前 -->
<script>
  document.getElementById('btn').onclick = ...
</script>

<!-- 修改后 -->
<script nonce="{{ request.state.csp_nonce }}">
  document.getElementById('btn').onclick = ...
</script>

<style nonce="{{ request.state.csp_nonce }}">
  .custom-class { color: red; }
</style>
```

外部引用的 CSS/JS 文件无需 nonce(CSP `script-src 'self'` 已允许同源)。

## 4. 其他安全头

| 头 | 值 | 用途 |
| -- | -- | ---- |
| `X-Frame-Options` | `DENY` | 旧浏览器兜底(等价于 CSP `frame-ancestors 'none'`) |
| `X-Content-Type-Options` | `nosniff` | 防 MIME 嗅探 |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | 跨源请求仅发送 origin,不泄露完整 URL |

## 5. 内容安全测试

### 5.1 手动验证

```bash
# 检查响应头
curl -I -u admin:password http://localhost:8080/

# 应包含:
# Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-...'; ...
# X-Frame-Options: DENY
# X-Content-Type-Options: nosniff
# Referrer-Policy: strict-origin-when-cross-origin
```

### 5.2 点击劫持测试

```html
<!-- 创建 test_clickjack.html,尝试 iframe 嵌入 Admin -->
<!DOCTYPE html>
<html>
<body>
  <h1>点击劫持测试</h1>
  <iframe src="http://localhost:8080/" width="800" height="600"></iframe>
</body>
</html>
```

预期: iframe 显示空白或错误(CSP `frame-ancestors 'none'` 拦截)。

### 5.3 XSS 注入测试

在 Admin 表单输入 `<script>alert('xss')</script>`:
- 预期: 脚本不执行(CSP `script-src 'nonce-...'` 拦截无 nonce 的 inline script)
- 浏览器控制台输出 CSP 违规报告

### 5.4 自动化扫描

使用 Mozilla Observatory 评分:

```bash
# 部署后访问:
https://observatory.mozilla.org/analyze/<your-domain>
# 预期评分: A+(CSP + X-Frame-Options + X-Content-Type-Options + Referrer-Policy)
```

## 6. 相关文件

- `admin/__init__.py` — CSP 中间件实现
- `admin/templates/` — Jinja2 模板(需添加 nonce 属性)
- `docs/least-privilege.md` — 最小权限原则
