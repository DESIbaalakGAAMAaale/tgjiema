# R40 P2-10: 国际化(i18n)与无障碍(WCAG 2.2 AA)实施指南

本指南为 tgjiema 项目提供国际化与无障碍最佳实践,目标是达到 **WCAG 2.2 AA 级别** 合规。

## 1. 国际化(i18n)架构

### 1.1 locale 文件结构

locale 文件存放于 `locales/` 目录,JSON 格式:

```
locales/
├── zh-CN.json   # 简体中文(默认 locale)
└── en-US.json   # English (United States)
```

每个 locale 文件包含以下命名空间:

| 命名空间 | 用途 | 示例 |
|---|---|---|
| `meta` | locale 元信息(name/version/fallback) | `{"locale": "zh-CN", "fallback": "en-US"}` |
| `common` | 通用 UI 文案(ok/cancel/search 等) | `{"ok": "确定"}` |
| `errors` | 错误码翻译(与 ErrorCode 枚举对应) | `{"quota.decode.exceeded": "今日解码次数已达上限"}` |
| `ui` | Admin Web 页面标题与按钮文案 | `{"admin_login_title": "管理后台登录"}` |
| `bot` | Bot 用户交互文案 | `{"decode_success": "解码成功"}` |
| `accessibility` | 无障碍相关文案(alt/aria-label) | `{"skip_to_content": "跳到主要内容"}` |

### 1.2 翻译 key 命名规范

- **点分命名空间**:`errors.quota.decode.exceeded`(对应 ErrorCode 枚举值)
- **snake_case**:同一命名空间内用下划线分隔单词:`bot.decode_in_progress`
- **避免缩写**:`admin_dashboard_title` 而非 `admin_dash_title`
- **保持稳定**:key 一旦发布,不可随意改名(会破坏向后兼容)

### 1.3 占位符插值

使用 `{placeholder}` 格式(Python `str.format` 兼容):

```json
{
  "bot": {
    "quota_remaining": "今日剩余配额 {count} 次",
    "upload_success": "上传成功,文件码: {code}"
  }
}
```

调用方式:

```python
from services.i18n import translate

msg = translate("bot.quota_remaining", locale="zh-CN", count=5)
# 输出: "今日剩余配额 5 次"
```

### 1.4 用户 locale 持久化

`users_local.locale` 列存储用户语言偏好,默认 `zh-CN`:

```sql
ALTER TABLE users_local ADD COLUMN locale TEXT DEFAULT 'zh-CN';
```

Bot 处理消息时按以下顺序确定 locale:

1. 用户显式设置(`/setlang en-US` 命令)
2. `users_local.locale` 字段
3. 默认 `zh-CN`

### 1.5 I18nManager API

```python
from services.i18n import I18nManager, translate, get_i18n_manager

# 单例方式
manager = get_i18n_manager()
msg = manager.translate("errors.file.not_found", locale="zh-CN")

# 模块级便捷函数
msg = translate("errors.file.not_found", locale="en-US")

# 列出可用 locale
locales = manager.get_available_locales()  # ["zh-CN", "en-US"]

# 切换默认 locale
manager.set_default_locale("en-US")

# 检查 key 是否存在
if manager.has_key("errors.file.not_found", locale="zh-CN"):
    ...

# 重新加载(开发期热更新)
manager.reload_all()
```

## 2. 无障碍(WCAG 2.2 AA)检查清单

### 2.1 感知性(Perceivable)

#### 1.1.1 非文本内容(A 级)
- [ ] 所有 `<img>` 标签提供 `alt` 属性(描述性文字)
- [ ] 装饰性图片使用空 `alt=""`(被屏幕阅读器跳过)
- [ ] 图标按钮提供 `aria-label`(如关闭按钮 `<button aria-label="关闭">×</button>`)
- [ ] 上传文件提供 `aria-describedby` 关联文件说明

#### 1.2.1 纯音频和视频(A 级)
- [ ] 预录音频提供文字稿(transcript)
- [ ] 预录视频提供字幕(caption)
- [ ] 直播音频/视频提供实时字幕

#### 1.3.1 信息和关系(A 级)
- [ ] 使用语义化 HTML(`<header>`/`<main>`/`<nav>`/`<footer>`)
- [ ] 表单字段使用 `<label>` 关联(`for` 属性匹配 `id`)
- [ ] 数据表格使用 `<thead>`/`<tbody>`/`<th scope="col">`
- [ ] 列表使用 `<ul>`/`<ol>` 而非 `<div>` + 项目符号

#### 1.4.3 对比度(AA 级)
- [ ] 普通文字(小于 18pt)对比度 ≥ 4.5:1
- [ ] 大文字(≥ 18pt 或 14pt 粗体)对比度 ≥ 3:1
- [ ] 禁用纯灰色 `#999` 在白色背景上的低对比组合
- [ ] 链接文字与正文文字有视觉区分(下划线或颜色差异)

#### 1.4.4 文字调整(A 级)
- [ ] 支持浏览器缩放至 200% 不破坏布局
- [ ] 不使用 `px` 固定字号,改用 `rem`/`em`/`%`
- [ ] 不禁用用户代理样式表(`!important` 滥用)

#### 1.4.10 重排(Reflow,AA 级)
- [ ] 在 320px 宽度下不出现横向滚动条
- [ ] 响应式布局(`@media` 查询)
- [ ] 表格在小屏上转为卡片布局或允许横向滚动

### 2.2 可操作性(Operable)

#### 2.1.1 键盘可访问(A 级)
- [ ] 所有交互元素可通过 Tab 键访问
- [ ] 焦点顺序符合阅读顺序(DOM 顺序)
- [ ] 不使用 `tabindex="-1"` 隐藏关键元素
- [ ] 模态对话框支持 Esc 关闭

#### 2.1.2 无键盘陷阱(A 级)
- [ ] 模态对话框内 Tab 不会跳出对话框
- [ ] 视频播放器焦点不陷入控件
- [ ] 嵌入 iframe 内容可 Tab 退出

#### 2.4.1 跳过块(A 级)
- [ ] 提供跳到主要内容的链接(`<a href="#main">跳到主要内容</a>`)
- [ ] 跳过链接在获得焦点时可见(不 `display:none`)
- [ ] Admin Web 模板含 `id="main"` 锚点

#### 2.4.3 焦点顺序(A 级)
- [ ] Tab 顺序与视觉阅读顺序一致(从上到下,从左到右)
- [ ] 不使用 `tabindex` 正整数改变自然顺序(除非必要)

#### 2.4.7 焦点可见(AA 级)
- [ ] 所有可聚焦元素有可见的 `:focus` 样式
- [ ] 不使用 `outline: none` 移除焦点轮廓(除非提供替代)
- [ ] 焦点轮廓对比度 ≥ 3:1
- [ ] Admin Web 已通过 CSP nonce 注入样式,确保焦点样式不被剥离

#### 2.5.3 标签包含名称(A 级)
- [ ] 可点击元素的可见文字包含其 `aria-label`
- [ ] 图标按钮可见文字(如 "×")与 `aria-label`(如 "关闭")有视觉关联

### 2.3 可理解性(Understandable)

#### 3.1.1 页面语言(A 级)
- [ ] `<html lang="zh-CN">` 设置页面主语言
- [ ] 动态切换 locale 时同步更新 `<html lang>` 属性
- [ ] Admin Web 模板已设置 `lang="zh-CN"`

#### 3.1.2 部分语言(AA 级)
- [ ] 页内外文片段使用 `lang` 属性标注(如 `<span lang="en">API</span>`)
- [ ] 引用外语术语标注原语言

#### 3.2.1 获得焦点(A 级)
- [ ] 元素获得焦点不触发上下文变化(不自动提交表单)
- [ ] 不自动跳转页面(除非明确告知用户)

#### 3.2.2 输入(A 级)
- [ ] 表单字段提供可见的标签(`<label>`)
- [ ] 必填字段用 `aria-required="true"` 或视觉标记(如 `*`)
- [ ] 输入格式提示使用 `aria-describedby`(如密码强度要求)
- [ ] 错误消息使用 `aria-live="assertive"` 实时通知

#### 3.3.1 错误识别(A 级)
- [ ] 表单校验错误用文字描述(非仅颜色)
- [ ] 错误消息关联到对应字段(`aria-describedby` + `aria-invalid="true"`)
- [ ] Admin Web 登录失败返回错误消息(非仅 HTTP 401)

#### 3.3.3 错误建议(AA 级)
- [ ] 错误消息提供修正建议(如 "邮箱格式错误,应为 user@example.com")
- [ ] DomainError `details` 字段携带修正提示

### 2.4 健壮性(Robust)

#### 4.1.1 解析(A 级)
- [ ] HTML 通过 W3C Validator 验证
- [ ] 不使用重复 `id` 属性
- [ ] 所有标签正确闭合
- [ ] Admin Web 模板使用 Jinja2 自动转义,避免 XSS

#### 4.1.2 名称、角色、值(A 级)
- [ ] 自定义控件提供 `role` 属性(如 `role="button"`)
- [ ] 使用 ARIA Live Regions 通知动态更新(`aria-live="polite"`)
- [ ] 表单控件暴露状态(`aria-checked`/`aria-expanded`/`aria-disabled`)
- [ ] 模态对话框使用 `role="dialog"` + `aria-modal="true"`

## 3. Admin Web 无障碍实施

### 3.1 登录页(/login)无障碍检查清单

- [ ] `<html lang="zh-CN">` 已设置
- [ ] 表单字段有 `<label>` 关联(`for` 属性匹配 `id`)
- [ ] 密码字段 `autocomplete="current-password"`
- [ ] 用户名字段 `autocomplete="username"`
- [ ] 错误消息使用 `role="alert"` 或 `aria-live="assertive"`
- [ ] 登录按钮有可见的 `:focus` 样式
- [ ] 表单可通过键盘提交(Enter 键)

### 3.2 CSP nonce 与无障碍

Admin Web 使用 per-request CSP nonce(`services/admin/__init__.py`):

```http
Content-Security-Policy:
  script-src 'self' 'nonce-<random>';
  style-src 'self' 'nonce-<random>';
```

确保:
- [ ] 内联 `<style>` 标签使用 `nonce` 属性
- [ ] 不使用 `style="..."` 行内样式(被 CSP 拒绝)
- [ ] 焦点样式通过 nonce `<style>` 注入(不被剥离)

### 3.3 表格无障碍

```html
<table>
  <caption>用户列表</caption>
  <thead>
    <tr>
      <th scope="col">用户名</th>
      <th scope="col">会员等级</th>
      <th scope="col">操作</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>alice</td>
      <td>premium</td>
      <td><button aria-label="编辑用户 alice">编辑</button></td>
    </tr>
  </tbody>
</table>
```

检查清单:
- [ ] 表格有 `<caption>` 标题
- [ ] 表头使用 `<th scope="col">`
- [ ] 操作按钮提供 `aria-label`(含上下文,如 "编辑用户 alice")
- [ ] 大表格支持横向滚动(`<div class="table-scroll">`)

## 4. Bot 无障碍考虑

Telegram Bot 本身不在 WCAG 范围内,但应考虑:

- [ ] 消息文案简洁(避免冗长)
- [ ] 文件码使用纯 ASCII(便于屏幕阅读器朗读)
- [ ] 错误消息提供下一步操作建议
- [ ] 多语言支持(根据 `users_local.locale` 切换)
- [ ] 不依赖图片传达关键信息(纯文字 fallback)

## 5. 自动化测试与验证

### 5.1 单元测试

- [ ] `tests/test_r40_p2_i18n.py` 验证 locale 加载与翻译查找
- [ ] locale 文件 JSON 结构验证
- [ ] 翻译 key 一致性检查(zh-CN 与 en-US 的 errors 命名空间 key 应一致)

### 5.2 集成测试

- [ ] Admin Web 页面渲染不破坏无障碍结构
- [ ] locale 切换后 `<html lang>` 同步更新
- [ ] 表单提交错误消息通过 `aria-live` 通知

### 5.3 工具验证

- [ ] axe DevTools 浏览器扩展扫描每个页面
- [ ] Lighthouse Accessibility 评分 ≥ 90
- [ ] WAVE 评估工具检测无错误
- [ ] 键盘导航测试(仅用 Tab/Shift+Tab/Enter/Esc)

## 6. 持续维护

### 6.1 新增翻译 key 流程

1. 在 `locales/zh-CN.json` 和 `locales/en-US.json` 中同时新增 key
2. 在代码中使用 `translate("namespace.key")` 而非硬编码字符串
3. 运行 `py -m pytest tests/test_r40_p2_i18n.py -v` 验证
4. 提交 PR 时附上截图证明翻译生效

### 6.2 新增 locale 流程

1. 在 `locales/` 目录新增 `<locale>.json`(如 `ja-JP.json`)
2. 复制 zh-CN.json 结构,翻译 value
3. 设置 `meta.fallback` 为 `zh-CN` 或 `en-US`
4. 运行 `manager.load_locale("ja-JP")` 验证
5. 在 Bot 添加 `/setlang ja-JP` 命令支持

### 6.3 文档更新

- 每次新增无障碍特性,更新本指南对应检查项
- 每次发布前运行 axe 扫描,记录问题到 issue tracker
- 每季度评审 WCAG 2.2 AA 合规状态

## 7. 参考资料

- [WCAG 2.2 官方文档](https://www.w3.org/TR/WCAG22/)
- [WCAG 2.2 AA 检查清单(简称)](https://webaim.org/standards/wcag/checklist)
- [ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/)
- [axe DevTools](https://www.deque.com/axe/devtools/)
- [Lighthouse Accessibility](https://web.dev/lighthouse-accessibility/)
- [Python i18n 文档](https://docs.python.org/3/library/gettext.html)

---

**版本**: 1.0
**最后更新**: 2026-07-13
**维护者**: tgjiema 工程团队
**审查周期**: 每季度
