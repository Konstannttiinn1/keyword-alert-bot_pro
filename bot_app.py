import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from filters.relevance import RelevanceFilter
from import_dataset import import_file_to_dataset
from train_relevance_model import train_for_tenant

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
GLOBAL_CONFIG_PATH = CONFIG_DIR / "global.json"
TENANTS_DIR = CONFIG_DIR / "tenants"

CONFIG_LOCK = asyncio.Lock()
LABEL_CONTEXT: dict[str, dict[str, Any]] = {}
ADMIN_STATE: dict[int, dict[str, Any]] = {}


@dataclass
class DecisionResult:
    score: float
    decision: str


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tf:
        json.dump(payload, tf, indent=2, ensure_ascii=False)
        tmp_name = tf.name
    os.replace(tmp_name, path)


def load_global_config() -> dict[str, Any]:
    if not GLOBAL_CONFIG_PATH.exists():
        raise FileNotFoundError("Отсутствует config/global.json")
    cfg = _read_json(GLOBAL_CONFIG_PATH)
    token = cfg.get("bot_token", "")
    if token.startswith("${") and token.endswith("}"):
        cfg["bot_token"] = os.getenv(token[2:-1], "")
    return cfg


def load_tenants_with_paths() -> dict[str, dict[str, Any]]:
    tenants: dict[str, dict[str, Any]] = {}
    if not TENANTS_DIR.exists():
        return tenants
    for path in TENANTS_DIR.glob("*.json"):
        tenant_cfg = _read_json(path)
        tenant_cfg["_config_path"] = str(path)
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


def resolve_tenant_for_admin(admin_id: int, tenants: dict[str, dict[str, Any]], default_tenant: str | None) -> str | None:
    for tenant_id, cfg in tenants.items():
        if admin_id in cfg.get("admins", []):
            if default_tenant and tenant_id == default_tenant:
                return tenant_id
    for tenant_id, cfg in tenants.items():
        if admin_id in cfg.get("admins", []):
            return tenant_id
    return None


def admin_menu_buttons(tenant_id: str):
    return [
        [Button.inline("➕ Добавить ключевое слово", f"adm:add_kw:{tenant_id}"), Button.inline("➖ Удалить ключевое слово", f"adm:del_kw:{tenant_id}")],
        [Button.inline("➕ Добавить чат", f"adm:add_chat:{tenant_id}"), Button.inline("➖ Удалить чат", f"adm:del_chat:{tenant_id}")],
        [Button.inline("📄 Показать настройки", f"adm:show:{tenant_id}")],
        [Button.inline("📥 Импорт датасета", f"adm:import:{tenant_id}"), Button.inline("🎓 Обучить модель", f"adm:train:{tenant_id}")],
    ]


def import_choice_buttons(tenant_id: str):
    return [
        [Button.inline("✅ Релевантные", f"adm:import_rel:{tenant_id}"), Button.inline("❌ Нерелевантные", f"adm:import_not:{tenant_id}")],
        [Button.inline("⬅️ Назад", f"adm:back:{tenant_id}")],
    ]


def format_settings(cfg: dict[str, Any]) -> str:
    context = cfg.get("context_filter", {})
    return (
        f"tenant_id: {cfg.get('tenant_id')}\n"
        f"admins: {cfg.get('admins', [])}\n"
        f"chats: {cfg.get('chats', [])}\n"
        f"keywords: {cfg.get('keywords', [])}\n"
        f"context_filter.enabled: {context.get('enabled')}\n"
        f"model_path: {context.get('model_path')}\n"
        f"threshold_alert: {context.get('threshold_alert')}\n"
        f"threshold_drop: {context.get('threshold_drop')}\n"
        f"collect_candidates: {context.get('collect_candidates')}"
    )


async def save_tenant_cfg(cfg: dict[str, Any]) -> None:
    async with CONFIG_LOCK:
        path = Path(cfg["_config_path"])
        serializable = {k: v for k, v in cfg.items() if k != "_config_path"}
        await asyncio.to_thread(_write_json_atomic, path, serializable)


async def main() -> None:
    global_cfg = load_global_config()
    if not global_cfg.get("bot_token"):
        raise RuntimeError("Не задан BOT_TOKEN")

    session_string = global_cfg.get("session_string", "")
    string_session = StringSession(session_string) if session_string else StringSession()
    bot_client = TelegramClient(string_session, global_cfg["api_id"], global_cfg["api_hash"])
    model_cache: dict[str, RelevanceFilter] = {}

    try:
        await bot_client.start(bot_token=global_cfg["bot_token"])
        if not session_string:
            print("Скопируйте session_string в config/global.json:")
            print(bot_client.session.save())

        @bot_client.on(events.CallbackQuery)
        async def callback_handler(event):
            sender = await event.get_sender()
            data = event.data.decode("utf-8")
            print(f"[CallbackQuery] user={sender.id} data={data}", flush=True)

            if data.startswith("lbl:"):
                _, token, label_raw = data.split(":")
                await handle_label_callback(event, token, int(label_raw))
                return

            tenants = load_tenants_with_paths()
            tenant_id = None
            if data.count(":") >= 2:
                tenant_id = data.split(":")[-1]
            if tenant_id and tenant_id in tenants:
                if sender.id not in tenants[tenant_id].get("admins", []):
                    await event.answer("Доступ запрещён", alert=True)
                    return

            if data.startswith("adm:back:"):
                await event.edit("Меню управления:", buttons=admin_menu_buttons(tenant_id))
                return

            if data.startswith("adm:add_kw:"):
                ADMIN_STATE[sender.id] = {"action": "await_keyword", "tenant_id": tenant_id}
                await event.respond("Отправьте ключевое слово")
                await event.answer()
                return

            if data.startswith("adm:del_kw:"):
                kws = tenants[tenant_id].get("keywords", [])
                if not kws:
                    await event.answer("Список пуст", alert=True)
                    return
                buttons = [[Button.inline(kw, f"adm:del_kw_do:{tenant_id}:{kw}")] for kw in kws]
                buttons.append([Button.inline("⬅️ Назад", f"adm:back:{tenant_id}")])
                await event.edit("Выберите ключевое слово для удаления", buttons=buttons)
                return

            if data.startswith("adm:del_kw_do:"):
                _, _, _, t_id, kw = data.split(":", 4)
                cfg = tenants[t_id]
                if kw in cfg.get("keywords", []):
                    cfg["keywords"] = [x for x in cfg.get("keywords", []) if x != kw]
                    await save_tenant_cfg(cfg)
                await event.edit(f"Удалено: {kw}", buttons=admin_menu_buttons(t_id))
                return

            if data.startswith("adm:add_chat:"):
                ADMIN_STATE[sender.id] = {"action": "await_chat", "tenant_id": tenant_id}
                await event.respond("Перешлите сообщение из чата или отправьте @username / chat_id")
                await event.answer()
                return

            if data.startswith("adm:del_chat:"):
                chat_ids = tenants[tenant_id].get("chats", [])
                if not chat_ids:
                    await event.answer("Список пуст", alert=True)
                    return
                buttons = [[Button.inline(str(chat_id), f"adm:del_chat_do:{tenant_id}:{chat_id}")] for chat_id in chat_ids]
                buttons.append([Button.inline("⬅️ Назад", f"adm:back:{tenant_id}")])
                await event.edit("Выберите чат для удаления", buttons=buttons)
                return

            if data.startswith("adm:del_chat_do:"):
                _, _, _, t_id, chat_raw = data.split(":", 4)
                cfg = tenants[t_id]
                cfg["chats"] = [c for c in cfg.get("chats", []) if str(c) != chat_raw]
                await save_tenant_cfg(cfg)
                await event.edit(f"Удалён чат: {chat_raw}", buttons=admin_menu_buttons(t_id))
                return

            if data.startswith("adm:show:"):
                await event.edit(format_settings(tenants[tenant_id]), buttons=[[Button.inline("⬅️ Назад", f"adm:back:{tenant_id}")]])
                return

            if data.startswith("adm:import:"):
                await event.edit("Выберите тип импортируемых примеров", buttons=import_choice_buttons(tenant_id))
                return

            if data.startswith("adm:import_rel:"):
                ADMIN_STATE[sender.id] = {"action": "await_import_file", "tenant_id": tenant_id, "label": 1}
                await event.respond("Загрузите .txt или .docx файл с релевантными примерами")
                await event.answer()
                return

            if data.startswith("adm:import_not:"):
                ADMIN_STATE[sender.id] = {"action": "await_import_file", "tenant_id": tenant_id, "label": 0}
                await event.respond("Загрузите .txt или .docx файл с нерелевантными примерами")
                await event.answer()
                return

            if data.startswith("adm:train:"):
                try:
                    result = await asyncio.to_thread(train_for_tenant, tenant_id)
                    await event.respond(
                        f"Обучение завершено\n"
                        f"Dataset size: {result['dataset_size']}\n"
                        f"Recall(label=1): {result['recall_label_1']:.4f}\n"
                        f"Model: {result['model_path']}\n"
                        f"Date: {result['date']}"
                    )
                except Exception as e:
                    await event.respond(f"Ошибка обучения: {e}")
                await event.answer()
                return

        @bot_client.on(events.NewMessage())
        async def new_message_handler(event):
            sender = await event.get_sender()
            chat_id = event.chat_id
            is_private = bool(event.is_private)
            tenants = load_tenants_with_paths()
            default_tenant = global_cfg.get("default_tenant")
            matched_tenant = resolve_tenant_for_admin(sender.id, tenants, default_tenant)
            print(f"[NewMessage] chat_id={chat_id} is_private={is_private} tenant_match={matched_tenant}", flush=True)

            if is_private and event.raw_text and event.raw_text.strip().startswith("/start"):
                if not matched_tenant:
                    await event.respond("Доступ запрещён")
                    return
                await event.respond("Меню управления:", buttons=admin_menu_buttons(matched_tenant))
                return

            state = ADMIN_STATE.get(sender.id)
            if is_private and state and matched_tenant:
                branch = state.get("action")
                print(f"[NewMessage] admin_branch={branch}", flush=True)
                tenant_id = state["tenant_id"]
                cfg = tenants.get(tenant_id)
                if not cfg or sender.id not in cfg.get("admins", []):
                    ADMIN_STATE.pop(sender.id, None)
                    await event.respond("Доступ запрещён")
                    return

                if state["action"] == "await_keyword":
                    kw = (event.raw_text or "").strip().lower()
                    if not kw:
                        await event.respond("Пустое ключевое слово")
                        return
                    if kw not in cfg.get("keywords", []):
                        cfg["keywords"] = cfg.get("keywords", []) + [kw]
                        await save_tenant_cfg(cfg)
                    ADMIN_STATE.pop(sender.id, None)
                    await event.respond(f"Добавлено: {kw}")
                    return

                if state["action"] == "await_chat":
                    raw = (event.raw_text or "").strip()
                    chat_id_to_add = None
                    try:
                        if event.message.fwd_from and event.message.forward and event.message.forward.chat:
                            chat_id_to_add = event.message.forward.chat.id
                        elif raw:
                            entity = await bot_client.get_entity(raw)
                            chat_id_to_add = entity.id
                    except Exception:
                        if raw and (raw.lstrip("-").isdigit()):
                            chat_id_to_add = int(raw)

                    if chat_id_to_add is None:
                        await event.respond("Не удалось определить chat_id")
                        return

                    if chat_id_to_add not in cfg.get("chats", []):
                        cfg["chats"] = cfg.get("chats", []) + [chat_id_to_add]
                        await save_tenant_cfg(cfg)
                    ADMIN_STATE.pop(sender.id, None)
                    await event.respond(f"Чат добавлен: {chat_id_to_add}")
                    return

                if state["action"] == "await_import_file":
                    if not event.file or not event.file.name:
                        await event.respond("Пришлите файл .txt или .docx как документ")
                        return
                    suffix = Path(event.file.name).suffix.lower()
                    if suffix not in {".txt", ".docx"}:
                        await event.respond("Поддерживаются только .txt и .docx")
                        return

                    with tempfile.TemporaryDirectory() as td:
                        dst = Path(td) / event.file.name
                        await event.download_media(file=str(dst))
                        count = await asyncio.to_thread(import_file_to_dataset, tenant_id, dst, state["label"])

                    ADMIN_STATE.pop(sender.id, None)
                    await event.respond(f"Импортировано примеров: {count}")
                    return

            # Текущая логика алертов (не изменяем по смыслу)
            text = event.message.message or ""
            if not text:
                return

            for tenant_id, tenant_cfg in tenants.items():
                chats = {str(chat) for chat in tenant_cfg.get("chats", [])}
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
                buttons = [[Button.inline("✅ Релевантно", f"lbl:{token}:1"), Button.inline("❌ Нерелевантно", f"lbl:{token}:0")]]

                for admin_id in tenant_cfg.get("admins", []):
                    await bot_client.send_message(admin_id, body, buttons=buttons)

        print("Бот запущен", flush=True)
        await bot_client.run_until_disconnected()
    finally:
        await bot_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановка бота", flush=True)
