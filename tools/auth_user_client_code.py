import asyncio

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from core.config_loader import load_telegram_credentials, normalize_phone


async def main() -> None:
    creds = load_telegram_credentials(require_phone=False)
    phone = creds.phone
    if not phone:
        phone = normalize_phone(input("Введите TG_PHONE (+7...): ").strip())

    client = TelegramClient(StringSession(), creds.api_id, creds.api_hash)

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
        except PhoneCodeInvalidError as exc:
            raise RuntimeError("Неверный код подтверждения") from exc

        print("USER_SESSION_STRING=")
        print(client.session.save())
    except PhoneNumberInvalidError:
        print("Некорректный номер телефона (PhoneNumberInvalidError)", flush=True)
    except ApiIdInvalidError:
        print("Некорректные API_ID/API_HASH (ApiIdInvalidError)", flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
