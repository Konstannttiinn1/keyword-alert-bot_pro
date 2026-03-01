import asyncio
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent.parent
GLOBAL_CONFIG_PATH = BASE_DIR / "config" / "global.json"


def _load_global_config() -> dict:
    if not GLOBAL_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _resolve_api_credentials() -> tuple[int, str]:
    cfg = _load_global_config()

    api_id_raw = os.getenv("TG_API_ID") or cfg.get("api_id")
    api_hash = os.getenv("TG_API_HASH") or cfg.get("api_hash")

    if not api_id_raw or not api_hash:
        raise RuntimeError("Укажите TG_API_ID и TG_API_HASH (env) или api_id/api_hash в config/global.json")

    try:
        api_id = int(str(api_id_raw))
    except ValueError as exc:
        raise RuntimeError("TG_API_ID/api_id должен быть числом") from exc

    return api_id, str(api_hash)


async def main() -> None:
    api_id, api_hash = _resolve_api_credentials()
    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.connect()

        qr = await client.qr_login()
        print("QR login URL:")
        print(qr.url)
        print("Откройте ссылку/QR в Telegram и подтвердите вход.")

        await qr.wait()

        print("USER_SESSION_STRING=")
        print(client.session.save())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
