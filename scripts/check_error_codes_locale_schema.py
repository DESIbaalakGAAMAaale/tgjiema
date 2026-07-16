#!/usr/bin/env python3
"""R56 §5.2: locale schema 验证 — 所有 ErrorRegistry.message_key 必须在 locale 文件中存在。

检查项:
1. ErrorRegistry 中所有 message_key 必须在 locales/zh-CN.json 中存在(点分路径)
2. ErrorRegistry 中所有 message_key 必须在 locales/en-US.json 中存在(点分路径)
3. zh-CN.json 和 en-US.json 的 key 必须完全一致(无缺失/多余)
4. {var} 占位符在两个 locale 中必须一致

CI 门禁:
    - 任何 message_key 缺失 → exit 1
    - locale key 不一致 → exit 1
    - 占位符不一致 → exit 1
    - 全部通过 → exit 0

用法:
    python scripts/check_error_codes_locale_schema.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"
SUPPORTED_LOCALES = ["zh-CN", "en-US"]
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def load_locale_flat(locale: str) -> dict[str, str]:
    """加载 locale JSON 并扁平化为点分 key → value 字典。"""
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


def collect_registry_message_keys() -> set[str]:
    """从 ErrorRegistry 收集所有已注册的 message_key。"""
    sys.path.insert(0, str(REPO_ROOT))
    # 触发 ErrorRegistry 初始化(_register_defaults 会在首次访问时执行)
    from services.error_codes import ErrorRegistry  # type: ignore

    # 触发初始化
    ErrorRegistry.all_codes()
    return set(ErrorRegistry.all_message_keys())


def check_message_keys_in_locales(
    registry_keys: set[str],
    locale_data: dict[str, str],
    locale_name: str,
) -> list[str]:
    """检查所有 message_key 是否在指定 locale 中存在。"""
    issues: list[str] = []
    for key in sorted(registry_keys):
        if key not in locale_data:
            issues.append(f"{locale_name}: message_key '{key}' 缺失")
    return issues


def check_locale_key_consistency() -> list[str]:
    """检查 zh-CN 和 en-US 的 key 是否完全一致。"""
    locales_data = {loc: load_locale_flat(loc) for loc in SUPPORTED_LOCALES}
    all_keys: set[str] = set()
    for keys in locales_data.values():
        all_keys.update(keys)
    issues: list[str] = []
    for loc, data in locales_data.items():
        missing = all_keys - set(data.keys())
        extra = set(data.keys()) - all_keys
        if missing:
            issues.append(
                f"{loc}: 缺失 {len(missing)} 个 key: {sorted(missing)[:5]}"
            )
        if extra:
            issues.append(
                f"{loc}: 多余 {len(extra)} 个 key: {sorted(extra)[:5]}"
            )
    return issues


def check_placeholder_consistency() -> list[str]:
    """检查所有 locale 中同一 key 的 {var} 占位符是否一致。"""
    locales_data = {loc: load_locale_flat(loc) for loc in SUPPORTED_LOCALES}
    all_keys: set[str] = set()
    for keys in locales_data.values():
        all_keys.update(keys)
    issues: list[str] = []
    icu_pattern_re = re.compile(
        r"\{[a-zA-Z_]\w*\s*,\s*(plural|select|selectordinal)\s*,"
    )
    for key in sorted(all_keys):
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
        ref_set = None
        ref_loc = None
        for loc, ph in placeholders_by_locale.items():
            if ref_set is None:
                ref_set = ph
                ref_loc = loc
            elif ph != ref_set:
                issues.append(
                    f"key '{key}': 占位符不一致 "
                    f"({ref_loc}={sorted(ref_set)} vs {loc}={sorted(ph)})"
                )
                break
    return issues


def main() -> int:
    print("R56 §5.2: locale schema 验证")
    print("=" * 60)

    all_issues: list[str] = []

    # 1. 从 ErrorRegistry 收集 message_keys
    print("\n[1] 收集 ErrorRegistry 中的 message_keys")
    try:
        registry_keys = collect_registry_message_keys()
        print(f"  ✓ ErrorRegistry 已注册 {len(registry_keys)} 个 message_key")
    except Exception as e:
        print(f"  ❌ 无法加载 ErrorRegistry: {e}")
        return 1

    # 2. 检查所有 message_key 在两个 locale 中都存在
    print("\n[2] 检查 message_key 在 zh-CN.json 和 en-US.json 中存在")
    for loc in SUPPORTED_LOCALES:
        locale_data = load_locale_flat(loc)
        missing = check_message_keys_in_locales(registry_keys, locale_data, loc)
        if missing:
            for m in missing[:10]:
                print(f"  ❌ {m}")
                all_issues.append(m)
            if len(missing) > 10:
                print(f"  ... 共 {len(missing)} 个缺失")
        else:
            print(f"  ✓ {loc}: 所有 {len(registry_keys)} 个 message_key 都存在")

    # 3. locale key 一致性
    print("\n[3] locale key 一致性检查(zh-CN vs en-US)")
    issues = check_locale_key_consistency()
    if issues:
        for i in issues:
            print(f"  ❌ {i}")
            all_issues.append(i)
    else:
        print("  ✓ 所有 locale key 完全一致")

    # 4. 占位符一致性
    print("\n[4] {var} 占位符一致性检查")
    issues = check_placeholder_consistency()
    if issues:
        for i in issues[:10]:
            print(f"  ❌ {i}")
            all_issues.append(i)
        if len(issues) > 10:
            print(f"  ... 共 {len(issues)} 个问题")
    else:
        print("  ✓ 所有 locale 的 {var} 占位符一致")

    print("\n" + "=" * 60)
    if all_issues:
        print(f"❌ R56 §5.2 locale schema 验证失败: 共 {len(all_issues)} 个问题")
        return 1
    print("✓ R56 §5.2 locale schema 验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
