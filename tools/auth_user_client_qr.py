import argparse
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from core.config_loader import load_telegram_credentials


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorize Telegram user session via QR login")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for QR confirmation (default: 300)")
    parser.add_argument("--loop", action="store_true", help="Regenerate QR on timeout and continue waiting")
    return parser


async def _run_qr_flow(client: TelegramClient, timeout: int) -> bool:
    qr = await client.qr_login()
    print("QR login URL:")
    print(qr.url)
    print("Откройте ссылку/QR в Telegram и подтвердите вход.", flush=True)

    try:
        await qr.wait(timeout=timeout)
        return True
    except asyncio.TimeoutError:
        print(
            "Таймаут ожидания QR. Подсказка: обновите Telegram, откройте на телефоне или сканируйте QR через Settings→Devices.",
            flush=True,
        )
        return False


async def main() -> None:
    args = _build_parser().parse_args()
    creds = load_telegram_credentials(require_phone=False)
    client = TelegramClient(StringSession(), creds.api_id, creds.api_hash)

    try:
        await client.connect()

        while True:
            success = await _run_qr_flow(client, timeout=args.timeout)
            if success:
                break
            if not args.loop:
                raise RuntimeError("QR login timeout")

        print("USER_SESSION_STRING=")
        print(client.session.save())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
