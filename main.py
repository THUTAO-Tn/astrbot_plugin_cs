import aiohttp                                    # 文档要求：不要用 requests
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)


    @filter.command("查询")
    async def find(self, event: AstrMessageEvent):
        """下面是调用查询动漫的api"""
        images = [seg for seg in event.get_messages() if isinstance(seg, Comp.Image)]
        if not images:
            yield event.plain_result("请发送图片")
            logger.info("测试111")
            return
