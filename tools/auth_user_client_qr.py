import argparse
import asyncio
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from core.config_loader import BASE_DIR, load_telegram_credentials


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorize Telegram user session via QR login")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for QR confirmation (default: 300)")
    parser.add_argument("--loop", action="store_true", help="Regenerate QR on timeout and continue waiting")
    parser.add_argument("--out", default="qr_login.png", help="Output PNG path for generated QR (default: qr_login.png)")
    return parser


def _save_qr_if_available(url: str, out_path: Path) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        print("Пакет qrcode не установлен.", flush=True)
        print("Установите: pip install qrcode[pil]", flush=True)
        print("После установки запустите скрипт снова, чтобы получить PNG QR.", flush=True)
        print(f"Либо откройте URL вручную и сгенерируйте QR: {url}", flush=True)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(url)
    img.save(out_path)
    print(f"QR PNG сохранён: {out_path}", flush=True)


async def _handle_2fa(client: TelegramClient) -> bool:
    for attempt in range(1, 4):
        password = getpass("Введите пароль 2FA: ")
        try:
            await client.sign_in(password=password)
            return True
        except PasswordHashInvalidError:
            print(f"Неверный пароль 2FA (попытка {attempt}/3)", flush=True)
    print("Достигнут лимит попыток пароля 2FA.", flush=True)
    return False


async def _run_qr_flow(client: TelegramClient, timeout: int, out_path: Path) -> bool:
    qr = await client.qr_login()
    print("QR login URL:")
    print(qr.url)
    _save_qr_if_available(qr.url, out_path)

    print("Инструкция:", flush=True)
    print("Телефон → Telegram → Настройки → Устройства → Сканировать QR", flush=True)

    try:
        await qr.wait(timeout=timeout)
        return True
    except SessionPasswordNeededError:
        print("Для завершения входа требуется пароль 2FA.", flush=True)
        return await _handle_2fa(client)
    except asyncio.TimeoutError:
        print(
            "Таймаут ожидания QR. Подсказка: обновите Telegram, откройте на телефоне или сканируйте QR через Settings→Devices.",
            flush=True,
        )
        return False


async def main() -> None:
    args = _build_parser().parse_args()
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = BASE_DIR / out_path

    creds = load_telegram_credentials(require_phone=False)
    client = TelegramClient(StringSession(), creds.api_id, creds.api_hash)

    try:
        await client.connect()

        while True:
            success = await _run_qr_flow(client, timeout=args.timeout, out_path=out_path)
            if success:
                break
            if not args.loop:
                raise RuntimeError("QR login timeout")

        print("USER_SESSION_STRING=")
        print(client.session.save())
        print("Сохраните строку в env или config/global.json -> user_session_string", flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
