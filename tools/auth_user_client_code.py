import asyncio
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
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


def _normalize_phone(phone: str) -> str:
    clean = phone.strip().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    if clean and not clean.startswith("+"):
        clean = "+" + clean
    return clean


def _resolve_inputs() -> tuple[int, str, str]:
    cfg = _load_global_config()

    api_id_raw = os.getenv("TG_API_ID") or cfg.get("api_id")
    api_hash = os.getenv("TG_API_HASH") or cfg.get("api_hash")
    phone_raw = os.getenv("TG_PHONE") or cfg.get("tg_phone") or ""

    if not api_id_raw or not api_hash:
        raise RuntimeError("Укажите TG_API_ID и TG_API_HASH (env) или api_id/api_hash в config/global.json")

    try:
        api_id = int(str(api_id_raw))
    except ValueError as exc:
        raise RuntimeError("TG_API_ID/api_id должен быть числом") from exc

    if not phone_raw:
        phone_raw = input("Введите TG_PHONE (+7...): ").strip()

    phone = _normalize_phone(phone_raw)
    if not phone:
        raise RuntimeError("TG_PHONE не задан")

    return api_id, str(api_hash), phone


async def main() -> None:
    api_id, api_hash, phone = _resolve_inputs()
    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.connect()

        sent = await client.send_code_request(phone)
        phone_code_hash = sent.phone_code_hash

        print("Код отправлен в Telegram app.")
        print("Введите 5-значный app-код.")
        code = input("Код: ").strip()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            password = input("Включен 2FA. Введите пароль: ").strip()
            await client.sign_in(password=password)
        except PhoneCodeInvalidError:
            raise RuntimeError("Неверный код подтверждения")

        print("USER_SESSION_STRING=")
        print(client.session.save())
    except PhoneNumberInvalidError:
        print("Некорректный номер телефона (PhoneNumberInvalidError)")
    except ApiIdInvalidError:
        print("Некорректные API_ID/API_HASH (ApiIdInvalidError)")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
