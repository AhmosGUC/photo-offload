from collections import Counter
import asyncio, sqlite3, sys
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

DB = sys.argv[1] if len(sys.argv) > 1 else "probe/Photos.sqlite"

def library_items():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT ZDIRECTORY, ZFILENAME FROM ZASSET WHERE ZTRASHEDSTATE=0"
    ).fetchall()
    con.close()
    return [f"/{d}/{f}" for d, f in rows]

async def phone_files():
    lockdown = await create_using_usbmux(connection_type="USB")
    async with AfcService(lockdown) as afc:
        paths = set()
        for folder in await afc.listdir('/DCIM'):
            if folder.endswith('APPLE'):
                for name in await afc.listdir(f'/DCIM/{folder}'):
                    paths.add(f'/DCIM/{folder}/{name}')
        return paths


async def main():
    items = library_items()
    on_phone = await phone_files()
    not_in_dcim = [p for p in items if p not in on_phone]

    lockdown = await create_using_usbmux(connection_type="USB")
    async with AfcService(lockdown) as afc:
        truly_missing = [p for p in not_in_dcim if not await afc.exists(p)]

    def group(p):
        return "/".join(p.split("/")[1:3])

    print(f"{len(items)} library items")
    print(f"{len(not_in_dcim)} outside DCIM, by location:", dict(Counter(map(group, not_in_dcim))))
    print(f"{len(truly_missing)} truly missing, by location:", dict(Counter(map(group, truly_missing))))
    for p in truly_missing[:10]:
        print("  missing:", p)

asyncio.run(main())
