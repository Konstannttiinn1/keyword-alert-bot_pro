import asyncio
import hashlib
import json
import os
import random
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from filters.relevance import RelevanceFilter, normalize_text
from import_dataset import import_file_to_dataset
from train_relevance_model import train_for_tenant

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
GLOBAL_CONFIG_PATH = CONFIG_DIR / "global.json"
TENANTS_DIR = CONFIG_DIR / "tenants"

CONFIG_LOCK = asyncio.Lock()
LABEL_CONTEXT: dict[str, dict[str, Any]] = {}
ADMIN_STATE: dict[int, dict[str, Any]] = {}
SAVED_ALERT_IDS: set[str] = set()


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


def _to_int_chat_id(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def get_tenant_chat_ids(tenant_cfg: dict[str, Any]) -> set[int]:
    chat_ids: set[int] = set()
    for raw in tenant_cfg.get("chats", []):
        chat_id = _to_int_chat_id(raw)
        if chat_id is not None:
            chat_ids.add(chat_id)

    for _, group_chat_ids in tenant_cfg.get("chat_groups", {}).items():
        for raw in group_chat_ids:
            chat_id = _to_int_chat_id(raw)
            if chat_id is not None:
                chat_ids.add(chat_id)
    return chat_ids


def get_chat_label(tenant_cfg: dict[str, Any], chat_id: int) -> str | None:
    labels = tenant_cfg.get("chat_labels", {})
    return labels.get(str(chat_id)) or labels.get(chat_id)


def _storage_cfg(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    context_filter = tenant_cfg.get("context_filter", {})
    storage = tenant_cfg.get("storage", {})
    return {
        "collect_candidates": storage.get("collect_candidates", context_filter.get("collect_candidates", False)),
        "candidates_sample_rate": float(storage.get("candidates_sample_rate", 1.0)),
        "candidates_max_mb": int(storage.get("candidates_max_mb", 20)),
        "candidates_max_lines": int(storage.get("candidates_max_lines", 200000)),
        "candidates_retention_days": int(storage.get("candidates_retention_days", 14)),
        "candidates_dedupe_window_days": int(storage.get("candidates_dedupe_window_days", 7)),
    }


def _candidate_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _iter_candidate_files(tenant_id: str) -> list[Path]:
    data_dir = BASE_DIR / "data" / tenant_id
    current = data_dir / "candidates.jsonl"
    rotated = sorted(data_dir.glob("candidates_*.jsonl"), key=lambda p: p.stat().st_mtime)
    return [*rotated, current]


def _is_candidate_duplicate(tenant_id: str, dedupe_hash: str, window_days: int) -> bool:
    if window_days <= 0:
        return False
    threshold = datetime.now(timezone.utc) - timedelta(days=window_days)

    for path in _iter_candidate_files(tenant_id):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("hash") != dedupe_hash:
                    continue
                ts_raw = row.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts >= threshold:
                    return True
    return False


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _rotate_candidates_if_needed(tenant_id: str, max_mb: int, max_lines: int) -> None:
    candidates_path = BASE_DIR / "data" / tenant_id / "candidates.jsonl"
    if not candidates_path.exists():
        return

    over_mb = max_mb > 0 and (candidates_path.stat().st_size / (1024 * 1024)) >= max_mb
    over_lines = max_lines > 0 and _count_lines(candidates_path) >= max_lines
    if not (over_mb or over_lines):
        return

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rotated_path = candidates_path.with_name(f"candidates_{ts}.jsonl")
    os.replace(candidates_path, rotated_path)


def _cleanup_candidate_archives(tenant_id: str, retention_days: int) -> None:
    if retention_days <= 0:
        return
    data_dir = BASE_DIR / "data" / tenant_id
    archives = sorted(data_dir.glob("candidates_*.jsonl"), key=lambda p: p.stat().st_mtime)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    for path in archives:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            path.unlink(missing_ok=True)

    archives = sorted(data_dir.glob("candidates_*.jsonl"), key=lambda p: p.stat().st_mtime)
    max_files = max(1, retention_days * 4)
    while len(archives) > max_files:
        oldest = archives.pop(0)
        oldest.unlink(missing_ok=True)


def save_candidate_if_needed(tenant_cfg: dict[str, Any], text: str, keyword: str, chat_id: int, message_id: int) -> bool:
    storage = _storage_cfg(tenant_cfg)
    if not storage["collect_candidates"]:
        return False

    sample_rate = min(1.0, max(0.0, storage["candidates_sample_rate"]))
    if random.random() >= sample_rate:
        return False

    tenant_id = tenant_cfg["tenant_id"]
    dedupe_hash = _candidate_hash(text)
    if _is_candidate_duplicate(tenant_id, dedupe_hash, storage["candidates_dedupe_window_days"]):
        return False

    _rotate_candidates_if_needed(tenant_id, storage["candidates_max_mb"], storage["candidates_max_lines"])
    append_jsonl(
        BASE_DIR / "data" / tenant_id / "candidates.jsonl",
        {
            "tenant_id": tenant_id,
            "text": text,
            "label": None,
            "keyword": keyword,
            "chat_id": chat_id,
            "message_id": message_id,
            "hash": dedupe_hash,
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "telegram",
        },
    )
    _cleanup_candidate_archives(tenant_id, storage["candidates_retention_days"])
    return True


def append_dataset_entry(entry: dict[str, Any]) -> None:
    tenant_id = entry["tenant_id"]
    append_jsonl(BASE_DIR / "data" / tenant_id / "dataset.jsonl", entry)


def is_alert_saved(tenant_id: str, alert_id: str) -> bool:
    if alert_id in SAVED_ALERT_IDS:
        return True

    dataset_path = BASE_DIR / "data" / tenant_id / "dataset.jsonl"
    if not dataset_path.exists():
        return False

    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("alert_id") == alert_id:
                SAVED_ALERT_IDS.add(alert_id)
                return True
    return False


async def handle_label_callback(token: str, label: int) -> bool:
    record = LABEL_CONTEXT.get(token)
    if not record:
        return False

    if is_alert_saved(record["tenant_id"], token):
        return False

    append_dataset_entry(
        {
            "tenant_id": record["tenant_id"],
            "alert_id": token,
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
    SAVED_ALERT_IDS.add(token)
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
        [Button.inline("🧭 routing", f"adm:routing:{tenant_id}")],
        [Button.inline("📥 Импорт датасета", f"adm:import:{tenant_id}"), Button.inline("🎓 Обучить модель", f"adm:train:{tenant_id}")],
    ]


def import_choice_buttons(tenant_id: str):
    return [
        [Button.inline("✅ Релевантные", f"adm:import_rel:{tenant_id}"), Button.inline("❌ Нерелевантные", f"adm:import_not:{tenant_id}")],
        [Button.inline("⬅️ Назад", f"adm:back:{tenant_id}")],
    ]


def routing_menu_buttons(tenant_id: str):
    return [
        [Button.inline("✅ Bind ALERT", f"adm:bind_route:{tenant_id}:alert")],
        [Button.inline("❓ Bind UNCERTAIN", f"adm:bind_route:{tenant_id}:review")],
        [Button.inline("🧊 Bind DROP", f"adm:bind_route:{tenant_id}:data")],
        [Button.inline("🧹 Clear routing", f"adm:clear_routing:{tenant_id}")],
        [Button.inline("⬅️ Back", f"adm:back:{tenant_id}")],
    ]


def apply_route_bind(cfg: dict[str, Any], kind: str, chat_id: int, thread_id: int | None) -> dict[str, Any]:
    route_map = {
        "alert": ("alert_chat_id", "alert_thread_id"),
        "review": ("review_chat_id", "review_thread_id"),
        "data": ("data_chat_id", "data_thread_id"),
    }
    if kind not in route_map:
        raise ValueError("Неверный тип маршрута")

    chat_key, thread_key = route_map[kind]
    cfg.setdefault("routing", {})
    cfg["routing"][chat_key] = int(chat_id)
    if thread_id is None:
        cfg["routing"].pop(thread_key, None)
    else:
        cfg["routing"][thread_key] = int(thread_id)
    return cfg


def clear_routing(cfg: dict[str, Any]) -> dict[str, Any]:
    routing = cfg.setdefault("routing", {})
    for key in [
        "alert_chat_id",
        "review_chat_id",
        "data_chat_id",
        "alert_thread_id",
        "review_thread_id",
        "data_thread_id",
    ]:
        routing.pop(key, None)
    return cfg


def extract_thread_id(message: Any) -> int | None:
    top_direct = getattr(message, "reply_to_top_id", None)
    if top_direct:
        return int(top_direct)

    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None:
        top_id = getattr(reply_to, "reply_to_top_id", None)
        if top_id:
            return int(top_id)

        reply_msg_id = getattr(reply_to, "reply_to_msg_id", None)
        if reply_msg_id:
            return int(reply_msg_id)

    return None


async def send_with_routing(bot_client: TelegramClient, routing: dict[str, Any], kind: str, text: str, buttons=None) -> bool:
    chat_key = f"{kind}_chat_id"
    thread_key = f"{kind}_thread_id"
    chat_id = routing.get(chat_key)
    if not chat_id:
        return False

    thread_id = routing.get(thread_key)
    decision_kind = {"alert": "ALERT", "review": "UNCERTAIN", "data": "DROP"}.get(kind, kind.upper())
    print(
        f"[Notify] kind={decision_kind} target_chat_id={chat_id} thread_id={thread_id} (via bot_client)",
        flush=True,
    )
    if thread_id:
        await bot_client.send_message(int(chat_id), text, buttons=buttons, reply_to=int(thread_id))
    else:
        await bot_client.send_message(int(chat_id), text, buttons=buttons)
    return True


def format_settings(cfg: dict[str, Any]) -> str:
    context = cfg.get("context_filter", {})
    storage = _storage_cfg(cfg)
    routing = cfg.get("routing", {})
    return (
        f"tenant_id: {cfg.get('tenant_id')}\n"
        f"admins: {cfg.get('admins', [])}\n"
        f"chats: {cfg.get('chats', [])}\n"
        f"chat_groups: {cfg.get('chat_groups', {})}\n"
        f"chat_labels: {cfg.get('chat_labels', {})}\n"
        f"keywords: {cfg.get('keywords', [])}\n"
        f"context_filter.enabled: {context.get('enabled')}\n"
        f"model_path: {context.get('model_path')}\n"
        f"threshold_alert: {context.get('threshold_alert')}\n"
        f"threshold_drop: {context.get('threshold_drop')}\n"
        f"collect_candidates: {storage.get('collect_candidates')}\n"
        f"candidates_sample_rate: {storage.get('candidates_sample_rate')}\n"
        f"candidates_max_mb: {storage.get('candidates_max_mb')}\n"
        f"candidates_max_lines: {storage.get('candidates_max_lines')}\n"
        f"candidates_retention_days: {storage.get('candidates_retention_days')}\n"
        f"candidates_dedupe_window_days: {storage.get('candidates_dedupe_window_days')}\n"
        f"routing.alert_chat_id: {routing.get('alert_chat_id')}\n"
        f"routing.alert_thread_id: {routing.get('alert_thread_id')}\n"
        f"routing.review_chat_id: {routing.get('review_chat_id')}\n"
        f"routing.review_thread_id: {routing.get('review_thread_id')}\n"
        f"routing.data_chat_id: {routing.get('data_chat_id')}\n"
        f"routing.data_thread_id: {routing.get('data_thread_id')}"
    )


async def save_tenant_cfg(cfg: dict[str, Any]) -> None:
    async with CONFIG_LOCK:
        path = Path(cfg["_config_path"])
        serializable = {k: v for k, v in cfg.items() if k != "_config_path"}
        await asyncio.to_thread(_write_json_atomic, path, serializable)


async def main() -> None:
    global_cfg = load_global_config()
    if not global_cfg.get("api_id") or not global_cfg.get("api_hash"):
        raise RuntimeError("Не заданы api_id/api_hash")
    if not global_cfg.get("bot_token"):
        raise RuntimeError("Не задан bot_token")

    session_string = global_cfg.get("session_string", "")
    session_name = global_cfg.get("session_name", "user_session")
    user_session = StringSession(session_string) if session_string else session_name

    user_client = TelegramClient(user_session, global_cfg["api_id"], global_cfg["api_hash"])
    bot_client = TelegramClient("bot_session", global_cfg["api_id"], global_cfg["api_hash"])
    model_cache: dict[str, RelevanceFilter] = {}

    try:
        await user_client.start()
        await bot_client.start(bot_token=global_cfg["bot_token"])
        if not session_string:
            print("Скопируйте session_string в config/global.json:")
            print(user_client.session.save())

        @bot_client.on(events.CallbackQuery)
        async def callback_handler(event):
            sender = await event.get_sender()
            data = event.data.decode("utf-8")
            print(f"[CallbackQuery] user={sender.id} data={data}", flush=True)

            if data.startswith("lbl:"):
                _, token, label_raw = data.split(":")
                record = LABEL_CONTEXT.get(token)
                if not record:
                    await event.answer("Уже сохранено", alert=False)
                    return

                if is_alert_saved(record["tenant_id"], token):
                    await event.answer("Уже сохранено", alert=False)
                    return

                await event.answer("Сохранено", alert=False)
                await handle_label_callback(token, int(label_raw))
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

            if data.startswith("adm:routing:"):
                await event.edit("Настройка routing:", buttons=routing_menu_buttons(tenant_id))
                return

            if data.startswith("adm:bind_route:"):
                parts = data.split(":")
                if len(parts) != 4:
                    await event.answer("Некорректный формат", alert=True)
                    return
                t_id = parts[2]
                route_kind = parts[3]
                ADMIN_STATE[sender.id] = {
                    "action": "await_bind_route",
                    "tenant_id": t_id,
                    "route_kind": route_kind,
                }
                await event.respond("Перейди в нужную тему и отправь /bind")
                await event.answer()
                return

            if data.startswith("adm:clear_routing:"):
                cfg = tenants[tenant_id]
                clear_routing(cfg)
                await save_tenant_cfg(cfg)
                await event.edit("Routing очищен", buttons=routing_menu_buttons(tenant_id))
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
        async def admin_message_handler(event):
            sender = await event.get_sender()
            chat_id = event.chat_id
            is_private = bool(event.is_private)
            tenants = load_tenants_with_paths()
            default_tenant = global_cfg.get("default_tenant")
            matched_tenant = resolve_tenant_for_admin(sender.id, tenants, default_tenant)
            print(f"[NewMessage] chat_id={chat_id} is_private={is_private} tenant_match={matched_tenant} (via bot_client)", flush=True)

            if is_private and event.raw_text and event.raw_text.strip().startswith("/start"):
                if not matched_tenant:
                    await event.respond("Доступ запрещён")
                    return
                await event.respond("Меню управления:", buttons=admin_menu_buttons(matched_tenant))
                return

            if event.raw_text and event.raw_text.strip().startswith("/bind"):
                parts = event.raw_text.strip().split()
                state = ADMIN_STATE.get(sender.id)
                route_kind = None
                tenant_id_for_bind = None

                if len(parts) > 1:
                    kind_raw = parts[1].lower()
                    route_kind = {"alert": "alert", "review": "review", "uncertain": "review", "data": "data", "drop": "data"}.get(kind_raw)
                    tenant_id_for_bind = matched_tenant
                elif state and state.get("action") == "await_bind_route":
                    route_kind = state.get("route_kind")
                    tenant_id_for_bind = state.get("tenant_id")

                if route_kind not in {"alert", "review", "data"} or not tenant_id_for_bind:
                    await event.respond("Не удалось определить маршрут. Используйте /bind alert|review|data или кнопку Bind.")
                    return

                cfg = tenants.get(tenant_id_for_bind)
                if not cfg or sender.id not in cfg.get("admins", []):
                    await event.respond("Доступ запрещён")
                    return

                thread_id = extract_thread_id(event.message)
                if thread_id is None:
                    reply_to = getattr(event.message, "reply_to", None)
                    print(
                        "[BindDebug] reply_to_top_id="
                        f"{getattr(event.message, 'reply_to_top_id', None)} "
                        f"has_reply_to={reply_to is not None} "
                        f"reply_to.reply_to_top_id={getattr(reply_to, 'reply_to_top_id', None) if reply_to else None} "
                        f"reply_to.reply_to_msg_id={getattr(reply_to, 'reply_to_msg_id', None) if reply_to else None}",
                        flush=True,
                    )
                apply_route_bind(cfg, route_kind, int(event.chat_id), thread_id)
                await save_tenant_cfg(cfg)
                if state and state.get("action") == "await_bind_route":
                    ADMIN_STATE.pop(sender.id, None)

                if thread_id is None:
                    await event.respond(
                        f"Маршрут {route_kind} привязан: chat_id={event.chat_id}. thread_id не найден, привязал в чат без темы."
                    )
                else:
                    await event.respond(f"Маршрут {route_kind} привязан: chat_id={event.chat_id}, thread_id={thread_id}")
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

        @user_client.on(events.NewMessage())
        async def source_message_handler(event):
            text = event.message.message or ""
            if not text:
                return

            tenants = load_tenants_with_paths()
            matched_tenants = [tenant_id for tenant_id, tenant_cfg in tenants.items() if str(event.chat_id) in {str(chat) for chat in get_tenant_chat_ids(tenant_cfg)}]
            tenant_match = ",".join(matched_tenants) if matched_tenants else "None"
            print(f"[SourceMessage] chat_id={event.chat_id} tenant_match={tenant_match} (via user_client)", flush=True)

            for tenant_id in matched_tenants:
                tenant_cfg = tenants[tenant_id]
                source_label = get_chat_label(tenant_cfg, int(event.chat_id))

                lower = text.lower()
                tenant_keywords = tenant_cfg.get("keywords", [])
                print(
                    f"[Pipeline] tenant={tenant_id} source_chat_id={event.chat_id} source_label={source_label} text='{text[:120]}' keywords={tenant_keywords}",
                    flush=True,
                )
                found_keyword = next((kw for kw in tenant_keywords if kw.lower() in lower), None)
                if not found_keyword:
                    print(f"[Pipeline] tenant={tenant_id} keyword_match=нет | skip: no keyword match", flush=True)
                    continue

                print(f"[Pipeline] tenant={tenant_id} match keyword={found_keyword}", flush=True)

                result = evaluate_message(tenant_cfg, text, model_cache)
                print(f"[Pipeline] tenant={tenant_id} decision={result.decision}", flush=True)
                if result.decision == "DROP":
                    print(f"[Pipeline] tenant={tenant_id} drop chat_id={event.chat_id}", flush=True)
                    was_saved = save_candidate_if_needed(tenant_cfg, text, found_keyword, event.chat_id, event.message.id)
                    routing = tenant_cfg.get("routing", {})
                    if was_saved:
                        await send_with_routing(
                            bot_client,
                            routing,
                            "data",
                            (
                                f"🗂 DROP candidate | tenant={tenant_id}\n"
                                f"source_chat_id={event.chat_id}\n"
                                f"source_label={source_label}\n"
                                f"keyword={found_keyword}\n"
                                f"text={text[:500]}"
                            ),
                        )
                    continue

                decision_line = f"Relevance score: {result.score:.2f} | Decision: {result.decision}"
                link = f"https://t.me/c/{str(event.chat_id)[4:]}/{event.message.id}" if str(event.chat_id).startswith("-100") else "(нет ссылки)"
                body = (
                    f"🚨 Совпадение по tenant: {tenant_id}\n"
                    f"Источник: chat_id={event.chat_id} label={source_label}\n"
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

                routing = tenant_cfg.get("routing", {})
                route_kind = "alert" if result.decision == "ALERT" else "review"
                sent_by_routing = await send_with_routing(bot_client, routing, route_kind, body, buttons=buttons)

                if sent_by_routing:
                    target_chat_id = routing.get(f"{route_kind}_chat_id")
                    print(
                        f"[Pipeline] tenant={tenant_id} sent decision={result.decision} target_chat_id={target_chat_id} source_chat_id={event.chat_id}",
                        flush=True,
                    )
                else:
                    for admin_id in tenant_cfg.get("admins", []):
                        await bot_client.send_message(admin_id, body, buttons=buttons)
                        print(
                            f"[Pipeline] tenant={tenant_id} sent decision={result.decision} admin_id={admin_id} source_chat_id={event.chat_id}",
                            flush=True,
                        )
        print("Бот запущен (user_client + bot_client)", flush=True)
        await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())
    finally:
        await user_client.disconnect()
        await bot_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановка бота", flush=True)
