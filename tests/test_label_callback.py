import asyncio
import json

import bot_app


def test_button_click_appends_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_app, "BASE_DIR", tmp_path)

    token = "tok123"
    bot_app.LABEL_CONTEXT[token] = {
        "tenant_id": "demo",
        "text": "нужен впн, посоветуйте",
        "keyword": "впн",
        "chat_id": -100123,
        "message_id": 99,
        "is_forward": True,
    }

    ok = asyncio.run(bot_app.handle_label_callback(token, 1))

    assert ok is True

    dataset = tmp_path / "data" / "demo" / "dataset.jsonl"
    assert dataset.exists()
    row = json.loads(dataset.read_text(encoding="utf-8").strip())
    assert row["label"] == 1
    assert row["tenant_id"] == "demo"
    assert row["alert_id"] == token
