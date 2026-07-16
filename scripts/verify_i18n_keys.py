#!/usr/bin/env python3
"""R42 P1-8 / R50 P1-4: 校验 zh-CN.json 与 en-US.json 翻译 key 一致性。

校验规则:
1. 两个 locale 文件的翻译 key 集合必须完全一致(无差异);
2. 每个 key 的翻译值必须非空(去除首尾空白后非空字符串);
3. meta 字段不参与比较(允许两个 locale 的 fallback 不同)。
4. R50 P1-4: schema.json 必需字段(required 列表)在两个 locale 中都存在;
5. R50 P1-4: 相同 key 的占位符 {var} 集合在 zh-CN / en-US 中必须一致;
6. R50 P1-4: CLDR 复数规则 — 每个 *.one 必须有对应 *.other,反之亦然;
7. R47 P1-c: 所有 ErrorCodes.message_key 都在两个 locale 文件中存在。

校验失败时输出差异详情,并以 exit 1 退出;成功时 exit 0。

CI 调用方式(基础):
    python scripts/verify_i18n_keys.py

CI 调用方式(R50 P1-4: 结构化 JSON artifact,可上传为 GitHub Actions artifact):
    python scripts/verify_i18n_keys.py --output-json i18n-report.json

JSON artifact 顶层结构:
    {
      "schema_check": {"passed": bool, "errors": [str, ...]},
      "zh_cn_check": {"passed": bool, "missing_keys": [...], "extra_keys": [...],
                      "placeholder_mismatches": [...]},
      "en_us_check": {"passed": bool, "missing_keys": [...], "extra_keys": [...],
                      "placeholder_mismatches": [...]},
      "plural_rules_check": {"passed": bool, "violations": [str, ...]},
      "summary": {"total_keys": int, "zh_cn_coverage": float,
                  "en_us_coverage": float, "timestamp": ISO-8601,
                  "scanner_version": str}
    }
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "locales"

# R50 P1-4: 结构化 artifact 扫描器版本(写入 summary.scanner_version)
SCANNER_VERSION = "1.0"


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


def _extract_placeholders(text: Any) -> set[str]:
    """R50 P1-4: 从字符串中提取 {placeholder} 格式的占位符名称集合。

    "剩余 {count} 次" → {"count"}
    "用户 {user_id} 于 {date} 操作" → {"user_id", "date"}
    "无占位符" → set()

    R56 §5.1: 使用 ASCII-only 标识符模式 [a-zA-Z_][a-zA-Z0-9_]*
    避免将 ICU MessageFormat 复数规则中的文本值(如中文"无文件")
    误识别为占位符(Python 3 的 \\w 默认匹配 Unicode 字母)。
    """
    if not isinstance(text, str):
        return set()
    return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text))


def _check_placeholders(
    zh_values: dict[str, str], en_values: dict[str, str]
) -> tuple[list[str], list[str]]:
    """R50 P1-4: 检查 zh-CN 与 en-US 相同 key 的占位符集合是否一致。

    Args:
        zh_values: zh-CN 扁平化后的 {key: value} 映射
        en_values: en-US 扁平化后的 {key: value} 映射

    Returns:
        (zh_mismatches, en_mismatches) — 双方各自的占位符不一致报告
        (相同 key 下 zh-CN 与 en-US 占位符集合不同时记录)
    """
    zh_mismatches: list[str] = []
    en_mismatches: list[str] = []
    common_keys = set(zh_values.keys()) & set(en_values.keys())
    for key in sorted(common_keys):
        zh_ph = _extract_placeholders(zh_values.get(key, ""))
        en_ph = _extract_placeholders(en_values.get(key, ""))
        if zh_ph != en_ph:
            zh_mismatches.append(
                f"{key}: zh-CN={sorted(zh_ph)} vs en-US={sorted(en_ph)}"
            )
            en_mismatches.append(
                f"{key}: en-US={sorted(en_ph)} vs zh-CN={sorted(zh_ph)}"
            )
    return zh_mismatches, en_mismatches


def _check_plural_rules(
    zh_values: dict[str, str], en_values: dict[str, str]
) -> list[str]:
    """R50 P1-4: 检查 CLDR 风格复数规则 — 每个 *.one 必须有对应 *.other,反之亦然。

    扫描扁平化后的 key 集合,识别以 .one / .other 结尾的复数形式 key,
    若某一侧缺失对应形式则记录违规。

    Args:
        zh_values: zh-CN 扁平化 key 集合
        en_values: en-US 扁平化 key 集合

    Returns:
        违规描述列表(空列表表示通过)
    """
    violations: list[str] = []
    for values, locale_name in [(zh_values, "zh-CN"), (en_values, "en-US")]:
        # .one = 4 chars, .other = 6 chars — 用 [:-4] / [:-6] 去掉后缀得到前缀
        one_prefixes = {
            k[:-4] for k in values
            if isinstance(k, str) and k.endswith(".one")
        }
        other_prefixes = {
            k[:-6] for k in values
            if isinstance(k, str) and k.endswith(".other")
        }
        for prefix in sorted(one_prefixes - other_prefixes):
            violations.append(f"{locale_name}: {prefix}.one 缺少对应的 .other")
        for prefix in sorted(other_prefixes - one_prefixes):
            violations.append(f"{locale_name}: {prefix}.other 缺少对应的 .one")
    return violations


def _empty_artifact(scanner_version: str = SCANNER_VERSION) -> dict:
    """R50 P1-4: 构造空 artifact 结构(用于部分检查无法运行时的早期返回)。

    确保所有顶层字段始终存在(便于 CI 下游脚本消费 JSON)。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema_check": {"passed": True, "errors": []},
        "zh_cn_check": {
            "passed": True,
            "missing_keys": [],
            "extra_keys": [],
            "placeholder_mismatches": [],
        },
        "en_us_check": {
            "passed": True,
            "missing_keys": [],
            "extra_keys": [],
            "placeholder_mismatches": [],
        },
        "plural_rules_check": {"passed": True, "violations": []},
        "summary": {
            "total_keys": 0,
            "zh_cn_coverage": 1.0,
            "en_us_coverage": 1.0,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scanner_version": scanner_version,
        },
    }


def _write_artifact(artifact: dict, output_json: Optional[Any]) -> None:
    """R50 P1-4: 将 artifact 写入 JSON 文件(若 output_json 提供)。

    父目录不存在时自动创建(便于 CI 在全新 workspace 中运行)。
    """
    if output_json is None:
        return
    out_path = Path(output_json)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def verify(
    output_json: Optional[Any] = None,
    scanner_version: str = SCANNER_VERSION,
) -> int:
    """主校验流程。返回退出码(0=成功,1=失败)。

    R50 P1-4 新增:
        - output_json: 若提供(Path/str),将结构化验证结果写入 JSON 文件,
          适合 GitHub Actions 上传为 artifact(向后兼容:None 时仅 stdout 输出)
        - scanner_version: 写入 summary.scanner_version,便于下游 CI 比对

    Args:
        output_json: JSON artifact 输出路径;None 时不写文件(向后兼容)
        scanner_version: 扫描器版本字符串

    Returns:
        0=校验通过;1=校验失败
    """
    zh_path = LOCALES_DIR / "zh-CN.json"
    en_path = LOCALES_DIR / "en-US.json"
    schema_path = LOCALES_DIR / "schema.json"
    artifact = _empty_artifact(scanner_version)

    # 1. 文件存在性检查
    file_errors: list[str] = []
    if not zh_path.exists():
        file_errors.append(f"zh-CN.json 不存在: {zh_path}")
    if not en_path.exists():
        file_errors.append(f"en-US.json 不存在: {en_path}")
    if file_errors:
        # 文件缺失 → schema_check 失败(无法继续后续校验)
        artifact["schema_check"]["passed"] = False
        artifact["schema_check"]["errors"] = file_errors
        _write_artifact(artifact, output_json)
        print("[FAIL] i18n 文件缺失:")
        for e in file_errors:
            print(f"  - {e}")
        return 1

    # 2. JSON 解析
    zh_data, zh_err = _load_json(zh_path)
    en_data, en_err = _load_json(en_path)
    parse_errors: list[str] = []
    if zh_err:
        parse_errors.append(f"zh-CN.json {zh_err}")
    if en_err:
        parse_errors.append(f"en-US.json {en_err}")
    if parse_errors:
        # JSON 解析失败 → schema_check 失败(无法继续后续校验)
        artifact["schema_check"]["passed"] = False
        artifact["schema_check"]["errors"] = parse_errors
        _write_artifact(artifact, output_json)
        print("[FAIL] i18n JSON 解析失败:")
        for e in parse_errors:
            print(f"  - {e}")
        return 1

    # 3. 排除 meta 键(允许两个 locale 的 meta 不同)
    zh_body = {k: v for k, v in zh_data.items() if k != "meta"}
    en_body = {k: v for k, v in en_data.items() if k != "meta"}

    # 4. 扁平化 key 集合与值映射
    zh_keys = _flatten_keys(zh_body)
    en_keys = _flatten_keys(en_body)
    zh_values = _flatten_values(zh_body)
    en_values = _flatten_values(en_body)

    errors: list[str] = []

    # 5. R50 P1-4: Schema 必需字段校验
    #    若 schema.json 不存在则跳过(向后兼容:旧测试 fixture 无 schema.json)
    schema_errors: list[str] = []
    if schema_path.exists():
        schema_data, schema_err = _load_json(schema_path)
        if schema_err:
            schema_errors.append(f"schema.json {schema_err}")
        elif isinstance(schema_data, dict):
            required = schema_data.get("required", [])
            if isinstance(required, list):
                for req_key in required:
                    if req_key not in zh_data:
                        schema_errors.append(
                            f"zh-CN.json 缺少 schema 必需字段: {req_key}"
                        )
                    if req_key not in en_data:
                        schema_errors.append(
                            f"en-US.json 缺少 schema 必需字段: {req_key}"
                        )
    if schema_errors:
        artifact["schema_check"]["passed"] = False
        artifact["schema_check"]["errors"] = schema_errors
        errors.extend(schema_errors)

    # 6. 检查 key 集合差异(对应 zh_cn_check / en_us_check)
    zh_only = sorted(zh_keys - en_keys)  # zh-CN 独有 → en-US 缺失 / zh-CN 额外
    en_only = sorted(en_keys - zh_keys)  # en-US 独有 → zh-CN 缺失 / en-US 额外
    if zh_only:
        errors.append(
            f"zh-CN 独有 key(在 en-US 中缺失,共 {len(zh_only)} 个): "
            f"{zh_only}"
        )
    if en_only:
        errors.append(
            f"en-US 独有 key(在 zh-CN 中缺失,共 {len(en_only)} 个): "
            f"{en_only}"
        )
    # zh-CN 检查:缺失 = en_only(en-US 有而 zh-CN 无),额外 = zh_only(zh-CN 有而 en-US 无)
    artifact["zh_cn_check"]["missing_keys"] = en_only
    artifact["zh_cn_check"]["extra_keys"] = zh_only
    # en-US 检查:缺失 = zh_only(zh-CN 有而 en-US 无),额外 = en_only(en-US 有而 zh-CN 无)
    artifact["en_us_check"]["missing_keys"] = zh_only
    artifact["en_us_check"]["extra_keys"] = en_only

    # 7. 检查空翻译
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

    # 8. R47 P1-c: 校验所有 ErrorCodes 的 message_key 都在 locale 文件中
    error_code_errors = _verify_error_code_message_keys(zh_keys, en_keys)
    errors.extend(error_code_errors)

    # 9. R50 P1-4: 占位符一致性检查
    zh_ph_mismatches, en_ph_mismatches = _check_placeholders(zh_values, en_values)
    artifact["zh_cn_check"]["placeholder_mismatches"] = zh_ph_mismatches
    artifact["en_us_check"]["placeholder_mismatches"] = en_ph_mismatches
    if zh_ph_mismatches:
        for m in zh_ph_mismatches:
            errors.append(f"占位符不一致(zh-CN vs en-US): {m}")

    # 10. R50 P1-4: 复数规则检查
    plural_violations = _check_plural_rules(zh_values, en_values)
    artifact["plural_rules_check"]["violations"] = plural_violations
    if plural_violations:
        artifact["plural_rules_check"]["passed"] = False
        for v in plural_violations:
            errors.append(f"复数规则违反: {v}")

    # 11. 设置 zh_cn_check / en_us_check 的 passed 标志
    #     (综合考虑 missing/extra/placeholder/empty)
    artifact["zh_cn_check"]["passed"] = (
        not artifact["zh_cn_check"]["missing_keys"]
        and not artifact["zh_cn_check"]["extra_keys"]
        and not artifact["zh_cn_check"]["placeholder_mismatches"]
        and not zh_empty
    )
    artifact["en_us_check"]["passed"] = (
        not artifact["en_us_check"]["missing_keys"]
        and not artifact["en_us_check"]["extra_keys"]
        and not artifact["en_us_check"]["placeholder_mismatches"]
        and not en_empty
    )

    # 12. R50 P1-4: 填充 summary
    total_keys = len(zh_keys | en_keys)
    artifact["summary"]["total_keys"] = total_keys
    artifact["summary"]["zh_cn_coverage"] = (
        round(len(zh_keys) / total_keys, 4) if total_keys else 1.0
    )
    artifact["summary"]["en_us_coverage"] = (
        round(len(en_keys) / total_keys, 4) if total_keys else 1.0
    )

    # 13. 写入 JSON artifact(若请求) — 即使后续 stdout 报错也保证 artifact 落盘
    _write_artifact(artifact, output_json)

    # 14. stdout 输出(保持向后兼容)
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


def main(argv: Optional[list[str]] = None) -> None:
    """脚本入口。

    R50 P1-4: 支持 --output-json 参数,将结构化验证结果写入 JSON 文件,
    适合 GitHub Actions 上传为 artifact。

    Args:
        argv: 命令行参数列表;None 时读取 sys.argv[1:](argparse 默认行为)
    """
    parser = argparse.ArgumentParser(
        description="校验 zh-CN.json 与 en-US.json 翻译 key 一致性",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="将验证结果输出为 JSON 文件(GitHub Actions 可上传为 artifact)",
    )
    args = parser.parse_args(argv)
    sys.exit(verify(output_json=args.output_json))


if __name__ == "__main__":
    main()
