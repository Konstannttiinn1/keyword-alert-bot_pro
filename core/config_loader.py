import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
GLOBAL_CONFIG_PATH = BASE_DIR / "config" / "global.json"
LEGACY_CONFIG_PATH = BASE_DIR / "config.json"

HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
PHONE_RE = re.compile(r"^\+\d{8,15}$")


@dataclass
class TelegramCredentials:
    api_id: int
    api_hash: str
    phone: str | None


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _mask_hash(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def normalize_phone(raw: str) -> str:
    clean = re.sub(r"[\s\-()]", "", raw.strip())
    if clean and not clean.startswith("+"):
        clean = "+" + clean
    return clean


def _pick(name: str, env_value: str | None, global_cfg: dict, legacy_cfg: dict) -> tuple[str | None, str]:
    if env_value not in {None, ""}:
        return env_value, "env"
    if name in global_cfg and str(global_cfg.get(name, "")) != "":
        return str(global_cfg.get(name)), "config/global.json"
    if name in legacy_cfg and str(legacy_cfg.get(name, "")) != "":
        return str(legacy_cfg.get(name)), "config.json (deprecated fallback)"
    return None, "missing"


def load_telegram_credentials(require_phone: bool = False) -> TelegramCredentials:
    load_dotenv(BASE_DIR / ".env")

    global_cfg = _read_json(GLOBAL_CONFIG_PATH)
    legacy_cfg = {}
    if not global_cfg:
        legacy_cfg = _read_json(LEGACY_CONFIG_PATH)
        if legacy_cfg:
            print("DEPRECATED: config.json fallback is used because config/global.json is missing or invalid", flush=True)

    api_id_raw, api_id_src = _pick("api_id", os.getenv("TG_API_ID"), global_cfg, legacy_cfg)
    api_hash, api_hash_src = _pick("api_hash", os.getenv("TG_API_HASH"), global_cfg, legacy_cfg)

    phone_env = os.getenv("TG_PHONE")
    phone_cfg = global_cfg.get("tg_phone") or global_cfg.get("phone")
    phone_legacy = legacy_cfg.get("tg_phone") or legacy_cfg.get("phone")
    phone_raw, phone_src = _pick("tg_phone", phone_env, {"tg_phone": phone_cfg}, {"tg_phone": phone_legacy})

    if api_id_raw is None or api_hash is None:
        raise RuntimeError("Missing Telegram API credentials: set TG_API_ID/TG_API_HASH or api_id/api_hash in config/global.json")

    try:
        api_id = int(str(api_id_raw))
    except ValueError as exc:
        raise RuntimeError("api_id must be integer") from exc
    if api_id <= 0:
        raise RuntimeError("api_id must be > 0")

    api_hash = str(api_hash).strip()
    if not HEX32_RE.fullmatch(api_hash):
        raise RuntimeError("api_hash must be 32 hex characters")

    phone = normalize_phone(phone_raw) if phone_raw else None
    if require_phone:
        if not phone:
            raise RuntimeError("Phone is required: set TG_PHONE or tg_phone in config/global.json")
        if not PHONE_RE.fullmatch(phone):
            raise RuntimeError("phone must match ^\\+[0-9]{8,15}$")
    elif phone and not PHONE_RE.fullmatch(phone):
        raise RuntimeError("phone must match ^\\+[0-9]{8,15}$")

    print(
        f"[ConfigLoader] api_id={api_id} ({api_id_src}) | api_hash={_mask_hash(api_hash)} ({api_hash_src}) | phone={phone or '<not set>'} ({phone_src})",
        flush=True,
    )

    return TelegramCredentials(api_id=api_id, api_hash=api_hash, phone=phone)
