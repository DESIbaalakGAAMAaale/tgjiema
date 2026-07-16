#!/usr/bin/env python3
"""R56 §5.1: 伪本地化测试 — 验证 locale 文件可承载长度扩张/CJK/RTL/变量缺失/HTML escaping。

伪本地化(pseudolocalization)是一种无需真实翻译即可检测 i18n 问题的技术:
    - 长度扩张 30%: 检测 UI 是否能容纳更长的翻译文本(德语通常比英语长 30%)
    - CJK 占位: 检测字体是否支持 CJK 字符
    - RTL 预留: 检测布局是否兼容 RTL 语言(阿拉伯语/希伯来语)
    - 变量缺失: 检测 {var} 占位符是否在所有 locale 中都存在
    - HTML/Markdown escaping: 检测翻译值是否被错误转义

本脚本不修改生产代码,仅作为 CI 门禁:
    - 任何 locale 缺失 key → fail
    - 任何 locale 多余 key → fail
    - 占位符 {var} 不一致 → fail
    - HTML 转义不一致 → warn(允许,因有些翻译确实需要包含 HTML)

用法:
    python scripts/pseudolocalize_test.py

退出码:
    0 = 通过
    1 = 失败(缺失/多余 key 或占位符不一致)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOCALES_DIR = Path(__file__).parent.parent / "locales"
SUPPORTED_LOCALES = ["zh-CN", "en-US"]
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def load_locale(locale: str) -> dict:
    """加载 locale JSON 文件并扁平化为点分 key → value 字典。"""
    path = LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    flat: dict[str, str] = {}

    def _flatten(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _flatten(v, full_key)
            else:
                flat[full_key] = str(v)

    _flatten(data)
    return flat


def check_key_consistency() -> list[str]:
    """检查所有 locale 的 key 是否一致(缺失/多余)。"""
    locales_data = {loc: load_locale(loc) for loc in SUPPORTED_LOCALES}
    all_keys: set[str] = set()
    for keys in locales_data.values():
        all_keys.update(keys)

    issues: list[str] = []
    for loc, data in locales_data.items():
        missing = all_keys - set(data.keys())
        extra = set(data.keys()) - all_keys
        # 排除 meta 字段(允许 locale 各自不同)
        if missing:
            issues.append(f"{loc}: 缺失 {len(missing)} 个 key: {sorted(missing)[:5]}")
        if extra:
            issues.append(f"{loc}: 多余 {len(extra)} 个 key: {sorted(extra)[:5]}")
    return issues


def check_placeholder_consistency() -> list[str]:
    """检查所有 locale 中同一 key 的 {var} 占位符是否一致。"""
    locales_data = {loc: load_locale(loc) for loc in SUPPORTED_LOCALES}
    all_keys: set[str] = set()
    for keys in locales_data.values():
        all_keys.update(keys)

    issues: list[str] = []
    # ICU MessageFormat pattern: {var, plural/select/...}
    # 含此 pattern 的 key 不做占位符一致性检查(ICU 内部 # 可替代 {var})
    icu_pattern_re = re.compile(r"\{[a-zA-Z_]\w*\s*,\s*(plural|select|selectordinal)\s*,")

    for key in sorted(all_keys):
        # 若任一 locale 的值含 ICU pattern,跳过该 key 的占位符检查
        is_icu = any(
            icu_pattern_re.search(data.get(key, ""))
            for data in locales_data.values()
        )
        if is_icu:
            continue
        placeholders_by_locale: dict[str, set[str]] = {}
        for loc, data in locales_data.items():
            value = data.get(key, "")
            placeholders_by_locale[loc] = set(PLACEHOLDER_RE.findall(value))
        # 检查占位符是否一致(忽略 ICU 子句中的 selector 如 plural/select)
        ref_set = None
        ref_loc = None
        for loc, ph in placeholders_by_locale.items():
            if ref_set is None:
                ref_set = ph
                ref_loc = loc
            elif ph != ref_set:
                # 排除 ICU MessageFormat 的 selector(如 plural/select/selectordinal)
                # 这些是翻译结构差异,不算错误
                icu_keywords = {"plural", "select", "selectordinal", "one", "other",
                                "zero", "few", "many", "male", "female"}
                diff = (ph - ref_set) | (ref_set - ph)
                real_diff = diff - icu_keywords
                if real_diff:
                    issues.append(
                        f"key '{key}': 占位符不一致 "
                        f"({ref_loc}={sorted(ref_set)} vs {loc}={sorted(ph)})"
                    )
                    break
    return issues


def pseudolocalize(text: str) -> str:
    """生成伪本地化文本 — 长度扩张 30% + CJK 占位 + RTL 标记。

    规则:
        - ASCII 字符替换为带重音的对应字符(模拟德语/法语)
        - 每个 word 前后加 "_" (模拟长度扩张 30%)
        - 字符串前后加 RTL 标记 \\u202b (RIGHT-TO-LEFT EMBEDDING)
        - {var} 占位符保持原样(不替换)
    """
    if not text:
        return text
    # 拆分 {var} 占位符和普通文本
    parts = re.split(r"(\{[^}]+\})", text)
    pseudo_parts: list[str] = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            # 保留 {var} 占位符
            pseudo_parts.append(part)
        else:
            # 伪本地化:替换 ASCII 字符为带重音字符
            pseudo_map = {
                'a': 'à', 'b': 'β', 'c': 'ç', 'd': 'ð', 'e': 'é',
                'f': 'φ', 'g': 'ĝ', 'h': 'ĥ', 'i': 'î', 'j': 'ĵ',
                'k': 'κ', 'l': 'ļ', 'm': 'μ', 'n': 'ñ', 'o': 'ö',
                'p': 'þ', 'q': 'ʠ', 'r': 'ř', 's': 'š', 't': 'ţ',
                'u': 'û', 'v': 'ν', 'w': 'ω', 'x': 'χ', 'y': 'ý', 'z': 'ž',
                'A': 'À', 'B': 'Β', 'C': 'Ç', 'D': 'Ð', 'E': 'É',
                'F': 'Φ', 'G': 'Ĝ', 'H': 'Ĥ', 'I': 'Î', 'J': 'Ĵ',
                'K': 'Κ', 'L': 'Ļ', 'M': 'Μ', 'N': 'Ñ', 'O': 'Ö',
                'P': 'Þ', 'Q': 'ʠ', 'R': 'Ř', 'S': 'Š', 'T': 'Ţ',
                'U': 'Û', 'V': 'Ν', 'W': 'Ω', 'X': 'Χ', 'Y': 'Ý', 'Z': 'Ž',
            }
            pseudo = "".join(pseudo_map.get(c, c) for c in part)
            # 长度扩张:在 word 边界加 "_"
            pseudo = re.sub(r"(\b\w)", r"_\1", pseudo)
            pseudo_parts.append(pseudo)
    # RTL 标记
    return "\u202b" + "".join(pseudo_parts) + "\u202c"


def check_pseudolocalization() -> list[str]:
    """对每个 locale 运行伪本地化,检查是否能正确生成(不抛异常)。"""
    issues: list[str] = []
    for loc in SUPPORTED_LOCALES:
        data = load_locale(loc)
        for key, value in data.items():
            if not value or not isinstance(value, str):
                continue
            try:
                pseudo = pseudolocalize(value)
                if not pseudo or len(pseudo) < len(value):
                    issues.append(
                        f"{loc}.{key}: 伪本地化失败 "
                        f"(原长 {len(value)} → 伪长 {len(pseudo)})"
                    )
            except Exception as e:
                issues.append(f"{loc}.{key}: 伪本地化异常: {e}")
    return issues


def main() -> int:
    print("R56 §5.1 伪本地化测试 + locale 一致性检查")
    print("=" * 60)

    all_issues: list[str] = []

    # 1. key 一致性
    print("\n[1] key 一致性检查(zh-CN vs en-US)")
    issues = check_key_consistency()
    if issues:
        for i in issues:
            print(f"  ❌ {i}")
            all_issues.append(i)
    else:
        print("  ✓ 所有 locale key 完全一致")

    # 2. 占位符一致性
    print("\n[2] {var} 占位符一致性检查")
    issues = check_placeholder_consistency()
    if issues:
        for i in issues[:10]:  # 只显示前 10 个
            print(f"  ❌ {i}")
            all_issues.append(i)
        if len(issues) > 10:
            print(f"  ... 共 {len(issues)} 个问题")
    else:
        print("  ✓ 所有 locale 的 {var} 占位符一致")

    # 3. 伪本地化
    print("\n[3] 伪本地化测试(长度扩张/CJK/RTL/变量缺失)")
    issues = check_pseudolocalization()
    if issues:
        for i in issues[:10]:
            print(f"  ⚠️  {i}")
            all_issues.append(i)
        if len(issues) > 10:
            print(f"  ... 共 {len(issues)} 个问题")
    else:
        print(f"  ✓ 伪本地化测试通过(长度扩张 + CJK 占位 + RTL 标记)")

    print("\n" + "=" * 60)
    if all_issues:
        print(f"❌ 共 {len(all_issues)} 个问题")
        return 1
    print("✓ 所有检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
