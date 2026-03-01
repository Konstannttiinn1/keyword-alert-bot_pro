import asyncio
import json
import os
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from getpass import getpass

from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _normalize_phone(raw: str) -> str:
    clean = raw.strip().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    if clean and not clean.startswith("+"):
        clean = "+" + clean
    return clean


def _save_user_session_string(global_cfg: dict[str, Any], global_config_path: Path, session_string: str) -> None:
    global_cfg["user_session_string"] = session_string
    _write_json_atomic(global_config_path, global_cfg)


def _print_auth_menu() -> None:
    print("User client не авторизован.", flush=True)
    print("Выберите способ авторизации:", flush=True)
    print("1 — QR login (рекомендуется)", flush=True)
    print("2 — Login по коду (через номер телефона)", flush=True)
    print("0 — Запустить в degraded режиме", flush=True)
    print("Введите номер варианта:", flush=True)


def _save_qr_png(url: str, out_path: Path) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        print("Пакет qrcode не установлен. Установите: pip install qrcode[pil]", flush=True)
        print(f"Используйте URL для генерации QR вручную: {url}", flush=True)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(url)
    img.save(out_path)
    print(f"QR сохранён: {out_path}", flush=True)
    if hasattr(os, "startfile"):
        try:
            os.startfile(str(out_path))  # type: ignore[attr-defined]
        except OSError:
            pass


def _print_ascii_qr(url: str) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        return

    try:
        qr_obj = qrcode.QRCode(border=1)
        qr_obj.add_data(url)
        qr_obj.make(fit=True)
        print("ASCII QR:", flush=True)
        qr_obj.print_ascii(invert=True)
    except Exception:
        return


async def _authorize_via_qr(api_id: int, api_hash: str, timeout: int = 600) -> str | None:
    auth_client = TelegramClient(StringSession(), api_id, api_hash)
    out_path = Path.cwd() / "data" / "auth" / "qr_login.png"
    try:
        await auth_client.connect()
        while True:
            qr = await auth_client.qr_login()
            print("QR login URL:", flush=True)
            print(qr.url, flush=True)
            _save_qr_png(qr.url, out_path)
            _print_ascii_qr(qr.url)
            print("Откройте Telegram на телефоне:", flush=True)
            print("Настройки → Устройства → Сканировать QR", flush=True)
            try:
                await qr.wait(timeout=timeout)
                return auth_client.session.save()
            except SessionPasswordNeededError:
                print("Для завершения QR входа требуется пароль 2FA.", flush=True)
                for attempt in range(1, 4):
                    password = getpass("Введите пароль 2FA: ")
                    try:
                        await auth_client.sign_in(password=password)
                        return auth_client.session.save()
                    except PasswordHashInvalidError:
                        print(f"Неверный пароль 2FA (попытка {attempt}/3)", flush=True)
                    except Exception as exc:
                        print(f"Ошибка 2FA: {type(exc).__name__}: {exc}", flush=True)
                        return None
                print("Достигнут лимит попыток пароля 2FA.", flush=True)
                return None
            except asyncio.TimeoutError:
                print("QR токен истёк. Пересоздаю QR...", flush=True)
                continue
    except Exception as exc:
        print(f"Ошибка QR авторизации: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        await auth_client.disconnect()


async def _authorize_via_code(api_id: int, api_hash: str, phone_from_cfg: str | None) -> str | None:
    auth_client = TelegramClient(StringSession(), api_id, api_hash)

    default_phone = _normalize_phone(phone_from_cfg) if phone_from_cfg else ""
    prompt = "Введите номер телефона (+7...)"
    if default_phone:
        prompt += f" [Enter = {default_phone}]"
    prompt += ": "

    raw_user_input = input(prompt).strip()
    user_phone = _normalize_phone(raw_user_input) if raw_user_input else ""

    if user_phone:
        phone = user_phone
        source = "user_input"
        if default_phone and user_phone != default_phone:
            print("TG_PHONE игнорируется, используем введённый номер", flush=True)
    else:
        phone = default_phone
        source = "env_default"

    if not phone:
        print("Телефон не задан. Отмена авторизации.", flush=True)
        return None

    print(f"Using phone={phone} source={source}", flush=True)

    try:
        await auth_client.connect()
        sent = await auth_client.send_code_request(phone)
        code = input("Введите код из Telegram: ").strip()
        try:
            await auth_client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            for attempt in range(1, 4):
                password = getpass("Введите пароль 2FA: ")
                try:
                    await auth_client.sign_in(password=password)
                    break
                except PasswordHashInvalidError:
                    print(f"Неверный пароль 2FA (попытка {attempt}/3)", flush=True)
                    if attempt == 3:
                        print("Достигнут лимит попыток пароля 2FA.", flush=True)
                        return None
        return auth_client.session.save()
    except Exception as exc:
        print(f"Ошибка авторизации по коду: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        await auth_client.disconnect()


async def ensure_user_authorized(api_id: int, api_hash: str, global_cfg: dict[str, Any], global_config_path: Path) -> str | None:
    while True:
        _print_auth_menu()
        choice = input().strip()

        if choice == "0":
            return None

        if choice == "1":
            session_string = await _authorize_via_qr(api_id, api_hash, timeout=600)
        elif choice == "2":
            session_string = await _authorize_via_code(api_id, api_hash, global_cfg.get("tg_phone") or global_cfg.get("phone"))
        else:
            print("Неизвестный вариант. Попробуйте снова.", flush=True)
            continue

        if not session_string:
            print("Авторизация не завершена. Выберите вариант снова.", flush=True)
            continue

        _save_user_session_string(global_cfg, global_config_path, session_string)
        print("Авторизация успешна, user_session_string сохранён", flush=True)
        return session_string
