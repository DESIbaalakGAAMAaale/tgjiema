#!/usr/bin/env python3
"""R42 P1-8: 校验 zh-CN.json 与 en-US.json 翻译 key 一致性。

校验规则:
1. 两个 locale 文件的翻译 key 集合必须完全一致(无差异);
2. 每个 key 的翻译值必须非空(去除首尾空白后非空字符串);
3. meta 字段不参与比较(允许两个 locale 的 fallback 不同)。

校验失败时输出差异详情,并以 exit 1 退出;成功时 exit 0。

CI 调用方式:
    python scripts/verify_i18n_keys.py

本地调用方式:
    python scripts/verify_i18n_keys.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"


def _flatten_keys(obj: Any, prefix: str = "") -> set[str]:
    """递归扁平化 dict,返回所有叶子节点的点分 key 集合。

    {"errors": {"quota.decode.exceeded": "x"}} -> {"errors.quota.decode.exceeded"}
    {"admin": {"errors": {"unauthorized": "x"}}} -> {"admin.errors.unauthorized"}

    非 dict / 非 str 的叶子值也会被收集(用其点分路径)。
    """
    keys: set[str] = set()
    if not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full_key)
        else:
            keys.add(full_key)
    return keys


def _flatten_values(obj: Any, prefix: str = "") -> dict[str, str]:
    """递归扁平化 dict,返回 {key: value} 映射(用于空值检查)。"""
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_values(v, full_key))
        else:
            result[full_key] = v
    return result


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


def verify() -> int:
    """主校验流程。返回退出码(0=成功,1=失败)。"""
    zh_path = LOCALES_DIR / "zh-CN.json"
    en_path = LOCALES_DIR / "en-US.json"

    # 1. 文件存在性检查
    if not zh_path.exists():
        print(f"[FAIL] zh-CN.json 不存在: {zh_path}")
        return 1
    if not en_path.exists():
        print(f"[FAIL] en-US.json 不存在: {en_path}")
        return 1

    # 2. JSON 解析
    zh_data, zh_err = _load_json(zh_path)
    if zh_err:
        print(f"[FAIL] zh-CN.json {zh_err}")
        return 1
    en_data, en_err = _load_json(en_path)
    if en_err:
        print(f"[FAIL] en-US.json {en_err}")
        return 1

    # 3. 排除 meta 键(允许两个 locale 的 meta 不同)
    zh_body = {k: v for k, v in zh_data.items() if k != "meta"}
    en_body = {k: v for k, v in en_data.items() if k != "meta"}

    # 4. 扁平化 key 集合
    zh_keys = _flatten_keys(zh_body)
    en_keys = _flatten_keys(en_body)

    errors: list[str] = []

    # 5. 检查 key 集合差异
    zh_only = zh_keys - en_keys
    en_only = en_keys - zh_keys
    if zh_only:
        errors.append(
            f"zh-CN 独有 key(在 en-US 中缺失,共 {len(zh_only)} 个): "
            f"{sorted(zh_only)}"
        )
    if en_only:
        errors.append(
            f"en-US 独有 key(在 zh-CN 中缺失,共 {len(en_only)} 个): "
            f"{sorted(en_only)}"
        )

    # 6. 检查空翻译
    zh_values = _flatten_values(zh_body)
    en_values = _flatten_values(en_body)
    zh_empty = sorted(
        k for k, v in zh_values.items()
        if v is None or (isinstance(v, str) and not v.strip())
    )
    en_empty = sorted(
        k for k, v in en_values.items()
        if v is None or (isinstance(v, str) and not v.strip())
    )
    if zh_empty:
        errors.append(
            f"zh-CN 空翻译 key(共 {len(zh_empty)} 个): {zh_empty}"
        )
    if en_empty:
        errors.append(
            f"en-US 空翻译 key(共 {len(en_empty)} 个): {en_empty}"
        )

    # 7. R47 P1-c: 校验所有 ErrorCodes 的 message_key 都在 locale 文件中
    error_code_errors = _verify_error_code_message_keys(zh_keys, en_keys)
    errors.extend(error_code_errors)

    # 8. 输出结果
    if errors:
        print("[FAIL] i18n key 校验失败:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(
        f"[OK] i18n key 校验通过 "
        f"(zh-CN: {len(zh_keys)} keys, en-US: {len(en_keys)} keys, "
        f"两文件 key 集合完全一致且无空翻译)"
    )
    return 0


def _verify_error_code_message_keys(zh_keys: set[str], en_keys: set[str]) -> list[str]:
    """R47 P1-c: 校验所有 ErrorCodes.message_key 都在 zh-CN 和 en-US locale 文件中。

    从 services.error_codes 模块导入 ErrorRegistry,获取所有已注册的 message_key,
    检查每个 key 是否同时存在于 zh_keys 和 en_keys 集合中。

    Args:
        zh_keys: zh-CN.json 扁平化后的 key 集合
        en_keys: en-US.json 扁平化后的 key 集合

    Returns:
        错误消息列表(空列表表示通过)
    """
    errors: list[str] = []
    try:
        # 添加项目根到 sys.path(确保能 import services.error_codes)
        import sys
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from services.error_codes import ErrorRegistry
    except Exception as e:
        errors.append(
            f"R47 P1-c: 无法导入 services.error_codes.ErrorRegistry: {e}"
        )
        return errors

    try:
        all_message_keys = ErrorRegistry.all_message_keys()
    except Exception as e:
        errors.append(
            f"R47 P1-c: ErrorRegistry.all_message_keys() 调用失败: {e}"
        )
        return errors

    if not all_message_keys:
        errors.append(
            "R47 P1-c: ErrorRegistry 中未注册任何 message_key"
            "(应至少包含 ErrorCodes.ERROR_INTERNAL 的 message_key)"
        )
        return errors

    zh_missing = sorted(k for k in all_message_keys if k not in zh_keys)
    en_missing = sorted(k for k in all_message_keys if k not in en_keys)
    if zh_missing:
        errors.append(
            f"R47 P1-c: zh-CN.json 缺失 ErrorCodes message_key "
            f"(共 {len(zh_missing)} 个): {zh_missing}"
        )
    if en_missing:
        errors.append(
            f"R47 P1-c: en-US.json 缺失 ErrorCodes message_key "
            f"(共 {len(en_missing)} 个): {en_missing}"
        )

    return errors


def main() -> None:
    """脚本入口。"""
    sys.exit(verify())


if __name__ == "__main__":
    main()
