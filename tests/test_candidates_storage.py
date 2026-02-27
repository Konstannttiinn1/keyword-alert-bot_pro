import json

import bot_app


def _tenant_cfg(storage: dict):
    return {
        "tenant_id": "demo",
        "storage": storage,
        "context_filter": {"collect_candidates": True},
    }


def test_candidates_rotation_by_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(bot_app.random, "random", lambda: 0.0)

    cfg = _tenant_cfg(
        {
            "collect_candidates": True,
            "candidates_sample_rate": 1.0,
            "candidates_max_mb": 100,
            "candidates_max_lines": 1,
            "candidates_retention_days": 14,
            "candidates_dedupe_window_days": 0,
        }
    )

    assert bot_app.save_candidate_if_needed(cfg, "text one", "впн", -1001, 1) is True
    assert bot_app.save_candidate_if_needed(cfg, "text two", "впн", -1001, 2) is True

    data_dir = tmp_path / "data" / "demo"
    rotated = list(data_dir.glob("candidates_*.jsonl"))
    assert rotated, "Ожидали ротированный файл"

    current = data_dir / "candidates.jsonl"
    lines = current.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["text"] == "text two"


def test_candidates_sampling_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(bot_app.random, "random", lambda: 0.0)

    cfg = _tenant_cfg(
        {
            "collect_candidates": True,
            "candidates_sample_rate": 1.0,
            "candidates_max_mb": 100,
            "candidates_max_lines": 1000,
            "candidates_retention_days": 14,
            "candidates_dedupe_window_days": 7,
        }
    )

    first = bot_app.save_candidate_if_needed(cfg, "Нужен VPN 123 https://x.com", "впн", -1001, 1)
    second = bot_app.save_candidate_if_needed(cfg, "нужен vpn 777 https://x.com", "впн", -1001, 2)

    assert first is True
    assert second is False

    path = tmp_path / "data" / "demo" / "candidates.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
