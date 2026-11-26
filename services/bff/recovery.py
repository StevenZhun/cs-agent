"""单进程后台恢复者：先核对图终态，不自动重跑未知执行。"""

import asyncio
import logging

from services.bff.app import app

logger = logging.getLogger(__name__)


async def main() -> None:
    """每隔五秒寻找一条已过期的运行，退出时关闭数据库连接池。"""

    try:
        while True:
            try:
                found = await app.state.customer_message_service.recover_one()
            except Exception:
                # 一条坏记录不能让恢复进程永久停摆；错误保留在日志供人工核对。
                logger.exception("图运行恢复失败；稍后再试")
                found = False
            if not found:
                await asyncio.sleep(5)
    finally:
        await app.state.sop_redis.aclose()
        await app.state.database_engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
