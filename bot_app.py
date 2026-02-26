import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events

from filters.relevance import RelevanceFilter

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
GLOBAL_CONFIG_PATH = CONFIG_DIR / "global.json"
TENANTS_DIR = CONFIG_DIR / "tenants"


@dataclass
class DecisionResult:
    score: float
    decision: str


LABEL_CONTEXT: dict[str, dict[str, Any]] = {}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_global_config() -> dict[str, Any]:
    if not GLOBAL_CONFIG_PATH.exists():
        raise FileNotFoundError("Отсутствует config/global.json")
    cfg = _read_json(GLOBAL_CONFIG_PATH)
    token = cfg.get("bot_token", "")
    if token.startswith("${") and token.endswith("}"):
        cfg["bot_token"] = os.getenv(token[2:-1], "")
    return cfg


def load_tenants() -> dict[str, dict[str, Any]]:
    tenants: dict[str, dict[str, Any]] = {}
    if not TENANTS_DIR.exists():
        return tenants
    for path in TENANTS_DIR.glob("*.json"):
        tenant_cfg = _read_json(path)
        tenant_id = tenant_cfg["tenant_id"]
        tenants[tenant_id] = tenant_cfg
    return tenants


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def decision_from_score(score: float, threshold_alert: float, threshold_drop: float) -> str:
    if score >= threshold_alert:
        return "ALERT"
    if score <= threshold_drop:
        return "DROP"
    return "UNCERTAIN"


def evaluate_message(tenant_cfg: dict[str, Any], text: str, model_cache: dict[str, RelevanceFilter]) -> DecisionResult:
    context = tenant_cfg.get("context_filter", {})
    if not context.get("enabled", False):
        return DecisionResult(score=1.0, decision="ALERT")

    tenant_id = tenant_cfg["tenant_id"]
    model_path = context["model_path"]

    if tenant_id not in model_cache:
        rel_filter = RelevanceFilter(model_path=model_path)
        loaded = rel_filter.load()
        if not loaded:
            return DecisionResult(score=0.5, decision="UNCERTAIN")
        model_cache[tenant_id] = rel_filter

    score = model_cache[tenant_id].predict_score(text)
    decision = decision_from_score(score, context.get("threshold_alert", 0.45), context.get("threshold_drop", 0.15))
    return DecisionResult(score=score, decision=decision)


def save_candidate_if_needed(tenant_cfg: dict[str, Any], text: str, keyword: str, chat_id: int, message_id: int) -> None:
    context = tenant_cfg.get("context_filter", {})
    if context.get("collect_candidates"):
        tenant_id = tenant_cfg["tenant_id"]
        append_jsonl(
            BASE_DIR / "data" / tenant_id / "candidates.jsonl",
            {
                "tenant_id": tenant_id,
                "text": text,
                "label": None,
                "keyword": keyword,
                "chat_id": chat_id,
                "message_id": message_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": "telegram",
            },
        )


def append_dataset_entry(entry: dict[str, Any]) -> None:
    tenant_id = entry["tenant_id"]
    append_jsonl(BASE_DIR / "data" / tenant_id / "dataset.jsonl", entry)


async def handle_label_callback(event, token: str, label: int) -> bool:
    record = LABEL_CONTEXT.get(token)
    if not record:
        await event.answer("Запись не найдена или устарела", alert=True)
        return False

    append_dataset_entry(
        {
            "tenant_id": record["tenant_id"],
            "text": record["text"],
            "label": label,
            "keyword": record["keyword"],
            "chat_id": record["chat_id"],
            "message_id": record["message_id"],
            "is_forward": record["is_forward"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "telegram",
        }
    )
    await event.answer("Сохранено в датасет", alert=False)
    return True


async def main() -> None:
    global_cfg = load_global_config()
    tenants = load_tenants()
    if not tenants:
        raise RuntimeError("Не найдены tenant-конфиги в config/tenants")
    if not global_cfg.get("bot_token"):
        raise RuntimeError("Не задан BOT_TOKEN")

    bot_client = TelegramClient(global_cfg.get("session_name", "bot_session"), global_cfg["api_id"], global_cfg["api_hash"])
    await bot_client.start(bot_token=global_cfg["bot_token"])
    model_cache: dict[str, RelevanceFilter] = {}

    @bot_client.on(events.CallbackQuery)
    async def callback_handler(event):
        data = event.data.decode("utf-8")
        if data.startswith("lbl:"):
            _, token, label_raw = data.split(":")
            await handle_label_callback(event, token, int(label_raw))

    @bot_client.on(events.NewMessage())
    async def keyword_alert_handler(event):
        text = event.message.message or ""
        if not text:
            return

        for tenant_id, tenant_cfg in tenants.items():
            chats = {str(chat_id) for chat_id in tenant_cfg.get("chats", [])}
            if str(event.chat_id) not in chats:
                continue

            lower = text.lower()
            found_keyword = next((kw for kw in tenant_cfg.get("keywords", []) if kw.lower() in lower), None)
            if not found_keyword:
                continue

            result = evaluate_message(tenant_cfg, text, model_cache)
            if result.decision == "DROP":
                save_candidate_if_needed(tenant_cfg, text, found_keyword, event.chat_id, event.message.id)
                continue

            decision_line = f"Relevance score: {result.score:.2f} | Decision: {result.decision}"
            link = f"https://t.me/c/{str(event.chat_id)[4:]}/{event.message.id}" if str(event.chat_id).startswith("-100") else "(нет ссылки)"
            body = (
                f"🚨 Совпадение по tenant: {tenant_id}\n"
                f"Ключевое слово: {found_keyword}\n"
                f"Сообщение: {text[:700]}\n"
                f"{decision_line}\n"
                f"Ссылка: {link}"
            )

            token = uuid.uuid4().hex[:10]
            LABEL_CONTEXT[token] = {
                "tenant_id": tenant_id,
                "text": text,
                "keyword": found_keyword,
                "chat_id": event.chat_id,
                "message_id": event.message.id,
                "is_forward": bool(event.message.fwd_from),
            }
            buttons = [
                [
                    Button.inline("✅ Релевантно", f"lbl:{token}:1"),
                    Button.inline("❌ Нерелевантно", f"lbl:{token}:0"),
                ]
            ]

            for admin_id in tenant_cfg.get("admins", []):
                await bot_client.send_message(admin_id, body, buttons=buttons)

    print("Бот запущен", flush=True)
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
