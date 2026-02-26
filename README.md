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

Там задаются:
- `admins` — кто получает алерты
- `chats` — какие чаты мониторить
- `keywords` — триггер-слова
- `context_filter` — путь к модели и пороги

## 2) Где хранятся данные и модель

Для каждого тенанта:
- `data/<tenant_id>/dataset.jsonl` — размеченный датасет
- `data/<tenant_id>/candidates.jsonl` — кандидаты из DROP
- `models/<tenant_id>/relevance.joblib` — обученная модель
- `models/<tenant_id>/metadata.json` — метрики и дата обучения

## 3) Импорт датасета (TXT/DOCX)

```bash
python import_dataset.py --tenant demo --relevant relevant.docx --not_relevant not_relevant.txt
```

- TXT: 1 строка = 1 сообщение
- DOCX: берутся непустые абзацы
- если строка начинается с `текст:`, сохраняется только часть после префикса

## 4) Обучение ML-модели

```bash
python train_relevance_model.py --tenant demo
```

Что происходит:
- загрузка `dataset.jsonl`
- нормализация текста (lower, URL→`<URL>`, числа→`<NUM>`, нормализация пробелов)
- pipeline: TF-IDF (word + char ngrams) + LogisticRegression
- вывод метрик, включая **recall для label=1**
- сохранение модели и `metadata.json`

## 5) Запуск бота локально (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Заполните BOT_TOKEN в .env, а также config/global.json
python Keyword-alert.py
```

## 6) Логика релевантности (high recall)

После keyword match:
- `score >= threshold_alert` → ALERT
- `score <= threshold_drop` → DROP (не отправляем, можно складывать в `candidates.jsonl`)
- иначе → UNCERTAIN (всё равно отправляем)

В алерте всегда есть:
`Relevance score: 0.xx | Decision: ALERT/UNCERTAIN`

## 7) Inline-разметка в Telegram

Под алертом есть кнопки:
- `✅ Релевантно`
- `❌ Нерелевантно`

По клику запись добавляется в `data/<tenant_id>/dataset.jsonl`, и бот отвечает: `Сохранено в датасет`.
