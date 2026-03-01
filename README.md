# keyword-alert-bot_pro

Multi-tenant Telegram-бот для мониторинга ключевых слов, локальной ML-фильтрации релевантности и дообучения на ваших метках.

## 1) Конфигурация multi-tenant

### Глобальная конфигурация
Файл: `config/global.json`

```json
{
  "api_id": 123456,
  "api_hash": "your_api_hash",
  "session_name": "session",
  "session_string": "",
  "bot_token": "${BOT_TOKEN}",
  "default_tenant": "demo"
}
```

### Конфигурация тенанта
Файл: `config/tenants/<tenant_id>.json`, пример `config/tenants/demo.json`.

```json
{
  "tenant_id": "demo",
  "admins": [8333432274],
  "chats": [-10011111111, -10022222222],
  "chat_groups": {
    "project_main": [-10011111111, -10022222222]
  },
  "chat_labels": {
    "-10011111111": "основной",
    "-10022222222": "саппорт"
  },
  "keywords": ["vpn", "впн"],
  "context_filter": {
    "enabled": true,
    "model_path": "models/demo/relevance.joblib",
    "threshold_alert": 0.45,
    "threshold_drop": 0.15,
    "collect_candidates": true
  },
  "storage": {
    "collect_candidates": true,
    "candidates_sample_rate": 1.0,
    "candidates_max_mb": 20,
    "candidates_max_lines": 200000,
    "candidates_retention_days": 14,
    "candidates_dedupe_window_days": 7
  },
  "routing": {
    "alert_chat_id": -10044444444,
    "review_chat_id": -10055555555,
    "data_chat_id": -10066666666,
    "alert_thread_id": 123,
    "review_thread_id": 456,
    "data_thread_id": 789
  }
}
```

Ключевые моменты:
- `chats` + все ID из `chat_groups` участвуют в tenant-match.
- `chat_labels` используются в логах и алертах как метка под-чата.
- `routing`:
  - `alert_chat_id` — куда отправлять `ALERT`
  - `review_chat_id` — куда отправлять `UNCERTAIN`
  - `data_chat_id` — куда отправлять уведомление о сохранённом `DROP`-кандидате (опционально)
  - `alert_thread_id` / `review_thread_id` / `data_thread_id` — ID темы (topic), отправка через `reply_to`
- если `routing` не задан — отправка остаётся по `admins` (как раньше).

## 1.1) Привязка routing к темам через кнопки

- В личке бота откройте `/start` → `🧭 routing`.
- Нажмите `Bind ALERT` / `Bind UNCERTAIN` / `Bind DROP`.
- Перейдите в нужную тему супергруппы и отправьте `/bind`.
- Альтернатива: `/bind alert|review|data` прямо в теме.

Бот привяжет `chat_id` и (если найден) `thread_id`. Если команда выполнена вне темы, привязка делается к чату без `thread_id`.

## 2) Где хранятся данные и модель

Для каждого тенанта:
- `data/<tenant_id>/dataset.jsonl` — размеченный датасет
- `data/<tenant_id>/candidates.jsonl` — кандидаты из DROP
- `data/<tenant_id>/candidates_YYYY-MM-DD_HHMMSS.jsonl` — ротированные архивы кандидатов
- `models/<tenant_id>/relevance.joblib` — обученная модель
- `models/<tenant_id>/metadata.json` — метрики и дата обучения

## 3) Контроль роста candidates (sampling/dedupe/rotation)

Для `DROP`-кандидатов доступны ограничения через `storage`:
- `candidates_sample_rate` — случайная выборка 0..1 перед записью
- `candidates_dedupe_window_days` — дедупликация одинакового нормализованного текста по hash
- `candidates_max_mb` / `candidates_max_lines` — лимиты текущего файла
- при превышении лимита выполняется ротация в `candidates_*.jsonl`
- затем retention чистит старые архивы (`candidates_retention_days`)

## 4) Импорт датасета (TXT/DOCX)

```bash
python import_dataset.py --tenant demo --relevant relevant.docx --not_relevant not_relevant.txt
```

- TXT: 1 строка = 1 сообщение
- DOCX: берутся непустые абзацы
- если строка начинается с `текст:`, сохраняется только часть после префикса

## 5) Обучение ML-модели

```bash
python train_relevance_model.py --tenant demo
```

Что происходит:
- загрузка `dataset.jsonl`
- нормализация текста (lower, URL→`<URL>`, числа→`<NUM>`, нормализация пробелов)
- pipeline: TF-IDF (word + char ngrams) + LogisticRegression
- вывод метрик, включая **recall для label=1**
- сохранение модели и `metadata.json`

## 6) Запуск бота локально (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Заполните BOT_TOKEN в .env, а также config/global.json
python Keyword-alert.py
```

## 7) Telegram API конфиг (единый источник)

Рекомендуемый и основной источник: `config/global.json`.

```json
{
  "api_id": 123456,
  "api_hash": "0123456789abcdef0123456789abcdef",
  "user_session_string": "",
  "bot_token": "${BOT_TOKEN}"
}
```

Для auth-утилит также поддерживаются переменные окружения:
- `TG_API_ID`
- `TG_API_HASH`
- `TG_PHONE` (для app-кода)

Порядок загрузки во всех auth-скриптах: **ENV/.env → config/global.json → config.json (deprecated fallback)**.

## 8) Получить `user_session_string` через QR

```bash
python tools/auth_user_client_qr.py --timeout 300
```

Полезные опции:
- `--timeout 300` — время ожидания подтверждения QR (сек)
- `--loop` — при таймауте автоматически перевыпускает QR и ждёт снова

Если видите таймаут/не приходит подтверждение:
- обновите Telegram до актуальной версии,
- откройте Telegram на телефоне,
- сканируйте QR через **Settings → Devices**.

Скрипт выводит `qr.url`, а после успешного входа:

```text
USER_SESSION_STRING=<...>
```

## 9) Получить `user_session_string` через app-код

```bash
python tools/auth_user_client_code.py
```

Скрипт отправляет код один раз (`send_code_request`), просит app-код и при необходимости пароль 2FA,
после чего печатает:

```text
USER_SESSION_STRING=<...>
```

## 10) Куда вставлять строку

Вставьте значение в `config/global.json` в поле `user_session_string` и перезапустите процесс бота.
