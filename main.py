import aiohttp                                    # 文档要求：不要用 requests
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp
import zhconv

try:
    from . import tracemoe  # 相对导入：插件自带的 tracemoe.py
except ImportError:  # 兼容以文件方式加载的场景
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tracemoe



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

        image_path = await images[0].convert_to_file_path()

        def fand(path):
            sc = tracemoe.search_file(path)
            en = str(sc[0]).split()
            jtz = zhconv.convert(en[0], "zh-hans")
            return f"名称:{jtz}\n具体时间:{en[1]},{en[2]}\n{en[3]}:{en[4]}"

        q = fand(image_path)
        yield event.plain_result(q)



