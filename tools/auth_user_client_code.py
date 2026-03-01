import asyncio
import re
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PasswordTooFreshError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from core.config_loader import load_telegram_credentials, normalize_phone

PHONE_RE = re.compile(r"^\+[0-9]{8,15}$")


def _is_valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.fullmatch(phone))


async def _handle_2fa(client: TelegramClient) -> bool:
    for attempt in range(1, 4):
        print("[2FA] Запрос пароля...", flush=True)
        password = getpass("Введите пароль 2FA: ")
        print("[2FA] Пароль введен, отправляю...", flush=True)
        try:
            await client.sign_in(password=password)
            print("[2FA] Проверка авторизации...", flush=True)
            authorized = await client.is_user_authorized()
            print(f"[2FA] is_user_authorized: {authorized}", flush=True)
            if authorized:
                print(f"[2FA] session.save(): {client.session.save()[:20]}...", flush=True)
            return True
        except PasswordHashInvalidError:
            print(f"Неверный пароль 2FA (попытка {attempt}/3)", flush=True)
        except PasswordTooFreshError:
            print("Пароль 2FA слишком свежий. Подождите и попробуйте снова.", flush=True)
            return False
    print("Достигнут лимит попыток пароля 2FA.", flush=True)
    return False


async def main() -> None:
    creds = load_telegram_credentials(require_phone=False)
    default_phone = normalize_phone(creds.phone) if creds.phone else ""

    while True:
        prompt = "Введите TG_PHONE (+7...)"
        if default_phone:
            prompt += f" [Enter = {default_phone}]"
        prompt += ": "
        raw = input(prompt).strip()
        phone = normalize_phone(raw) if raw else default_phone
        source = "user_input" if raw else "default"
        if phone and _is_valid_phone(phone):
            break
        print("Некорректный номер. Поддержка форматов: 8XXX, +7XXX, +7 XXX XXX-XX-XX", flush=True)

    print(f"Using phone={phone} source={source}", flush=True)

    client = TelegramClient(StringSession(), creds.api_id, creds.api_hash)

    try:
        await client.connect()

        sent = await client.send_code_request(phone)
        phone_code_hash = sent.phone_code_hash

        print("Код отправлен в Telegram app.")
        code = input("Введите код из Telegram: ").strip()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            authorized = await client.is_user_authorized()
            print(f"[2FA] is_user_authorized: {authorized}", flush=True)
            if not authorized:
                return
        except SessionPasswordNeededError:
            print("Требуется пароль 2FA.", flush=True)
            ok = await _handle_2fa(client)
            if not ok:
                return
        except PhoneCodeInvalidError:
            print("Неверный код подтверждения", flush=True)
            return

        print("Авторизация успешна.")
        print("USER_SESSION_STRING=")
        print(client.session.save())
    except FloodWaitError as exc:
        print(f"Слишком много попыток. Подождите {exc.seconds} секунд.", flush=True)
    except PhoneNumberInvalidError:
        print("Некорректный номер телефона. Введите номер заново.", flush=True)
    except ApiIdInvalidError:
        print("Некорректные API_ID/API_HASH. Получите их на my.telegram.org.", flush=True)
    except Exception:
        print("Ошибка авторизации по коду.", flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
