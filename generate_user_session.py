import asyncio
import json
import re
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
GLOBAL_CONFIG_PATH = BASE_DIR / "config" / "global.json"


def _load_defaults() -> tuple[str, str]:
    if not GLOBAL_CONFIG_PATH.exists():
        return "", ""
    try:
        cfg = json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "", ""
    api_id = str(cfg.get("api_id", "") or "")
    api_hash = str(cfg.get("api_hash", "") or "")
    return api_id, api_hash


def _prompt_with_default(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _normalize_phone(raw: str) -> str:
    compact = re.sub(r"[\s\-()]+", "", raw)
    if compact.startswith("+"):
        compact = "+" + re.sub(r"\D", "", compact[1:])
    else:
        compact = "+" + re.sub(r"\D", "", compact)
    return compact


def _safe_attr(value: Any, attr: str) -> Any:
    return getattr(value, attr, None)


def _describe_code_delivery(sent: Any) -> str:
    sent_type = _safe_attr(sent, "type")
    next_type = _safe_attr(sent, "next_type")
    timeout = _safe_attr(sent, "timeout")
    return (
        f"sent.type={sent_type} | "
        f"sent.next_type={next_type} | "
        f"sent.timeout={timeout}"
    )


async def main() -> None:
    default_api_id, default_api_hash = _load_defaults()

    api_id_raw = _prompt_with_default("API_ID", default_api_id)
    api_hash = _prompt_with_default("API_HASH", default_api_hash)
    phone_raw = input("PHONE (+7... можно с пробелами/скобками): ").strip()

    if not api_id_raw or not api_hash:
        raise RuntimeError("API_ID и API_HASH обязательны")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("API_ID должен быть числом") from exc

    phone = _normalize_phone(phone_raw)
    if len(phone) < 8:
        raise RuntimeError("Некорректный номер телефона")

    print(f"Нормализованный номер: {phone}", flush=True)
    print("Логин user-аккаунта Telegram (bot_token не используется).", flush=True)

    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.connect()

        sent = await client.send_code_request(phone, force_sms=False)
        print("Код запрошен:", flush=True)
        print(_describe_code_delivery(sent), flush=True)

        while True:
            code = input("Введите код из Telegram (или 'sms' для force_sms): ").strip()
            if code.lower() == "sms":
                sent = await client.send_code_request(phone, force_sms=True)
                print("Повторный запрос через force_sms=True:", flush=True)
                print(_describe_code_delivery(sent), flush=True)
                continue

            try:
                await client.sign_in(phone=phone, code=code)
                break
            except PhoneCodeInvalidError:
                print("Неверный код. Попробуйте снова.", flush=True)
                continue
            except SessionPasswordNeededError:
                password = input("Включена 2FA. Введите пароль: ").strip()
                await client.sign_in(password=password)
                break

        print("\nUSER_SESSION_STRING:")
        print(client.session.save())
        print("\nВставьте строку в config/global.json в поле user_session_string.")

    except FloodWaitError as exc:
        print(f"Подождите {exc.seconds} секунд", flush=True)
    except PhoneNumberInvalidError:
        print("Некорректный номер телефона (PhoneNumberInvalidError)", flush=True)
    except ApiIdInvalidError:
        print("Некорректные API_ID/API_HASH (ApiIdInvalidError)", flush=True)
    except Exception as exc:
        print(f"Неожиданная ошибка: {type(exc).__name__}: {exc}", flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
