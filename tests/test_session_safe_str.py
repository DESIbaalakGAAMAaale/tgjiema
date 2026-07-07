"""回归测试 3 —— session._safe_str / _json_dumps 不向 stdout 泄露敏感负载（P0 日志脱敏）。

P0-4 修复点：_safe_str / _json_dumps 不得把原始 payload（密钥、文件内容等）打印到
stdout/stderr，避免日志泄露。

本测试用 monkeypatch 接管 builtins.print（捕获任何 print 调用），构造含**唯一敏感
标记串** SECRET_MARKER_XYZ123 的 payload，分别调用两个修复点。断言：被接管的 print
收到的所有参数中，**不包含**该标记串。
- 当前修复下：两个函数不调用 print → 标记串不会出现在 print 参数中 → 通过。
- 若有人回归、在 _safe_str / _json_dumps 重新加入 `print(payload)` 或
  `print(_safe_str(payload))` 之类的泄露语句 → 标记串会出现在 print 参数中 → 断言失败。
这正是非空（non-vacuous）回归保护：它实际检查函数是否触发了泄露，而非检查一段
本就为空的输出（旧版用 capsys 断言空输出，形同无保护）。
"""

import builtins
import json

from database.session import _safe_str, _json_dumps


# 唯一敏感标记串：正常代码不会输出它；一旦有人在 _safe_str/_json_dumps 内
# 重新 print(payload)，该串就会出现在 print 参数里，触发回归失败。
SECRET_MARKER = "SECRET_MARKER_XYZ123"


def test_safe_str_does_not_print_payload(monkeypatch):
    # 接管 builtins.print，收集所有 print 收到的位置参数
    printed_args = []

    def _spy_print(*args, **kwargs):
        for a in args:
            try:
                printed_args.append(str(a))
            except Exception:
                printed_args.append(repr(a))

    monkeypatch.setattr(builtins, "print", _spy_print)

    # 覆盖 P0-4 两个修复点：_safe_str 与 _json_dumps
    # bytes 经 _safe_str 内部 decode 处理；标准 json 不序列化 bytes，
    # 故 bytes 仅传给 _safe_str，不传给 _json_dumps（避免非回归性 TypeError）。
    safe_str_payloads = [
        SECRET_MARKER,                                  # 纯字符串
        SECRET_MARKER.encode(),                         # bytes
        {"password": SECRET_MARKER, "user": "alice"},   # dict
        ["a", SECRET_MARKER, 1],                        # list
    ]
    json_dumps_payloads = [
        SECRET_MARKER,
        {"password": SECRET_MARKER, "user": "alice"},
        ["a", SECRET_MARKER, 1],
    ]

    for payload in safe_str_payloads:
        _safe_str(payload)
    for payload in json_dumps_payloads:
        _json_dumps(payload)

    # 控制断言：证明 spy 真的接管了 builtins.print。
    # 若 monkeypatch 未生效（如环境异常），print 会走真实 stdout，spy 收不到，
    # 此断言失败——从而暴露"测试本身失效"，避免下面的"不含标记"沦为真空断言。
    print("__P0_4_CONTROL_TOKEN__")  # noqa: T201
    assert "__P0_4_CONTROL_TOKEN__" in "\n".join(printed_args)

    # 核心断言：_safe_str / _json_dumps 不得把敏感标记串泄露到 stdout。
    captured = "\n".join(printed_args)
    assert SECRET_MARKER not in captured, (
        f"P0-4 回归失败：_safe_str/_json_dumps 向 stdout 泄露了敏感标记 {SECRET_MARKER!r}。"
        "请检查是否有人在函数体内重新加入了 print(payload) 类泄露语句。"
    )


def test_safe_str_preserves_types():
    """类型与特殊值应被安全保留，且同样不打印。"""
    assert _safe_str(None) is None
    assert _safe_str(42) == 42
    assert _safe_str(True) is True
    assert _safe_str("plain") == "plain"
    # 列表/字典走 json
    assert json.loads(_safe_str([1, 2, 3])) == [1, 2, 3]
