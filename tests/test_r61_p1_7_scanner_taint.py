"""R61 P1-07: 硬编码 scanner taint / source-to-sink 模型测试。

审计要求 P1-07: 硬编码 scanner 仍只扫描已知 sink。本测试覆盖 taint 模型整改:
1. 所有字符串字面量默认可疑(流入 sink 即 tainted)
2. sink 注册表扩展(render context / Response / JSONResponse / HTMLResponse /
   bot wrapper / 邮件 / 通知 等)
3. 协议常量显式豁免(HTTP 状态 / UUID / 格式占位符 / 单字符分隔符)
4. exempt 函数(_i18n_t / translate / ErrorEnvelope 等)的字面量不被收集
5. logger.* 调用保持 log_only(baseline ratchet,非 user_visible)

5 个必需用例(a-e):
    (a) string 字面量入 JSONResponse 被标记 user_visible
    (b) string 入 send_message 被标记 user_visible
    (c) _i18n_t('key') 入相同 sink 不被标记(exempt)
    (d) allowlisted 协议常量("OK")入 sink 不被标记
    (e) logger 调用保持 log_only

额外覆盖:
    - 新 sink 注册表完整性(13 项新 sink 在 PYTHON_SINK_FUNCS 中)
    - call-chain 编码(sink:JSONResponse.content.dict[msg])
    - 协议常量正则(_is_protocol_constant)
    - 结构性 kwargs 豁免(status_code/headers/url 等)
    - 递归抽取(dict/list/tuple/Set/IfExp 嵌套字面量)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试能导入 scripts/scan_hardcoded_strings.py
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_hardcoded_strings as scan  # noqa: E402


# ─── 辅助函数 ──────────────────────────────────────────────────

def _classify(ptype: str) -> str:
    """包装 classify_finding(无需 file_path,pattern_type 已编码父调用)。"""
    return scan.classify_finding('test.py', ptype)


def _find_texts(findings) -> list[str]:
    """从 findings 抽取字面量文本列表(便于断言)。"""
    return [ct for _ln, _pt, ct in findings]


def _find_ptypes(findings) -> list[str]:
    """从 findings 抽取 pattern_type 列表(便于断言 call-chain)。"""
    return [pt for _ln, pt, _ct in findings]


# ===========================================================================
# 必需用例 (a-e)
# ===========================================================================

class TestRequiredCases:
    """R61 P1-07 必需 5 用例 — taint / source-to-sink 模型核心验证。"""

    def test_a_string_literal_into_jsonresponse_is_user_visible(self):
        """(a) string 字面量入 JSONResponse 被标记 user_visible。

        验证:
        - JSONResponse 是新注册的 sink(在 PYTHON_SINK_FUNCS 中)
        - content= 携带 dict 字面量时,递归抽取其中的字符串字面量
        - pattern_type 携带 call-chain(如 sink:JSONResponse.content.dict[msg])
        - classify_finding 归为 user_visible(绝对门禁)
        """
        src = (
            'from fastapi.responses import JSONResponse\n'
            'def handler():\n'
            '    return JSONResponse(content={"msg": "Operation succeeded"})\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 1 条 finding(递归抽取 dict[msg] 中的 "Operation succeeded")
        assert len(findings) == 1, f"期望 1 条 finding,实际 {findings}"
        line, ptype, text = findings[0]
        # call-chain 应含 JSONResponse + content + dict[msg]
        assert ptype.startswith('sink:JSONResponse'), f"ptype={ptype}"
        assert 'content' in ptype, f"ptype 应含 content kwarg: {ptype}"
        assert 'dict[msg]' in ptype, f"ptype 应含 dict[msg] chain: {ptype}"
        # 字面量文本应是被抽取的字符串
        assert text == 'Operation succeeded', f"text={text}"
        # classify 应归 user_visible(绝对门禁)
        assert _classify(ptype) == 'user_visible', f"ptype={ptype} 应归 user_visible"

    def test_b_string_into_send_message_is_user_visible(self):
        """(b) string 入 send_message 被标记 user_visible。

        验证:
        - send_message 是已注册 sink
        - 位置参数字符串字面量被收集
        - reply_text 同样被收集
        - 两者均归 user_visible
        """
        src = (
            'def handler(update, context):\n'
            '    update.message.reply_text("Please log in first")\n'
            '    context.bot.send_message(chat_id=123, text="Welcome back")\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 2 条 findings(reply_text 位置参数 + send_message text= kwarg)
        assert len(findings) == 2, f"期望 2 条 findings,实际 {findings}"
        texts = _find_texts(findings)
        assert 'Please log in first' in texts, f"texts={texts}"
        assert 'Welcome back' in texts, f"texts={texts}"
        # 两条均归 user_visible
        for _ln, ptype, _ct in findings:
            assert ptype.startswith('sink:'), f"ptype={ptype}"
            assert _classify(ptype) == 'user_visible', f"ptype={ptype} 应归 user_visible"

    def test_c_i18n_t_into_sink_not_flagged(self):
        """(c) _i18n_t('key') 入相同 sink 不被标记(exempt)。

        验证 taint 模型的核心豁免:
        - exempt 函数(_i18n_t / translate / ErrorEnvelope 等)的 Call 节点不被深入
        - 其内部的 i18n key 字面量("app.welcome")不被收集为违规
        - 无论是位置参数还是 kwarg,exempt Call 均被跳过
        """
        src = (
            'from services.i18n import translate as _i18n_t\n'
            'from fastapi.responses import JSONResponse\n'
            'def handler(update, context):\n'
            '    # exempt Call 入 JSONResponse(content=...) — 不应标记\n'
            '    return JSONResponse(content={"msg": _i18n_t("app.welcome")})\n'
            '    # exempt Call 入 reply_text 位置参数 — 不应标记\n'
            '    update.message.reply_text(_i18n_t("app.hello"))\n'
            '    # exempt Call 入 send_message text= kwarg — 不应标记\n'
            '    context.bot.send_message(chat_id=1, text=_i18n_t("app.bye"))\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 0 条 findings(_i18n_t 是 exempt,其字面量不被收集)
        assert findings == [], (
            f"_i18n_t('key') 入 sink 不应被标记,但得到 findings={findings}"
        )

    def test_d_allowlisted_protocol_constant_not_flagged(self):
        """(d) allowlisted 协议常量("OK")入 sink 不被标记。

        验证协议常量豁免:
        - "OK" / "ok" / "FAIL" / "200" / "404" 等 HTTP/JSON 状态值允许流入 sink
        - "{}" / "%s" / "%d" 等格式占位符允许流入 sink
        - ":" / "," / "-" / "_" 等单字符分隔符允许流入 sink
        - UUID / hex / 纯数字允许流入 sink
        这些是机器可读结构常量,非自然语言文本,允许流入用户面向 sink。
        """
        src = (
            'from fastapi.responses import JSONResponse\n'
            'def handler():\n'
            '    # 协议常量入 sink — 不应标记\n'
            '    a = JSONResponse(content={"status": "OK"})\n'
            '    b = JSONResponse(content={"code": "200"})\n'
            '    c = JSONResponse(content={"result": "ok"})\n'
            '    d = JSONResponse(content={"sep": ":"})\n'
            '    e = JSONResponse(content={"fmt": "{}"})\n'
            '    return a, b, c, d, e\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 0 条 findings(所有字面量都是协议常量)
        assert findings == [], (
            f"协议常量入 sink 不应被标记,但得到 findings={findings}"
        )

    def test_e_logger_calls_remain_log_only(self):
        """(e) logger 调用保持 log_only(baseline ratchet,非 user_visible)。

        验证 logger.*/logging.*/print() 调用的字面量:
        - pattern_type 以 'log:' 开头(不是 'sink:')
        - classify_finding 归为 log_only
        - 即使是中文字面量也归 log_only(不归 user_visible)
        """
        src = (
            'from loguru import logger\n'
            'def handler():\n'
            '    logger.info("Processing started")\n'
            '    logger.warning("Cache miss for key")\n'
            '    logger.error("Connection refused")\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 3 条 findings(每个 logger 调用 1 条)
        assert len(findings) == 3, f"期望 3 条 findings,实际 {findings}"
        for _ln, ptype, _ct in findings:
            assert ptype.startswith('log:'), (
                f"logger 调用 ptype 应以 'log:' 开头: {ptype}"
            )
            assert _classify(ptype) == 'log_only', (
                f"logger 调用应归 log_only: {ptype}"
            )


# ===========================================================================
# 额外覆盖:新 sink 注册表完整性
# ===========================================================================

class TestSinkRegistryExpansion:
    """R61 P1-07: 验证 13 个新 sink 已在 PYTHON_SINK_FUNCS 中注册。"""

    REQUIRED_NEW_SINKS = frozenset({
        # Web 框架响应 sink
        'JSONResponse', 'HTMLResponse', 'PlainResponse', 'Response',
        # render context
        'TemplateResponse', 'render', 'render_template',
        # 邮件 / 通知 sink
        'send_mail', 'send_email', 'email_message',
        'notify', 'push_notification', 'send_notification',
    })

    def test_all_new_sinks_registered(self):
        """13 个新 sink 必须全部在 PYTHON_SINK_FUNCS 中(新 sink 必须先注册)。"""
        missing = self.REQUIRED_NEW_SINKS - scan.PYTHON_SINK_FUNCS
        assert not missing, f"未注册的新 sink: {missing}"

    def test_redirectresponse_not_in_sinks(self):
        """RedirectResponse 不应在 PYTHON_SINK_FUNCS 中(仅携带 URL,非用户文本)。

        验证假阳性修复:RedirectResponse(url="/login") 的 url 是结构性参数,
        不应被识别为 user_visible sink。
        """
        assert 'RedirectResponse' not in scan.PYTHON_SINK_FUNCS, (
            "RedirectResponse 不应注册为 sink(url 是结构性参数,非用户文本)"
        )

    def test_structural_kwargs_defined(self):
        """结构性 kwargs 集合已定义(status_code/headers/url 等)。"""
        required = {'status_code', 'headers', 'url', 'content_type',
                    'media_type', 'cookies', 'background', 'charset'}
        assert required <= scan.SINK_STRUCTURAL_KWARGS, (
            f"结构性 kwargs 缺失: {required - scan.SINK_STRUCTURAL_KWARGS}"
        )

    def test_scanner_version_bumped_to_6(self):
        """scanner 版本升至 6.0(P1-07 整改)。"""
        assert scan.SCANNER_VERSION == "6.0", (
            f"SCANNER_VERSION 应为 '6.0',实际 '{scan.SCANNER_VERSION}'"
        )


# ===========================================================================
# 额外覆盖:call-chain 编码
# ===========================================================================

class TestCallChainEncoding:
    """R61 P1-07: call-chain 编码 — pattern_type 携带从 sink 到字面量的路径。"""

    def test_direct_positional_arg_no_chain_suffix(self):
        """直接位置参数字面量 — pattern_type 为 sink:<name>(无 chain 后缀)。"""
        src = (
            'def handler(update):\n'
            '    update.message.reply_text("hello world")\n'
        )
        findings = scan.scan_python_content(src)
        assert len(findings) == 1
        _ln, ptype, _ct = findings[0]
        assert ptype == 'sink:reply_text', f"ptype={ptype}"

    def test_kwarg_in_chain(self):
        """关键字参数 — pattern_type 为 sink:<name>.<kwarg>。"""
        src = (
            'from fastapi import HTTPException\n'
            'def handler():\n'
            '    raise HTTPException(status_code=404, detail="Not found")\n'
        )
        findings = scan.scan_python_content(src)
        assert len(findings) == 1
        _ln, ptype, _ct = findings[0]
        # status_code 是结构性 kwarg(跳过),detail 是 sink kwarg
        assert ptype == 'sink:HTTPException.detail', f"ptype={ptype}"

    def test_nested_dict_chain(self):
        """嵌套 dict 字面量 — call-chain 含 dict[key]。"""
        src = (
            'from fastapi.responses import JSONResponse\n'
            'def handler():\n'
            '    return JSONResponse(content={"msg": "hello", "err": "bad"})\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 2 条 finding(msg + err 两个 key 的字面量)
        assert len(findings) == 2, f"findings={findings}"
        ptypes = _find_ptypes(findings)
        assert 'sink:JSONResponse.content.dict[msg]' in ptypes, f"ptypes={ptypes}"
        assert 'sink:JSONResponse.content.dict[err]' in ptypes, f"ptypes={ptypes}"

    def test_nested_list_chain(self):
        """嵌套 list 字面量 — call-chain 含 [index]。"""
        src = (
            'def handler(update):\n'
            '    update.message.reply_text(["keep", "drop me"])\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 2 条 finding(list[0] + list[1])
        assert len(findings) == 2, f"findings={findings}"
        ptypes = _find_ptypes(findings)
        assert 'sink:reply_text.[0]' in ptypes, f"ptypes={ptypes}"
        assert 'sink:reply_text.[1]' in ptypes, f"ptypes={ptypes}"


# ===========================================================================
# 额外覆盖:协议常量豁免
# ===========================================================================

class TestProtocolConstantExemption:
    """R61 P1-07: 协议常量豁免(_is_protocol_constant)。"""

    @pytest.mark.parametrize("text", [
        # HTTP/JSON 状态值
        'OK', 'FAIL', 'ok', 'fail', 'not_ready', 'unknown', 'ready', 'passed',
        # 布尔/null 字面量
        'true', 'false', 'null', 'none', 'None', 'True', 'False',
        # HTTP 状态码
        '200', '201', '204', '301', '302', '400', '401', '403', '404',
        '409', '422', '429', '500', '502', '503',
        # 单字符分隔符 / 标点
        '', ' ', ':', ',', ';', '-', '_', '/', '.', '|', '=', '?', '!', '@', '#',
        # 格式占位符
        '{}', '%s', '%d', '%r', '%i', '%f',
        # UUID
        '12345678-1234-1234-1234-123456789012',
        # hex / 纯数字
        '0xff', '12345',
        # {key} 形式(loguru / f-string 占位)
        '{key}', '{name}',
    ])
    def test_protocol_constants_exempt(self, text):
        """协议常量应被豁免(_is_protocol_constant 返回 True)。"""
        assert scan._is_protocol_constant(text) is True, (
            f"协议常量 '{text}' 应被豁免"
        )

    @pytest.mark.parametrize("text", [
        # 自然语言文本(英文)— 不应豁免
        'Operation succeeded', 'Welcome back', 'Please log in',
        # 自然语言文本(中文)— 不应豁免
        '操作成功', '请登录', '欢迎回来',
        # 含空格的非协议字符串
        'hello world', 'not a protocol',
        # 表名 / 列名 / 键前缀(技术标识符)— 不豁免
        # (审计要求:技术标识符若流入用户面向 sink 仍可能是用户可见文本)
        'users', 'config_key', 'admin_principals',
    ])
    def test_non_protocol_not_exempt(self, text):
        """非协议常量(自然语言/技术标识符)不应被豁免。"""
        assert scan._is_protocol_constant(text) is False, (
            f"非协议常量 '{text}' 不应被豁免"
        )


# ===========================================================================
# 额外覆盖:结构性 kwargs 豁免(假阳性修复)
# ===========================================================================

class TestStructuralKwargsExemption:
    """R61 P1-07: 结构性 kwargs 豁免(status_code/headers/url/content_type 等)。

    验证假阳性修复:
    - HTTPException(status_code=404, headers={...}) — status_code/headers 跳过
    - RedirectResponse(url="/login") — url 跳过(且 RedirectResponse 不是 sink)
    - subprocess.run(..., text=True) — text=True 是布尔标志,不触发 sink
    """

    def test_http_exception_status_code_not_flagged(self):
        """HTTPException(status_code=404) 的 status_code 不应被标记(结构性 kwarg)。"""
        src = (
            'from fastapi import HTTPException\n'
            'def handler():\n'
            '    raise HTTPException(status_code=404, detail="Not found")\n'
        )
        findings = scan.scan_python_content(src)
        # 只应标记 detail="Not found"(1 条),status_code=404 跳过
        assert len(findings) == 1, f"findings={findings}"
        _ln, ptype, text = findings[0]
        assert 'detail' in ptype, f"ptype={ptype}"
        assert text == 'Not found', f"text={text}"

    def test_http_exception_headers_not_flagged(self):
        """HTTPException(headers={...}) 的 headers 不应被标记(结构性 kwarg)。"""
        src = (
            'from fastapi import HTTPException\n'
            'def handler():\n'
            '    raise HTTPException(\n'
            '        status_code=401,\n'
            '        headers={"WWW-Authenticate": "Basic realm=Admin"},\n'
            '    )\n'
        )
        findings = scan.scan_python_content(src)
        # headers 是结构性 kwarg,跳过;status_code 也是结构性 kwarg,跳过
        # 应有 0 条 findings(无 detail kwarg)
        assert findings == [], (
            f"headers/status_code 不应被标记,但得到 findings={findings}"
        )

    def test_subprocess_text_true_not_flagged(self):
        """subprocess.run(..., text=True) 的 text=True 不应触发 sink(布尔标志)。

        验证 _is_potential_string_arg 修复:text=True 的值是 bool Constant,
        不是字符串型,不触发 sink 判定。
        """
        src = (
            'import subprocess\n'
            'def handler():\n'
            '    result = subprocess.run(\n'
            '        ["git", "rev-parse", "HEAD"],\n'
            '        capture_output=True, text=True, timeout=5,\n'
            '    )\n'
            '    return result.stdout\n'
        )
        findings = scan.scan_python_content(src)
        # subprocess.run 不是 sink;text=True 是布尔标志,不触发 sink
        # 应有 0 条 findings
        assert findings == [], (
            f"subprocess.run(text=True) 不应触发 sink,但得到 findings={findings}"
        )


# ===========================================================================
# 额外覆盖:递归抽取(dict/list/tuple/Set/IfExp 嵌套)
# ===========================================================================

class TestRecursiveExtraction:
    """R61 P1-07: 递归抽取 — 支持 dict/list/tuple/Set/IfExp 嵌套字面量。"""

    def test_tuple_nested_extraction(self):
        """tuple 字面量入 sink — 递归抽取每个元素。"""
        src = (
            'def handler(update):\n'
            '    update.message.reply_text(("first error", "second error"))\n'
        )
        findings = scan.scan_python_content(src)
        assert len(findings) == 2, f"findings={findings}"
        texts = sorted(_find_texts(findings))
        assert texts == ['first error', 'second error'], f"texts={texts}"

    def test_ifexp_nested_extraction(self):
        """IfExp 条件表达式入 sink — 递归抽取 body/orelse 两个分支。"""
        src = (
            'def handler(update, cond):\n'
            '    update.message.reply_text("yes" if cond else "no")\n'
        )
        findings = scan.scan_python_content(src)
        # IfExp 两个分支的字面量都应被抽取
        assert len(findings) == 2, f"findings={findings}"
        texts = _find_texts(findings)
        assert 'yes' in texts and 'no' in texts, f"texts={texts}"

    def test_deeply_nested_dict_in_list(self):
        """深度嵌套:list 内含 dict,dict 内含字面量 — 递归抽取。"""
        src = (
            'from fastapi.responses import JSONResponse\n'
            'def handler():\n'
            '    return JSONResponse(content=[{"a": "x"}, {"b": "y"}])\n'
        )
        findings = scan.scan_python_content(src)
        # 应有 2 条 finding(list[0].dict[a] + list[1].dict[b])
        assert len(findings) == 2, f"findings={findings}"
        ptypes = _find_ptypes(findings)
        assert 'sink:JSONResponse.content.[0].dict[a]' in ptypes, f"ptypes={ptypes}"
        assert 'sink:JSONResponse.content.[1].dict[b]' in ptypes, f"ptypes={ptypes}"


# ===========================================================================
# 额外覆盖:CI 门禁(--check 退出 0)
# ===========================================================================

class TestCIGate:
    """R61 P1-07: CI 门禁 — scanner --check 必须退出 0(user_visible=0)。"""

    def test_scanner_check_exits_zero(self):
        """运行 scan_hardcoded_strings.py --check 应退出 0。

        验证:
        - 所有模块 user_visible=0(绝对门禁)
        - log_only 未超过 baseline(services/ baseline 已提升至 1256)
        - scanner_version=6.0
        """
        import subprocess
        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "scripts/scan_hardcoded_strings.py", "--check"],
            cwd=project_root,
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"scanner --check 应退出 0,实际退出 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 必须含绝对门禁通过提示
        assert "R56 §5.1 绝对门禁通过" in result.stdout, (
            f"未看到绝对门禁通过提示:\n{result.stdout}"
        )

    def test_baseline_scanner_version_is_6(self):
        """baseline.json 的 scanner_version 必须为 6.0。"""
        import json
        baseline_path = Path(__file__).resolve().parent.parent / "locales" / "baseline.json"
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert data.get("scanner_version") == "6.0", (
            f"baseline scanner_version 应为 '6.0',实际 '{data.get('scanner_version')}'"
        )


if __name__ == "__main__":
    # 支持直接运行:python tests/test_r61_p1_7_scanner_taint.py
    pytest.main([__file__, "-x", "-q"])
