#!/usr/bin/env python3
"""R47 P1-d: 模块化 i18n 硬编码字符串扫描与逐模块下降门禁。

历史:
    R44 6.3: 引入硬编码用户面向中文字符串扫描,baseline 机制(单一数字)。
    R46 P1: 新增 --ratchet 模式(违规数只减不增)。
    R47 P1-d: 改为**模块化 baseline**,逐模块下降门禁,清零目标 0。
        - baseline 文件迁移至 locales/baseline.json,按模块分别统计。
        - --check: 任何模块超过各自 baseline 或 total 超过 baseline.total → 失败。
        - --ratchet: 自动更新 baseline.json(模块可升降,total 只允许非增加)。
        - --report: 输出每模块进度、距清零目标差距、top 10 文件。

模块划分(贴合真实目录):
    - bots/up_bot.py            (Up)
    - bots/idx_bot.py           (Idx)
    - bots/dsp_bot.py           (Dsp)
    - bots/mon_bot.py           (Mon)
    - bots/admin_bot/           (Admin Bot)
    - admin/                    (admin 后端 Python: admin/*.py)
    - admin/templates/          (Admin Web HTML 模板)
    - admin/static/             (Admin Web 静态资源;当前目录不存在,占位清零门禁)
    - services/                 (服务层;原 R44 未扫描,本次纳入)

说明:
    原 R44 baseline 为 954(仅覆盖 bots/+admin)。本次纳入 services/ 扫描后,
    真实基线 total 为各模块实际计数之和(services/ 历史遗漏债务被显式纳入)。
    total 门禁以 baseline.json 的 total 字段为准(只允许非增加);
    _original_r44_baseline 字段保留 954 作为历史参考。

用法:
    # 模块化检查(CI 默认;任何模块/total 超标 → 退出码 1)
    python scripts/scan_hardcoded_strings.py --check
    python scripts/scan_hardcoded_strings.py          # 等价于 --check

    # 下降 baseline(修复后更新,只降不升)
    python scripts/scan_hardcoded_strings.py --ratchet

    # 生成/重建 baseline.json(初始基线)
    python scripts/scan_hardcoded_strings.py --generate-baseline

    # 查看进度报告
    python scripts/scan_hardcoded_strings.py --report
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 中文 Unicode 范围
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# 用户面向字符串模式(reply_text/answer/HTTPException/detail/f-string 等)
USER_FACING_PATTERNS = [
    # Python: reply_text("中文") / reply_text(f"中文") / answer("中文") 等
    re.compile(r'(?:reply_text|answer|reply_photo|reply_document|send_message)\s*\(\s*f?["\']([^"\']*)["\']'),
    re.compile(r'detail\s*=\s*f?["\']([^"\']*)["\']'),
    re.compile(r'HTTPException\s*\([^)]*detail\s*=\s*f?["\']([^"\']*)["\']'),
    # f-string 中的中文(仅匹配 f"..." 包含中文的)
    re.compile(r'f["\']([^"\']*[\u4e00-\u9fff][^"\']*)["\']'),
]

# 白名单目录/文件
SKIP_PATTERNS = [
    'tests/',
    'docs/',
    'scripts/',
    '__pycache__',
    '.git/',
    'node_modules/',
]

# 模块基线文件(R47 P1-d)
MODULE_BASELINE_FILE = Path(__file__).parent.parent / 'locales' / 'baseline.json'

# 原 R44 基线(历史参考:bots/+admin 范围)
ORIGINAL_R44_BASELINE = 954

# 模块定义(顺序即报告输出顺序);清零目标均为 0
MODULE_KEYS = [
    'bots/up_bot.py',
    'bots/idx_bot.py',
    'bots/dsp_bot.py',
    'bots/mon_bot.py',
    'bots/admin_bot/',
    'admin/',
    'admin/templates/',
    'admin/static/',
    'services/',
]


def is_skipped(path: Path) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern in str(path):
            return True
    return False


def _strip_string_literals(line: str) -> str:
    """移除字符串字面量,以便准确计算括号深度。

    避免字符串中的括号影响多行调用跟踪。
    """
    return re.sub(r'f?["\'][^"\']*["\']', '""', line)


def scan_python_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 Python 文件,返回 (line_no, pattern_type, content) 列表。

    改进:正确跳过多行 logger./logging./print() 调用中的 f-string。
    """
    findings = []
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return findings

    lines = content.splitlines()
    # 多行 logger 调用跟踪
    in_logger_call = False
    paren_depth = 0

    for idx, line in enumerate(lines, 1):
        stripped = line.lstrip()
        # 跳过注释行
        if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
            continue

        # 用于括号计数的行(移除字符串字面量)
        code_only = _strip_string_literals(line)

        if not in_logger_call:
            # 检查是否是 logger 调用行
            if 'logger.' in line or 'logging.' in line or 'print(' in line:
                opens = code_only.count('(')
                closes = code_only.count(')')
                if opens > closes:
                    # 多行 logger 调用开始
                    in_logger_call = True
                    paren_depth = opens - closes
                # 无论单行还是多行,跳过此行
                continue
        else:
            # 在多行 logger 调用内部
            paren_depth += code_only.count('(') - code_only.count(')')
            if paren_depth <= 0:
                in_logger_call = False
            continue

        # 正常模式匹配
        for pattern in USER_FACING_PATTERNS:
            for match in pattern.finditer(line):
                matched_text = match.group(1)
                if CJK_PATTERN.search(matched_text):
                    findings.append((idx, pattern.pattern[:50], matched_text[:80]))
    return findings


def scan_html_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描 HTML 文件中的硬编码中文(不在 {{ }} 内的)。"""
    findings = []
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return findings

    for i, line in enumerate(content.splitlines(), 1):
        # 跳过注释
        if '<!--' in line and '-->' in line:
            continue
        # 跳过 {{ }} 内的内容(Jinja2 模板变量)
        cleaned = re.sub(r'\{\{[^}]*\}\}', '', line)
        # 检查 >中文< 模式(标签之间的文本)
        for match in re.finditer(r'>([^<>]*[\u4e00-\u9fff][^<>]*)<', cleaned):
            text = match.group(1).strip()
            if text and CJK_PATTERN.search(text):
                findings.append((i, 'html_text', text[:80]))
    return findings


def _violation_key(file: str, content: str) -> str:
    """生成违规唯一键(基于文件路径和内容,不依赖行号)。

    路径分隔符归一化为 /,确保 Windows 生成的 baseline 在 Linux CI 中可用。
    """
    return f"{file.replace(chr(92), '/')}::{content}"


def _module_for_file(rel: str) -> str | None:
    """将相对文件路径(使用 / 分隔)映射到所属模块键。

    返回 None 表示未归属任何模块(应在扫描范围之外或无违规)。
    admin/(整个目录)按子目录细分为 admin/ + admin/templates/ + admin/static/,
    以便 Admin Web HTML 模板独立门禁、逐模块清零。
    """
    if rel == 'bots/up_bot.py':
        return 'bots/up_bot.py'
    if rel == 'bots/idx_bot.py':
        return 'bots/idx_bot.py'
    if rel == 'bots/dsp_bot.py':
        return 'bots/dsp_bot.py'
    if rel == 'bots/mon_bot.py':
        return 'bots/mon_bot.py'
    if rel.startswith('bots/admin_bot/'):
        return 'bots/admin_bot/'
    if rel.startswith('admin/templates/'):
        return 'admin/templates/'
    if rel.startswith('admin/static/'):
        return 'admin/static/'
    if rel.startswith('admin/'):
        return 'admin/'
    if rel.startswith('services/'):
        return 'services/'
    return None


def collect_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """收集所有违规,返回 (file, line_no, ptype, content) 列表(file 使用 / 分隔)。

    R47 P1-d: 扫描范围扩展至 services/**/*.py(原 R44 未覆盖)。
    """
    findings = []

    # 扫描 Python 文件
    for pattern in ['bots/**/*.py', 'admin/**/*.py', 'services/**/*.py']:
        for path in root.glob(pattern):
            if is_skipped(path):
                continue
            rel = str(path.relative_to(root)).replace(chr(92), '/')
            for line_no, ptype, content in scan_python_file(path):
                findings.append((rel, line_no, ptype, content))

    # 扫描 HTML 文件
    for path in root.glob('admin/templates/**/*.html'):
        if is_skipped(path):
            continue
        rel = str(path.relative_to(root)).replace(chr(92), '/')
        for line_no, ptype, content in scan_html_file(path):
            findings.append((rel, line_no, ptype, content))

    return findings


def count_by_module(findings) -> dict[str, int]:
    """按模块统计去重后的违规数(以 file::content 为去重键)。

    由于去重键包含文件路径,每个键唯一归属一个模块,
    故各模块计数之和等于全局去重总数。
    """
    sets: dict[str, set[str]] = {m: set() for m in MODULE_KEYS}
    for file, _line, _ptype, content in findings:
        m = _module_for_file(file)
        if m is None:
            continue
        sets[m].add(_violation_key(file, content))
    return {m: len(s) for m, s in sets.items()}


def file_counts(findings) -> list[tuple[str, int]]:
    """返回按违规数降序的 (file, 去重违规数) 列表,用于 top 10 报告。"""
    sets: dict[str, set[str]] = {}
    for file, _line, _ptype, content in findings:
        sets.setdefault(file, set()).add(content)
    return sorted(((f, len(s)) for f, s in sets.items()), key=lambda x: (-x[1], x[0]))


def _load_module_baseline() -> dict:
    """加载模块 baseline 文件(locales/baseline.json)。"""
    if not MODULE_BASELINE_FILE.exists():
        return {}
    try:
        return json.loads(MODULE_BASELINE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_module_baseline(module_counts: dict[str, int]) -> None:
    """写入模块 baseline 文件(locales/baseline.json,可序列化 JSON)。"""
    data = {
        '_description': 'R47 P1-d: 模块化 i18n 硬编码字符串 baseline(逐模块下降门禁,清零目标 0)',
        '_note': ('由 --generate-baseline / --ratchet 维护;total 只允许非增加(下降或持平)。'
                  '原 R44 baseline 954 仅覆盖 bots/+admin;纳入 services/ 后真实基线见 total。'),
        '_original_r44_baseline': ORIGINAL_R44_BASELINE,
    }
    for m in MODULE_KEYS:
        data[m] = int(module_counts.get(m, 0))
    data['total'] = sum(data[m] for m in MODULE_KEYS)
    MODULE_BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODULE_BASELINE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def _baseline_module_count(baseline: dict, m: str) -> int:
    """从 baseline dict 读取某模块的计数(容错)。"""
    try:
        return int(baseline.get(m, 0))
    except Exception:
        return 0


def _baseline_total(baseline: dict) -> int:
    """从 baseline dict 读取 total(优先 total 字段,否则回退为模块求和)。"""
    if not baseline:
        return 0
    if 'total' in baseline:
        try:
            return int(baseline['total'])
        except Exception:
            pass
    return sum(_baseline_module_count(baseline, m) for m in MODULE_KEYS)


def cmd_check(module_counts: dict[str, int], baseline: dict) -> int:
    """模块化门禁检查:任何模块超标或 total 超标 → 失败(退出码 1)。

    - 任何模块当前 > baseline → 失败
    - total 当前 > baseline.total → 失败
    - 任何模块当前 < baseline → 提示运行 --ratchet 下降 baseline(不失败)
    - 额外输出距清零目标(0)的差距
    """
    if not baseline:
        print(f"❌ 未找到模块 baseline: {MODULE_BASELINE_FILE}")
        print("   请先运行: python scripts/scan_hardcoded_strings.py --generate-baseline")
        print("   并将 locales/baseline.json 提交到 git。")
        return 1

    failed: list[tuple[str, int, int]] = []
    decreased: list[tuple[str, int, int]] = []
    for m in MODULE_KEYS:
        cur = module_counts.get(m, 0)
        base = _baseline_module_count(baseline, m)
        if cur > base:
            failed.append((m, cur, base))
        elif cur < base:
            decreased.append((m, cur, base))

    cur_total = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    base_total = _baseline_total(baseline)
    total_failed = cur_total > base_total

    # 模块明细表
    print("模块化 i18n 硬编码字符串门禁(--check)")
    print(f"  {'模块':<22}{'当前':>7}{'baseline':>10}{'状态':>6}{'距清零':>8}")
    for m in MODULE_KEYS:
        cur = module_counts.get(m, 0)
        base = _baseline_module_count(baseline, m)
        if cur > base:
            status = '超标'
        elif cur < base:
            status = '已降'
        else:
            status = '持平'
        print(f"  {m:<22}{cur:>7}{base:>10}{status:>6}{cur:>8}")
    print(f"  {'total':<22}{cur_total:>7}{base_total:>10}"
          f"{'超标' if total_failed else 'OK':>6}{cur_total:>8}")
    print(f"  (原 R44 baseline: {ORIGINAL_R44_BASELINE}, 仅覆盖 bots/+admin 范围)")

    if failed:
        print(f"\n❌ {len(failed)} 个模块超过 baseline:")
        for m, cur, base in failed:
            print(f"   {m}: {cur} > {base}(+{cur - base})")
    if total_failed:
        print(f"\n❌ total 超过 baseline: {cur_total} > {base_total}")
    if failed or total_failed:
        print("\n请修复新增的硬编码字符串并接入 i18n,而非扩大基线。")
        return 1

    if decreased:
        print(f"\n✓ {len(decreased)} 个模块已下降,建议运行 --ratchet 下降 baseline:")
        for m, cur, base in decreased:
            print(f"   {m}: {base} → {cur}(-{base - cur}, 距清零 {cur})")
        print("   鼓励每次 PR 至少下降 1 个模块的 baseline,直至清零。")
    else:
        print("\n✓ 所有模块均未超标(模块化 baseline 门禁通过)。")
    return 0


def cmd_ratchet(module_counts: dict[str, int], baseline: dict) -> int:
    """下降 baseline:自动更新 baseline.json。

    规则(R47 P1-d):
    - total 只允许非增加(new_total ≤ old_total);否则拒绝更新。
    - 模块可升降(只要 total 非增加),以便模块间再平衡。
    - 写回后提示提交到 git。
    """
    if not baseline:
        print(f"❌ 未找到模块 baseline: {MODULE_BASELINE_FILE}")
        print("   请先运行: python scripts/scan_hardcoded_strings.py --generate-baseline")
        return 1

    old_total = _baseline_total(baseline)
    new_total = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    if new_total > old_total:
        print(f"❌ ratchet 拒绝更新: total {new_total} > 旧 {old_total}(只允许非增加)")
        print("   请修复新增违规(--check 查看详情),而非扩大基线。")
        return 1

    # 模块可升降;写回当前计数(仅当 total 非增加)
    _save_module_baseline(module_counts)
    print(f"✓ baseline 已更新: {MODULE_BASELINE_FILE.name}")
    print(f"   total: {old_total} → {new_total}({new_total - old_total:+d})")
    print("   模块变化:")
    for m in MODULE_KEYS:
        old = _baseline_module_count(baseline, m)
        new = module_counts.get(m, 0)
        if new != old:
            tag = '升' if new > old else '降'
            print(f"     [{tag}] {m}: {old} → {new}({new - old:+d}, 距清零 {new})")
    print("\n   请将 locales/baseline.json 提交到 git。")
    return 0


def cmd_report(module_counts: dict[str, int], baseline: dict, findings) -> int:
    """输出模块化进度报告:每模块当前/baseline/差距/清零目标、总进度、top 10 文件。"""
    cur_total = sum(module_counts.get(m, 0) for m in MODULE_KEYS)
    base_total = _baseline_total(baseline)
    orig_scope = sum(module_counts.get(m, 0) for m in MODULE_KEYS if m != 'services/')

    print('=' * 78)
    print('R47 P1-d i18n 模块化 baseline 进度报告')
    print('=' * 78)

    print('\n【总进度】')
    print(f"  当前 total:           {cur_total}")
    print(f"  baseline total:       {base_total}")
    if base_total > 0:
        pct = (base_total - cur_total) / base_total * 100
        print(f"  较 baseline 下降:     {base_total - cur_total}({pct:.1f}%)")
    print(f"  原 R44 baseline:      {ORIGINAL_R44_BASELINE}(仅 bots/+admin 范围)")
    print(f"  原范围(bots/+admin)当前: {orig_scope}")
    if ORIGINAL_R44_BASELINE > 0:
        opct = (ORIGINAL_R44_BASELINE - orig_scope) / ORIGINAL_R44_BASELINE * 100
        print(f"  较 R44 下降:          {ORIGINAL_R44_BASELINE - orig_scope}({opct:.1f}%)")
    print(f"  清零目标:             0(尚需清理 {cur_total} 处)")

    print('\n【模块明细】(距清零 = 当前计数,目标 0)')
    print(f"  {'模块':<22}{'当前':>7}{'baseline':>10}{'距清零':>8}{'状态':>6}")
    for m in MODULE_KEYS:
        cur = module_counts.get(m, 0)
        base = _baseline_module_count(baseline, m)
        if cur > base:
            status = '超标'
        elif cur < base:
            status = '已降'
        else:
            status = '持平'
        print(f"  {m:<22}{cur:>7}{base:>10}{cur:>8}{status:>6}")

    print('\n【Top 10 硬编码字符串文件】(按去重违规数)')
    fc = file_counts(findings)
    if not fc:
        print('  (无违规)')
    for i, (f, c) in enumerate(fc[:10], 1):
        print(f"  {i:>2}. {f:<52}{c:>5}")

    print('=' * 78)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='R47 P1-d 模块化 i18n 硬编码字符串扫描与逐模块下降门禁',
    )
    parser.add_argument('--check', action='store_true',
                        help='模块化门禁检查(默认行为)')
    parser.add_argument('--ratchet', action='store_true',
                        help='下降 baseline.json(模块可升降,total 只允许非增加)')
    parser.add_argument('--report', action='store_true',
                        help='输出模块化进度报告')
    parser.add_argument('--generate-baseline', action='store_true',
                        help='生成/重建 locales/baseline.json(仅用于初始基线)')
    args = parser.parse_args(argv)

    root = Path(__file__).parent.parent
    findings = collect_findings(root)
    module_counts = count_by_module(findings)
    baseline = _load_module_baseline()

    if args.generate_baseline:
        # R46 P1: 添加警告 — 仅用于初始基线生成,不应在 PR 中使用
        print("⚠️  警告: --generate-baseline 仅用于初始基线生成/重建,不应在 PR 中使用。")
        print("   PR 中新增违规应修复后接入 i18n,而非纳入基线。")
        print("   CI 中应使用 --ratchet 下降 baseline(只降不升)。\n")
        _save_module_baseline(module_counts)
        print(f"✓ 模块 baseline 已生成: {MODULE_BASELINE_FILE}")
        print("  模块明细:")
        for m in MODULE_KEYS:
            print(f"    {m:<22}{module_counts.get(m, 0):>6}")
        print(f"    {'total':<22}{sum(module_counts.get(m, 0) for m in MODULE_KEYS):>6}")
        return 0

    if args.ratchet:
        return cmd_ratchet(module_counts, baseline)

    if args.report:
        return cmd_report(module_counts, baseline, findings)

    # 默认行为:模块化检查(向后兼容 CI 的 `python scripts/scan_hardcoded_strings.py`)
    return cmd_check(module_counts, baseline)


if __name__ == '__main__':
    sys.exit(main())
