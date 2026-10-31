import asyncio
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

async def main():
    lockdown = await create_using_usbmux(connection_type="USB")
    async with AfcService(lockdown) as afc:
        count, size = 0, 0
        for folder in await afc.listdir('/DCIM'):
            if not folder.endswith('APPLE'):
                continue
            for name in await afc.listdir(f'/DCIM/{folder}'):
                st = await afc.stat(f'/DCIM/{folder}/{name}')
                count += 1
                size += st['st_size']
        print(f"{count} files, {size / 1e9:.2f} GB")

asyncio.run(main())
