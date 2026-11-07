from email.mime import image

form astrbot.api.event import filter,AstrMessageEventReault
from astrbot.api.star import Context, Star
from astrbot.api import logger

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)


    @filter.command("查询"):
    async def find(self, event: AstrMessageEventReault):
        """下面是调用查询动漫的api"""
        user_image = event.get_message(image)
        logger.info(user_image)
        yield event.image_result(user_image)