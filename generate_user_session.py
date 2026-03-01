import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
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


async def main() -> None:
    default_api_id, default_api_hash = _load_defaults()

    api_id_raw = _prompt_with_default("API_ID", default_api_id)
    api_hash = _prompt_with_default("API_HASH", default_api_hash)

    if not api_id_raw or not api_hash:
        raise RuntimeError("API_ID и API_HASH обязательны")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("API_ID должен быть числом") from exc

    print("Логин user-аккаунта Telegram (bot_token не используется).", flush=True)
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()

    session_string = client.session.save()
    print("\nUSER_SESSION_STRING:")
    print(session_string)
    print("\nВставьте строку в config/global.json в поле user_session_string.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
