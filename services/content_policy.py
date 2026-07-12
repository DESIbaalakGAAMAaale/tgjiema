"""R40 §9.4: 文件策略插件 — 类型/大小/恶意内容检查。

本模块提供文件上传前的安全策略检查能力,采用可扩展的插件机制:
- 内置插件: 文件大小限制 / 文件类型黑白名单 / 文件名安全检查
- 自定义插件: 通过 register_plugin() 注册第三方策略
- 统一入口: check_file() 串行执行所有已注册插件,任一拒绝即拒绝

设计约束:
- 策略插件为纯同步函数(检查逻辑无 IO),check_file 为 async 入口
- 使用 dataclass 定义 PolicyResult / FileMeta,便于结构化传递
- 模块加载时自动调用 _init_builtin_plugins() 注册内置插件
"""
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Any

from loguru import logger


# ─── 可配置阈值 ──────────────────────────────────────────────
# 文件大小上限(字节),默认 2GB(R40 §9.4 配置项,可通过 register_plugin 覆盖)
DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# 默认禁止扩展名(可执行脚本 / 动态库 / 宏文档等高风险类型)
DEFAULT_BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs",
    ".js", ".jar", ".dll", ".scr",
}

# 路径遍历与特殊字符检测模式
# - ../ 或 ..\\ 序列(路径遍历)
# - 绝对路径前缀 / 或盘符 C:
# - 控制字符与空字节
_PATH_TRAVERSAL_PATTERN = re.compile(r"(\.\.[/\\])|(^[/\\])|([A-Za-z]:[\\/])|([\x00-\x1f])")
# 文件名长度上限
DEFAULT_MAX_FILENAME_LEN = 255


@dataclass
class PolicyResult:
    """策略检查结果。

    Attributes:
        allowed: 是否允许通过
        reason: 拒绝原因(allowed=False 时填写)
        policy_name: 拒绝策略名称(便于审计定位)
        metadata: 附加元数据(如命中规则、阈值等)
    """
    allowed: bool = True
    reason: str = ""
    policy_name: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class FileMeta:
    """待检查文件元数据。

    Attributes:
        file_name: 文件名(含扩展名)
        file_size: 文件大小(字节)
        file_type: MIME 类型(如 application/pdf)
        file_ext: 扩展名(不含点,小写)
        file_id: 文件唯一标识(可选)
        user_id: 上传者 id(可选,用于配额相关策略)
    """
    file_name: str = ""
    file_size: int = 0
    file_type: str = ""        # mime_type
    file_ext: str = ""        # 扩展名(不含.)
    file_id: str = ""
    user_id: int = 0


# ─── 策略插件注册表 ──────────────────────────────────────────
# name → handler(FileMeta) -> PolicyResult
_POLICY_PLUGINS: dict[str, Callable[[FileMeta], PolicyResult]] = {}


def register_plugin(name: str, handler: Callable[[FileMeta], PolicyResult]) -> bool:
    """注册策略插件。

    同名插件会被覆盖(便于热更新策略)。

    Args:
        name: 插件名称(唯一标识)
        handler: 策略处理函数(FileMeta → PolicyResult)

    Returns:
        True 注册成功;False 参数非法
    """
    if not name or not callable(handler):
        logger.warning(
            f"[ContentPolicy] register_plugin 非法参数 name={name} handler={handler}"
        )
        return False
    _POLICY_PLUGINS[name] = handler
    logger.info(f"[ContentPolicy] 注册策略插件: {name}")
    return True


def unregister_plugin(name: str) -> bool:
    """注销插件。

    Args:
        name: 插件名称

    Returns:
        True 注销成功;False 插件不存在
    """
    if name in _POLICY_PLUGINS:
        del _POLICY_PLUGINS[name]
        logger.info(f"[ContentPolicy] 注销策略插件: {name}")
        return True
    logger.warning(f"[ContentPolicy] 注销插件不存在: {name}")
    return False


def list_plugins() -> list[dict]:
    """列出已注册插件。

    Returns:
        [{"name": ..., "handler": <函数名>}, ...]
    """
    result = []
    for name, handler in _POLICY_PLUGINS.items():
        result.append({
            "name": name,
            "handler": getattr(handler, "__name__", str(handler)),
        })
    return result


async def check_file(file_meta: FileMeta) -> PolicyResult:
    """检查文件是否符合策略(运行所有已注册插件,任一拒绝即拒绝)。

    策略串行执行,首个拒绝立即返回(fail-fast)。
    插件异常视为允许通过(避免单插件 bug 阻塞全部上传),
    并记录 warning 日志便于排查。

    Args:
        file_meta: 待检查文件元数据

    Returns:
        PolicyResult: 任一插件拒绝则 allowed=False;全通过 allowed=True
    """
    if not _POLICY_PLUGINS:
        # 无插件注册时默认放行(可由部署方按需注册)
        return PolicyResult(allowed=True, policy_name="default_allow")
    for name, handler in _POLICY_PLUGINS.items():
        try:
            result = handler(file_meta)
        except Exception as e:
            # 插件异常不阻塞上传,记录 warning 便于排查
            logger.warning(
                f"[ContentPolicy] 插件 {name} 异常(忽略,按放行处理): {e}"
            )
            continue
        if not result.allowed:
            logger.info(
                f"[ContentPolicy] 文件被拒 file={file_meta.file_name} "
                f"policy={name} reason={result.reason}"
            )
            return result
    return PolicyResult(allowed=True, policy_name="all_passed")


# ─── 内置策略插件 ────────────────────────────────────────────

def _check_file_size(file_meta: FileMeta) -> PolicyResult:
    """文件大小限制(默认 2GB,可配置)。

    通过修改模块级常量 DEFAULT_MAX_FILE_SIZE 调整阈值。
    """
    if file_meta.file_size < 0:
        return PolicyResult(
            allowed=False, policy_name="file_size",
            reason="文件大小非法(负值)",
            metadata={"file_size": file_meta.file_size},
        )
    if file_meta.file_size > DEFAULT_MAX_FILE_SIZE:
        return PolicyResult(
            allowed=False, policy_name="file_size",
            reason=f"文件大小 {file_meta.file_size} 字节超过上限 "
                   f"{DEFAULT_MAX_FILE_SIZE} 字节",
            metadata={
                "file_size": file_meta.file_size,
                "max_size": DEFAULT_MAX_FILE_SIZE,
            },
        )
    return PolicyResult(allowed=True, policy_name="file_size")


def _check_file_type(file_meta: FileMeta) -> PolicyResult:
    """文件类型白名单/黑名单。

    默认禁止: .exe .bat .cmd .sh .ps1 .vbs .js .jar .dll .scr
    优先使用 file_ext;若为空则从 file_name 解析扩展名。
    """
    ext = (file_meta.file_ext or "").lower().lstrip(".")
    if not ext and file_meta.file_name:
        # 从文件名解析扩展名
        last_dot = file_meta.file_name.rfind(".")
        if last_dot >= 0 and last_dot < len(file_meta.file_name) - 1:
            ext = file_meta.file_name[last_dot + 1:].lower()
    blocked_exts = {e.lstrip(".").lower() for e in DEFAULT_BLOCKED_EXTENSIONS}
    if ext and ext in blocked_exts:
        return PolicyResult(
            allowed=False, policy_name="file_type",
            reason=f"文件类型 .{ext} 被禁止上传(高风险可执行/脚本类型)",
            metadata={"ext": ext, "blocked_list": sorted(blocked_exts)},
        )
    return PolicyResult(allowed=True, policy_name="file_type")


def _check_file_name(file_meta: FileMeta) -> PolicyResult:
    """文件名安全检查(路径遍历、特殊字符)。

    检查项:
    - 路径遍历序列(../ 或 ..\\)
    - 绝对路径前缀(/ 或 C:\\)
    - 控制字符与空字节(\\x00-\\x1f)
    - 文件名长度超限
    - 文件名为空
    """
    name = file_meta.file_name or ""
    if not name:
        return PolicyResult(
            allowed=False, policy_name="file_name",
            reason="文件名为空",
            metadata={},
        )
    if len(name) > DEFAULT_MAX_FILENAME_LEN:
        return PolicyResult(
            allowed=False, policy_name="file_name",
            reason=f"文件名长度 {len(name)} 超过上限 {DEFAULT_MAX_FILENAME_LEN}",
            metadata={"name_len": len(name), "max_len": DEFAULT_MAX_FILENAME_LEN},
        )
    if _PATH_TRAVERSAL_PATTERN.search(name):
        return PolicyResult(
            allowed=False, policy_name="file_name",
            reason="文件名包含路径遍历序列或非法特殊字符",
            metadata={"file_name": name},
        )
    return PolicyResult(allowed=True, policy_name="file_name")


def _init_builtin_plugins():
    """初始化内置插件(模块加载时自动调用)。

    注册三个内置策略: file_size / file_type / file_name。
    幂等:重复调用安全(同名校验覆盖)。
    """
    register_plugin("file_size", _check_file_size)
    register_plugin("file_type", _check_file_type)
    register_plugin("file_name", _check_file_name)
    logger.debug(
        f"[ContentPolicy] 内置插件初始化完成,共 {len(_POLICY_PLUGINS)} 个插件"
    )


# 模块加载时自动注册内置插件
_init_builtin_plugins()
