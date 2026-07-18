#!/usr/bin/env python3
"""R63 P1-12: ICU 预编译构建时校验脚本(CI fail-fast 门禁)。

与 ``scripts/check_i18n_key_symmetry.py`` 互补:
    - check_i18n_key_symmetry.py: **静态正则分析**(key 集合 / 参数集 / ICU selector
      集合 / HTML 标签对称),不导入 services/i18n.py,纯文本校验。
    - check_i18n_icu_precompile.py (本脚本): **运行时预编译**,直接调用
      ``services.i18n._validate_icu_message`` / ``_extract_icu_param_set``,
      实际执行 ICU 解析器(``_icu_format``)以验证每条 ICU message 可被编译。

校验内容(R63 P1-12 审计要求):
    1. **ICU 语法预编译**:对每个含 ``{var, plural/select/selectordinal, ...}``
       的 value 调用 ``_validate_icu_message`` 实际解析;任一失败 → exit 1。
       覆盖:括号不平衡 / selector 缺失 / 空 body / 运行时解析异常。
    2. **参数集对称**:对每个公共 key,zh-CN 与 en-US 的参数集合(简单 ``{var}``
       + ICU var 名)必须完全一致;任一不对称 → exit 1。
       与 check_i18n_key_symmetry.py 的 _extract_param_set 等价但调用
       services.i18n._extract_icu_param_set(确保 runtime 与 CI 判定标准统一)。

CI 调用方式:
    python scripts/check_i18n_icu_precompile.py

成功退出 0,失败退出 1。

设计说明:
    - 本脚本必须导入 ``services.i18n`` 以复用 runtime 预编译逻辑(避免 CI 与
      runtime 判定标准漂移)。
    - 不修改 locale 文件,只读校验。
    - 不依赖第三方 ICU 库(与 services/i18n.py 保持一致)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# 扫描器版本(写入 artifact,便于 CI 下游脚本比对)
SCANNER_VERSION = "1.0-r63-p1-12"

# 默认检查的 locale 对(产品仅支持 zh-CN / en-US)
DEFAULT_LOCALES = ("zh-CN", "en-US")


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

    与 scripts/check_i18n_key_symmetry.py._flatten_values 保持一致:
        - 排除 "meta" 顶层 key(允许两个 locale 的 meta 不同)
        - 非 dict 叶子值转为 str(便于后续正则提取)
    """
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        if not prefix and k == "meta":
            continue
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_values(v, full_key))
        else:
            result[full_key] = "" if v is None else str(v)
    return result


def _import_i18n_validators() -> tuple[Any, Any]:
    """延迟导入 services.i18n 的预编译校验函数。

    Returns:
        (_validate_icu_message, _extract_icu_param_set) 两个可调用对象

    Raises:
        ImportError: services.i18n 不可导入或缺少必要函数
    """
    # 确保项目根目录在 sys.path 中(脚本从 CI 或本地均可运行)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # R63 P1-12: 注入 ICU_STRICT_MODE=0 防止 load_locale 在导入阶段
    # 因预编译失败而抛 AppError(本脚本自己调用 _validate_icu_message 做校验,
    # 不依赖 I18nManager.load_locale 的副作用)
    import os
    os.environ.setdefault("ICU_STRICT_MODE", "0")
    from services.i18n import _validate_icu_message, _extract_icu_param_set
    return _validate_icu_message, _extract_icu_param_set


def verify(locales: tuple[str, ...] = DEFAULT_LOCALES) -> int:
    """主校验流程。返回退出码(0=成功,1=失败)。

    校验顺序(fail-fast):
        1. 文件存在性 + JSON 解析
        2. ICU 语法预编译(每个含 ICU pattern 的 value 实际解析)
        3. 参数集对称(每个公共 key 的参数集合一致)
    """
    errors: list[str] = []

    # 0. 导入 runtime 校验函数(确保 CI 与 runtime 判定标准统一)
    try:
        _validate_icu_message, _extract_icu_param_set = _import_i18n_validators()
    except Exception as e:
        print(f"[FAIL] 无法导入 services.i18n 预编译校验函数: {e}")
        return 1

    # 1. 加载所有 locale
    locale_values: dict[str, dict[str, str]] = {}
    for loc in locales:
        path = LOCALES_DIR / f"{loc}.json"
        if not path.exists():
            print(f"[FAIL] {loc}.json 不存在: {path}")
            return 1
        data, err = _load_json(path)
        if err:
            print(f"[FAIL] {loc}.json {err}")
            return 1
        locale_values[loc] = _flatten_values(data)

    # 2. ICU 语法预编译校验(每个 locale 独立检查)
    # R63 P1-12: 任一 ICU message 预编译失败 → 阻断构建
    icu_compile_failures: dict[str, list[tuple[str, str]]] = {
        loc: [] for loc in locales
    }
    for loc, values in locale_values.items():
        for key, value in values.items():
            if not isinstance(value, str) or "{" not in value:
                continue
            ok, reason = _validate_icu_message(value)
            if not ok:
                icu_compile_failures[loc].append((key, reason))

    for loc, failures in icu_compile_failures.items():
        if failures:
            errors.append(
                f"{loc} ICU 预编译失败(共 {len(failures)} 个 key,fail-fast):"
            )
            for key, reason in failures:
                errors.append(f"  - {key}: {reason}")

    # 3. 参数集对称检查(所有 locale 两两比对)
    # R63 P1-12: 每个公共 key 的参数集合必须完全一致
    locale_list = list(locales)
    for i in range(len(locale_list)):
        for j in range(i + 1, len(locale_list)):
            loc_a = locale_list[i]
            loc_b = locale_list[j]
            values_a = locale_values[loc_a]
            values_b = locale_values[loc_b]
            common_keys = sorted(set(values_a.keys()) & set(values_b.keys()))
            param_asymmetries: list[str] = []
            for key in common_keys:
                params_a = _extract_icu_param_set(values_a[key])
                params_b = _extract_icu_param_set(values_b[key])
                if params_a != params_b:
                    param_asymmetries.append(
                        f"{key}: {loc_a} 参数集={sorted(params_a)} "
                        f"vs {loc_b} 参数集={sorted(params_b)}"
                    )
            if param_asymmetries:
                errors.append(
                    f"参数集不对称({loc_a} vs {loc_b},"
                    f"共 {len(param_asymmetries)} 个 key 不一致):"
                )
                for m in param_asymmetries:
                    errors.append(f"  - {m}")

    # 4. 输出结果
    if errors:
        print("[FAIL] R63 P1-12 ICU 预编译构建时校验失败:")
        for e in errors:
            print(f"  - {e}")
        return 1

    # 汇总统计
    total_keys = sum(len(v) for v in locale_values.values())
    icu_count = sum(
        1
        for values in locale_values.values()
        for value in values.values()
        if isinstance(value, str) and "{" in value
    )
    locales_str = "/".join(locales)
    print(
        f"[OK] R63 P1-12 ICU 预编译构建时校验通过 "
        f"(locales: {locales_str}, total keys: {total_keys}, "
        f"含占位符 keys: {icu_count}, ICU 预编译 + 参数集对称全部通过)"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """脚本入口。

    支持 --version 打印扫描器版本(便于 CI 日志追溯)。
    """
    parser = argparse.ArgumentParser(
        description="R63 P1-12: ICU 预编译构建时校验(CI fail-fast 门禁)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"check_i18n_icu_precompile {SCANNER_VERSION}",
    )
    parser.parse_args(argv)
    sys.exit(verify())


if __name__ == "__main__":
    main()
