import asyncio

from telegram import Bot
from telegram.ext import Application
from loguru import logger

from config import settings
from services.queue_manager import dequeue_send_task
from utils.monitor import metrics

TOKEN = settings.BOT_TOKENS.get("SENDER_BOT", "")


async def process_queue(bot: Bot):
    while True:
        try:
            task = await dequeue_send_task()
            if task is None:
                await asyncio.sleep(0.5)
                continue

            logger.info(
                f"发送文件: 用户 {task.target_user_id}, "
                f"频道 {task.channel_id}, 消息 {task.message_id}"
            )

            try:
                await bot.forward_message(
                    chat_id=task.target_user_id,
                    from_chat_id=task.channel_id,
                    message_id=task.message_id,
                )
                logger.info(
                    f"文件发送成功: 用户 {task.target_user_id}, 码 {task.file_code}"
                )
                metrics.send_success_count += 1
                metrics.record_processed("sender_bot")
            except Exception as e:
                logger.error(f"文件发送失败: {e}")
                metrics.send_fail_count += 1
                metrics.record_error("sender_bot")
                try:
                    await bot.send_message(
                        chat_id=task.target_user_id,
                        text="文件发送失败，请稍后重试或联系管理员。",
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"队列处理异常: {e}")
            await asyncio.sleep(1)


async def _init():
    from database import init_db
    await init_db()


def run():
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(_init())
    logger.info("启动文件发送机器人...")
    app = Application.builder().token(TOKEN).build()
    bot = app.bot

    metrics.ping_bot("sender_bot")

    async def health_ping():
        while True:
            metrics.ping_bot("sender_bot")
            await asyncio.sleep(30)

    async def main():
        await asyncio.gather(
            process_queue(bot),
            health_ping(),
        )

    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(main())


if __name__ == "__main__":
    run()