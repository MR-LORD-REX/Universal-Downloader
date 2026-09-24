from downloader import Downloader
import asyncio

url = "https://x.com/genshinmem_es/status/2100652253917098215?s=20"

async def main():

    async with Downloader() as dl:
        meta = await dl.get_metadata(url)
        quota = 50 * 1024 * 1024
        print(meta.media_type, meta.media_group_type, meta.links(), meta.size_human)
        if dl.affordable(meta, quota)[0]:
            result = await dl.download(meta, quality="1080p", target="disk", max_size_bytes=quota,)
            await result.save("downloads", pattern="{platform}_{id}_{index}_{quality}.{ext}")

asyncio.run(main())