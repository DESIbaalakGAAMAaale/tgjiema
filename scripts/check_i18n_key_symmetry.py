#!/usr/bin/env python3
"""R59 §5.1 P1: zh-CN.json 与 en-US.json key 对称检查(独立 fail-fast 门禁)。

校验规则(三条对称):
1. **key 集合对称**:两个 locale 的扁平化 key 集合必须完全一致(无孤儿 key)。
2. **参数集对称**:每个 key 引用的所有变量名集合必须一致。
   - 简单 {var} 占位符的变量名
   - ICU MessageFormat 子集 {var, plural/select/selectordinal, ...} 中的 selector 变量名
3. **ICU 结构对称**:每个 ICU 子句的 selector 集合必须一致。
   - 例如 ``{n, plural, =0 {..} one {..} other {..}}`` 的 selector 集合为 ``{"=0", "one", "other"}``
   - 同一 key 在 zh-CN / en-US 两侧必须提供完全一致的 selector 集合
   - 防止英文版漏掉 ``=0`` 子句或新增 ``few`` 子句而中文版没有,反之亦然

R59 §5.1 P1 要求 4:"缺变量和 malformed ICU 在 CI 中 fail-fast"。
本脚本在以下情况立即 exit 1:
    - 任一 locale 文件缺失 / JSON 解析失败
    - key 集合不对称(孤儿 key)
    - 任一 key 的参数集不对称
    - 任一 key 的 ICU selector 集合不对称
    - 任一 ICU 子句的括号不匹配 / selector 为空(malformed)

CI 调用方式:
    python scripts/check_i18n_key_symmetry.py

成功退出 0,失败退出 1。

设计说明:
    - 与 scripts/verify_i18n_keys.py 互补:后者做"key 存在性 + 占位符集合一致 + CLDR
      plural .one/.other 对称"等基础校验,本脚本专注于 R59 §5.1 P1 新增的
      "ICU 结构对称"(selector 集合完全一致)+ "参数集对称"(含 ICU var 名)。
    - 不依赖第三方 ICU 库(禁止增加 babel/pyicu 依赖),手工解析 ICU 子集。
    - 与 services/i18n.py 的 _ICU_PATTERN / _SIMPLE_VAR_PATTERN 保持一致。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# 扫描器版本(写入 artifact,便于 CI 下游脚本比对)
SCANNER_VERSION = "1.0"

# R59 §5.1 P1: ICU MessageFormat 子集 — 与 services/i18n.py 保持一致
# 匹配 {name, type, ...} 模式(type ∈ plural/select/selectordinal)
_ICU_PATTERN = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*(plural|select|selectordinal)\s*,"
)

# 简单 {var} 占位符(非 ICU pattern)
_SIMPLE_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# R61 P1-06: HTML 标签正则 — 检测 i18n 值中的 HTML 上下文不对称(注入风险)
# 匹配 <tag> / </tag> / <tag attr="..."> / <br/> 等合法 HTML 标签
# 不匹配 < 3 / a<b / {<var>} 等非 HTML 文本(< 后必须紧跟字母或 /)
_HTML_TAG_PATTERN = re.compile(r"<([a-zA-Z/][^>]*)>")


# ===========================================================================
# JSON 加载与扁平化
# ===========================================================================

def _load_json(filepath: Path) -> tuple[dict | None, str | None]:
    """加载 JSON 文件,返回 (data, error)。成功时 error=None。"""
    try:
        raw = filepath.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"根对象应为 dict,实际类型: {type(data).__name__}"
        return data, None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"
    except OSError as e:
        return None, f"文件读取失败: {e}"


def _flatten_values(obj: Any, prefix: str = "") -> dict[str, str]:
    """递归扁平化 dict,返回 {key: value} 映射。

    {"errors": {"quota.decode.exceeded": "x"}} -> {"errors.quota.decode.exceeded": "x"}
    非 dict 叶子值也按点分路径收集(str/int/bool/None 均转为 str)。
    排除 "meta" 顶层 key(允许两个 locale 的 meta 不同)。
    """
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        # 跳过 meta 顶层(允许两个 locale 的 fallback 等元信息不同)
        if not prefix and k == "meta":
            continue
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_values(v, full_key))
        else:
            # str / int / bool / None 一律转为 str(便于后续正则提取)
            result[full_key] = "" if v is None else str(v)
    return result


# ===========================================================================
# ICU 结构解析
# ===========================================================================

def _find_matching_brace(text: str, start: int) -> int:
    """从 text[start] == '{' 开始,查找匹配的 '}' 位置(考虑嵌套)。

    返回 '}' 的索引;若不匹配(到达字符串末尾仍未闭合),返回 -1(malformed)。
    """
    assert start < len(text) and text[start] == "{"
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1  # 未匹配


def _parse_icu_branches(body: str) -> tuple[set[str], str | None]:
    """解析 ICU plural/select/selectordinal 子句 body,返回 (selectors, error)。

    body 形如: ``=0 {none} one {# item} other {# items}``
    返回 selectors = ``{"=0", "one", "other"}``

    若 body malformed(某子句缺 `{` / 缺 `}` / selector 为空),返回 (selectors_so_far, error_msg)。
    """
    selectors: set[str] = set()
    i = 0
    n = len(body)
    while i < n:
        # 跳过空白
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        # 读取 selector(=N / one / other / male / female / ...)
        selector_start = i
        while i < n and not body[i].isspace() and body[i] != "{":
            i += 1
        selector = body[selector_start:i].strip()
        if not selector:
            # 空白后直接到末尾,正常结束
            break
        # 跳过空白直到 {
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != "{":
            # malformed: selector 后无 {
            return selectors, f"selector '{selector}' 后缺少 '{{'"
        # 读取 {..} 内容(考虑嵌套)
        end = _find_matching_brace(body, i)
        if end == -1:
            return selectors, f"selector '{selector}' 的 '{{...}}' 未闭合"
        selectors.add(selector)
        i = end + 1  # 跳过 }
    return selectors, None


def _extract_icu_structures(text: str) -> tuple[list[dict], list[str]]:
    """从字符串中提取所有 ICU 子句结构,返回 (structures, errors)。

    每个结构形如:
        {
            "var": "n",                  # ICU pattern 中的变量名
            "type": "plural",            # plural / select / selectordinal
            "selectors": {"=0", "one", "other"},  # 子句 selector 集合
        }

    若存在 malformed ICU(未闭合的 {、selector 后无 {),errors 收集错误描述。

    Args:
        text: 翻译值字符串

    Returns:
        (structures, errors) — structures 为结构列表,errors 为错误列表(空表示无 malformed)
    """
    structures: list[dict] = []
    errors: list[str] = []
    if not isinstance(text, str):
        return structures, errors

    # 找到所有 ICU pattern 起始位置
    for m in _ICU_PATTERN.finditer(text):
        var_name = m.group(1)
        block_type = m.group(2)
        # ICU pattern 的 '{' 在 m.start() 位置
        brace_start = m.start()
        # 找匹配的 '}'
        brace_end = _find_matching_brace(text, brace_start)
        if brace_end == -1:
            errors.append(f"ICU pattern '{var_name}, {block_type}, ...' 未闭合")
            continue
        # 提取 block 内容(去掉外层 {var, type, })
        # m.end() 指向 ',' 之后的第一个非空白字符
        # 实际 body 范围:从 m.end() 到 brace_end(不含)
        body = text[m.end():brace_end]
        # 解析子句 selector
        selectors, branch_err = _parse_icu_branches(body)
        if branch_err:
            errors.append(
                f"ICU pattern '{var_name}, {block_type}, ...' 子句解析失败: {branch_err}"
            )
        if not selectors and not branch_err:
            errors.append(
                f"ICU pattern '{var_name}, {block_type}, ...' 无任何 selector(空子句)"
            )
        structures.append({
            "var": var_name,
            "type": block_type,
            "selectors": selectors,
        })

    return structures, errors


def _extract_param_set(text: str) -> set[str]:
    """提取字符串中引用的所有变量名(简单 {var} + ICU var 名)。

    简单 {var} 占位符:`"剩余 {count} 次"` → {"count"}
    ICU pattern 中的 var:`"{n, plural, =0 {无} other {# 个}}"` → {"n"}

    两者合并去重,代表此 key 引用的全部参数集合。
    """
    if not isinstance(text, str):
        return set()
    params: set[str] = set()
    # ICU pattern 中的 var 名
    for m in _ICU_PATTERN.finditer(text):
        params.add(m.group(1))
    # 简单 {var} 占位符(与 ICU var 名合并)
    for m in _SIMPLE_VAR_PATTERN.finditer(text):
        params.add(m.group(1))
    return params


def _extract_html_tags(text: str) -> set[str]:
    """R61 P1-06: 提取文本中的 HTML 标签名集合(小写,不含属性,不含尖括号)。

    用于检测两侧 locale 的 HTML 安全上下文不对称:
        - 一侧含 HTML 标签而另一侧不含 → 不对称(HTML 注入风险)
        - 两侧都含 HTML 但标签集合不一致 → 不对称(渲染差异 / 注入风险)

    解析规则:
        ``<b>bold</b>``        → ``{"b"}``
        ``</b>``               → ``{"b"}``(闭合标签与开标签合并)
        ``<code class="x">``   → ``{"code"}``(忽略属性)
        ``<br/>``              → ``{"br"}``(自闭合)
        ``a < b`` / ``< 3``    → ``{}``(非 HTML,不匹配)

    Args:
        text: 翻译值字符串

    Returns:
        标签名集合(小写);无 HTML 标签时返回空集
    """
    if not isinstance(text, str):
        return set()
    tags: set[str] = set()
    for m in _HTML_TAG_PATTERN.finditer(text):
        inner = m.group(1).strip()
        if not inner:
            continue
        # 取第一个 token 作为标签名(忽略后续属性)
        name = inner.split()[0]
        # 去掉闭合斜杠 </b> → b 和自闭合斜杠 <br/> → br
        name = name.lstrip("/").rstrip("/").lower()
        # 仅保留合法标签名(字母,避免误匹配 <3 / <- 等)
        if name and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", name):
            tags.add(name)
    return tags


def _icu_struct_signature(structures: list[dict]) -> frozenset[tuple[str, str, frozenset[str]]]:
    """生成 ICU 结构签名(用于两侧比对)。

    返回 frozenset of (var, type, frozenset(selectors)),可哈希可比较。
    顺序无关(两侧 selector 集合顺序可能不同,但集合内容必须一致)。
    """
    return frozenset(
        (s["var"], s["type"], frozenset(s["selectors"]))
        for s in structures
    )


# ===========================================================================
# 主校验流程
# ===========================================================================

def verify() -> int:
    """主校验流程。返回退出码(0=成功,1=失败)。

    校验顺序(fail-fast):
        1. 文件存在性 + JSON 解析
        2. key 集合对称(无孤儿 key)
        3. 参数集对称(每个 key 的 {var} 集合一致)
        4. ICU 结构对称(每个 ICU 子句的 selector 集合一致)
        5. malformed ICU 检测(未闭合 / 空 selector)
        6. R61 P1-06: HTML 安全上下文对称(标签集合一致,防 HTML 注入风险)
    """
    zh_path = LOCALES_DIR / "zh-CN.json"
    en_path = LOCALES_DIR / "en-US.json"

    errors: list[str] = []

    # 1. 文件存在性 + JSON 解析(fail-fast)
    if not zh_path.exists():
        print(f"[FAIL] zh-CN.json 不存在: {zh_path}")
        return 1
    if not en_path.exists():
        print(f"[FAIL] en-US.json 不存在: {en_path}")
        return 1

    zh_data, zh_err = _load_json(zh_path)
    en_data, en_err = _load_json(en_path)
    if zh_err:
        print(f"[FAIL] zh-CN.json {zh_err}")
        return 1
    if en_err:
        print(f"[FAIL] en-US.json {en_err}")
        return 1

    # 2. 扁平化(排除 meta 顶层)
    zh_values = _flatten_values(zh_data)
    en_values = _flatten_values(en_data)

    zh_keys = set(zh_values.keys())
    en_keys = set(en_values.keys())

    # 3. key 集合对称检查
    zh_only = sorted(zh_keys - en_keys)
    en_only = sorted(en_keys - zh_keys)
    if zh_only:
        errors.append(
            f"zh-CN 独有 key(在 en-US 中缺失,共 {len(zh_only)} 个): {zh_only}"
        )
    if en_only:
        errors.append(
            f"en-US 独有 key(在 zh-CN 中缺失,共 {len(en_only)} 个): {en_only}"
        )

    # 4. 参数集对称 + ICU 结构对称(对公共 key 逐项比对)
    common_keys = sorted(zh_keys & en_keys)
    param_mismatches: list[str] = []
    icu_mismatches: list[str] = []
    html_mismatches: list[str] = []
    malformed_zh: list[str] = []
    malformed_en: list[str] = []

    for key in common_keys:
        zh_val = zh_values.get(key, "")
        en_val = en_values.get(key, "")

        # 4a. 参数集对称
        zh_params = _extract_param_set(zh_val)
        en_params = _extract_param_set(en_val)
        if zh_params != en_params:
            param_mismatches.append(
                f"{key}: zh-CN 参数集={sorted(zh_params)} "
                f"vs en-US 参数集={sorted(en_params)}"
            )

        # 4b. ICU 结构对称
        zh_structs, zh_struct_errs = _extract_icu_structures(zh_val)
        en_structs, en_struct_errs = _extract_icu_structures(en_val)

        # 4c. malformed ICU 收集(不影响后续比对,但最终 fail-fast)
        for e in zh_struct_errs:
            malformed_zh.append(f"{key}: {e}")
        for e in en_struct_errs:
            malformed_en.append(f"{key}: {e}")

        # 4d. ICU 结构签名比对(只比对两侧都无 malformed 的结构)
        # 若一侧 malformed,签名比对无意义(已在 malformed 列表记录)
        if not zh_struct_errs and not en_struct_errs:
            zh_sig = _icu_struct_signature(zh_structs)
            en_sig = _icu_struct_signature(en_structs)
            if zh_sig != en_sig:
                # 详细列出差异(便于开发者定位)
                zh_only_structs = zh_sig - en_sig
                en_only_structs = en_sig - zh_sig
                detail_parts: list[str] = []
                if zh_only_structs:
                    zh_detail = sorted(
                        f"({v},{t},{sorted(s)})"
                        for v, t, s in zh_only_structs
                    )
                    detail_parts.append(f"zh-CN 独有 ICU 结构={zh_detail}")
                if en_only_structs:
                    en_detail = sorted(
                        f"({v},{t},{sorted(s)})"
                        for v, t, s in en_only_structs
                    )
                    detail_parts.append(f"en-US 独有 ICU 结构={en_detail}")
                icu_mismatches.append(
                    f"{key}: ICU 结构不对称 {' | '.join(detail_parts)}"
                )

        # 4e. R61 P1-06: HTML 安全上下文对称
        # 一侧含 HTML 标签而另一侧不含 → 不对称(HTML 注入风险)
        # 两侧都含 HTML 但标签集合不一致 → 不对称(渲染差异 / 注入风险)
        zh_tags = _extract_html_tags(zh_val)
        en_tags = _extract_html_tags(en_val)
        if zh_tags != en_tags:
            html_mismatches.append(
                f"{key}: zh-CN HTML 标签={sorted(zh_tags)} "
                f"vs en-US HTML 标签={sorted(en_tags)}"
            )

    # 5. 汇总错误
    if param_mismatches:
        errors.append(
            f"参数集不对称(共 {len(param_mismatches)} 个 key 不一致):"
        )
        for m in param_mismatches:
            errors.append(f"  - {m}")

    if icu_mismatches:
        errors.append(
            f"ICU 结构不对称(共 {len(icu_mismatches)} 个 key 不一致):"
        )
        for m in icu_mismatches:
            errors.append(f"  - {m}")

    if html_mismatches:
        errors.append(
            f"HTML 安全上下文不对称(共 {len(html_mismatches)} 个 key 不一致,"
            f"R61 P1-06: HTML 注入风险):"
        )
        for m in html_mismatches:
            errors.append(f"  - {m}")

    if malformed_zh:
        errors.append(
            f"zh-CN malformed ICU(共 {len(malformed_zh)} 处,fail-fast):"
        )
        for m in malformed_zh:
            errors.append(f"  - {m}")

    if malformed_en:
        errors.append(
            f"en-US malformed ICU(共 {len(malformed_en)} 处,fail-fast):"
        )
        for m in malformed_en:
            errors.append(f"  - {m}")

    # 6. 输出结果
    if errors:
        print("[FAIL] R59 §5.1 P1 i18n key 对称检查失败:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(
        f"[OK] R59 §5.1 P1 i18n key 对称检查通过 "
        f"(zh-CN: {len(zh_keys)} keys, en-US: {len(en_keys)} keys, "
        f"key 集合/参数集/ICU 结构/HTML 安全上下文完全对称)"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """脚本入口。

    支持 --version 打印扫描器版本(便于 CI 日志追溯),无其他参数(向后兼容)。
    """
    parser = argparse.ArgumentParser(
        description="R59 §5.1 P1: zh-CN.json 与 en-US.json key 对称检查",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="打印扫描器版本后退出",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"check_i18n_key_symmetry v{SCANNER_VERSION}")
        return

    sys.exit(verify())


if __name__ == "__main__":
    main()
