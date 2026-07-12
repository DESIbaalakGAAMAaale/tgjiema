"""项目通用异常定义。

R36 Batch 4 H3: 异常安全双写降级为日志。

引入 DurabilityError 用于划分两类状态:
- 业务正确性必需: session/outbox/receipt/task 写入失败必须失败/重试当前请求
- 仅可观测性: metrics、调试日志可 best-effort(不影响主流程)

db_writer._execute_sqlite 对业务必需写入检查返回值,返回 False/0 时抛 DurabilityError。
DurabilityError 被捕获后入死信队列(永久死信,不重试当前消息),避免静默数据丢失。
"""


class DurabilityError(Exception):
    """持久化必需操作失败,调用方必须失败/重试当前请求。

    触发场景(db_writer._execute_sqlite 内):
    - bool 返回的业务方法返回 False(状态迁移未生效/记录不存在)
    - int 返回的业务方法返回 0(任务创建失败)
    - 注意: 返回 None 的方法(create_upload_session 等)在 Writer 事务模式下
      失败会 raise,不通过返回值判断;None 是正常返回,不触发本异常。

    处理方式(db_writer._process_message):
    - 抛出后 _execute_atomic 执行 ROLLBACK(业务写+inbox 一起回滚)
    - _process_message 捕获后入死信队列(permanent=True,不重试当前消息)
      因为业务必需写入失败通常是逻辑问题(状态不匹配/记录不存在),
      重试也会得到同样结果,故标记为永久死信等待人工审核。

    与普通 Exception 的区别:
    - 普通 Exception(如 sqlite3.OperationalError: database is locked):
      可重试,入死信队列(attempts 递增,DLQ Worker 延迟重试回主 Stream)
    - DurabilityError: 不可重试(逻辑失败),入永久死信
    """

    pass
