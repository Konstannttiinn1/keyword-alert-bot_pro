import bot_app


def test_apply_route_bind_with_thread_id():
    cfg = {"tenant_id": "demo", "routing": {}}
    out = bot_app.apply_route_bind(cfg, "alert", -100123, 456)
    assert out["routing"]["alert_chat_id"] == -100123
    assert out["routing"]["alert_thread_id"] == 456


def test_apply_route_bind_without_thread_id():
    cfg = {"tenant_id": "demo", "routing": {"review_thread_id": 999}}
    out = bot_app.apply_route_bind(cfg, "review", -100555, None)
    assert out["routing"]["review_chat_id"] == -100555
    assert "review_thread_id" not in out["routing"]


def test_clear_routing_removes_all_keys():
    cfg = {
        "tenant_id": "demo",
        "routing": {
            "alert_chat_id": -1,
            "review_chat_id": -2,
            "data_chat_id": -3,
            "alert_thread_id": 11,
            "review_thread_id": 22,
            "data_thread_id": 33,
        },
    }
    out = bot_app.clear_routing(cfg)
    for key in [
        "alert_chat_id",
        "review_chat_id",
        "data_chat_id",
        "alert_thread_id",
        "review_thread_id",
        "data_thread_id",
    ]:
        assert key not in out["routing"]
