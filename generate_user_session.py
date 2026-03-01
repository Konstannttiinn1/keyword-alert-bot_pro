import asyncio

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from core.config_loader import load_telegram_credentials, normalize_phone


def _describe_code_delivery(sent) -> str:
    sent_type = getattr(sent, "type", None)
    next_type = getattr(sent, "next_type", None)
    timeout = getattr(sent, "timeout", None)
    return f"sent.type={sent_type} | sent.next_type={next_type} | sent.timeout={timeout}"


async def main() -> None:
    creds = load_telegram_credentials(require_phone=False)
    phone = creds.phone
    if not phone:
        phone = normalize_phone(input("PHONE (+7... можно с пробелами/скобками): ").strip())

    print(f"Нормализованный номер: {phone}", flush=True)
    print("Логин user-аккаунта Telegram (bot_token не используется).", flush=True)

    client = TelegramClient(StringSession(), creds.api_id, creds.api_hash)

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
