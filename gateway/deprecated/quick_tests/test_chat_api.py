"""The gateway's HTTP API: settings, chat streaming, saving conversations, cache and router hooks."""

from gateway.catalog import load_catalog
from gateway.schemas import RunSettings
from gateway.service import fit_params
from quick_tests import fake_router
from quick_tests.helpers import send_chat

FAKE_ROUTER = "quick_tests.fake_router:FakeRouter"
MEMORY_CACHE = "gateway.plugins.cache:MemoryCache"


def answer_text(events):
    """The full answer: all streamed text pieces joined together."""
    return "".join(d["text"] for e, d in events if e == "text")


def model_event(events):
    """The 'model' event: which model answered, who chose it and with which params."""
    return next(d for e, d in events if e == "model")


def final_steps(events):
    """The last state of every step the UI shows (cache, router, model, ...), by step id."""
    out = {s["id"]: s for s in next(d for e, d in events if e == "meta")["steps"]}
    for e, d in events:
        if e == "step":
            out[d["id"]] = d
    return out


def test_providers_without_api_key_are_marked_not_configured(client):
    """The catalog says which providers can be used: mock always, real ones only with a key."""
    cat = client.get("/api/catalog").json()
    by_name = {p["name"]: p for p in cat["providers"]}
    assert by_name["mock"]["configured"] is True
    assert by_name["anthropic"]["configured"] is False
    assert cat["cache_enabled"] is False and cat["router_enabled"] is False


def test_default_settings_use_a_working_model_and_can_be_saved(client):
    """With no API keys the default model is mock/echo, and saved settings are read back."""
    s = client.get("/api/settings").json()
    assert (s["provider"], s["model"]) == ("mock", "echo")
    s["temperature"] = 0.3
    assert client.put("/api/settings", json=s).status_code == 200
    assert client.get("/api/settings").json()["temperature"] == 0.3


def test_saving_a_provider_without_api_key_is_rejected(client):
    """You cannot pick a provider whose key is missing; the error says which key to set."""
    r = client.put("/api/settings", json={"provider": "anthropic", "model": "claude-opus-5-5"})
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_chat_streams_an_answer_and_saves_the_conversation(client):
    """A chat streams meta -> text -> done, is saved with both messages, and a second turn sees the history."""
    events = send_chat(client, "hello there", routing="manual")
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    meta, done = events[0][1], events[-1][1]
    assert model_event(events)["routed_by"] == "user"
    assert "hello there" in answer_text(events)
    assert done["status"] == "ok" and done["cache_status"] == "bypass"

    cid = meta["conversation_id"]
    conv = client.get(f"/api/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["meta"]["model"] == "echo"
    assert conv["messages"][1]["meta"]["steps"][0]["id"] == "cache"
    assert conv["settings"]["routing"] == "manual"

    # second turn sees the history
    events = send_chat(client, "again", conversation_id=cid)
    assert "turn 2" in answer_text(events)
    assert len(client.get(f"/api/conversations/{cid}").json()["messages"]) == 4
    assert client.get("/api/conversations").json()[0]["id"] == cid


def test_chat_with_unknown_model_is_rejected(client):
    """A model that is not in catalog.yaml gives HTTP 400."""
    r = client.post("/api/chat", json={"message": "x", "settings": {"provider": "mock", "model": "nope"}})
    assert r.status_code == 400


def test_chat_with_provider_without_api_key_is_rejected(client):
    """A chat to a provider whose key is missing gives HTTP 400 instead of a failed API call."""
    r = client.post(
        "/api/chat", json={"message": "x", "settings": {"provider": "anthropic", "model": "claude-opus-5-5"}}
    )
    assert r.status_code == 400


def test_same_question_twice_is_answered_from_cache(make_client):
    """The second identical chat is a cache hit and skips the model. Different params or
    use_cache=False do not use the cache."""
    with make_client(cache_class=MEMORY_CACHE) as client:
        first = send_chat(client, "cache me")
        second = send_chat(client, "cache me")  # new conversation, same prompt + params
        assert first[-1][1]["cache_status"] == "miss"
        assert second[-1][1]["cache_status"] == "hit"
        st = final_steps(second)
        assert st["cache"]["value"] == "Hit"
        assert st["router"]["status"] == "stopped" and st["generate"]["status"] == "stopped"
        assert st["model"]["source"] == "cache" and st["save"]["status"] == "success"
        assert final_steps(first)["generate"]["status"] == "success"
        assert answer_text(first) == answer_text(second)
        # different params -> different key
        third = send_chat(client, "cache me", temperature=0.1)
        assert third[-1][1]["cache_status"] == "miss"
        # opting out
        fourth = send_chat(client, "cache me", use_cache=False)
        assert fourth[-1][1]["cache_status"] == "bypass"
        stats = client.get("/api/stats").json()
        assert stats["total_requests"] == 4 and stats["cache_hit_rate"] == round(1 / 3, 4)


def test_router_can_change_settings_and_falls_back_when_it_fails(make_client):
    """The router may override your model and params. If it has no opinion, picks a bad model,
    crashes or is too slow, your own settings are used. Manual mode never asks the router."""
    with make_client(router_class=FAKE_ROUTER) as client:
        ev = send_chat(client, "route me", level="low")
        m = model_event(ev)
        assert m["routed_by"] == "router"
        assert m["params"]["level"] == "high"
        assert m["route_reason"] == "test rule"
        st = final_steps(ev)
        assert st["router"]["status"] == "success" and st["router"]["label"] == "Ask JJ"
        assert st["level"]["value"] == "high" and st["level"]["source"] == "router"
        assert st["max_tokens"]["source"] == "model default"
        assert fake_router.CALLS[-1].requested.model == "echo"

        for prompt, reason, status in [
            ("keep", "JJ kept your settings", "success"),
            ("bogus", "JJ picked unavailable mock/does-not-exist: your settings", "warning"),
            ("boom", "JJ failed (RuntimeError): your settings", "warning"),
            ("slow", "JJ timed out: your settings", "warning"),
        ]:
            ev = send_chat(client, prompt, level="low")
            m = model_event(ev)
            assert m["routed_by"] == "user", prompt
            assert m["route_reason"] == reason
            assert m["params"]["level"] == "low"
            assert final_steps(ev)["router"]["status"] == status
            assert final_steps(ev)["level"]["source"] == "you"

        # manual mode skips the router entirely
        n = len(fake_router.CALLS)
        ev = send_chat(client, "route me", routing="manual")
        assert model_event(ev)["routed_by"] == "user" and len(fake_router.CALLS) == n
        assert final_steps(ev)["router"]["status"] == "stopped"


def test_settings_a_model_does_not_support_are_dropped():
    """Each model only gets the params it accepts: e.g. no temperature for Opus, and
    max_tokens is capped at the model's limit."""
    cat = load_catalog("catalog.yaml")
    opus = cat.get("anthropic", "claude-opus-5-5")
    s = RunSettings(provider="anthropic", model=opus.id, temperature=0.9, top_p=0.5, level="minimal",
                    max_tokens=10**9, system_prompt="  ")
    p = fit_params(opus, s)
    assert p.temperature is None and p.top_p is None  # sampling not accepted
    assert p.level == "medium"  # invalid level -> model default
    assert p.max_tokens == opus.max_output_tokens
    assert p.system_prompt is None

    llama = cat.get("ollama", "llama3.2")
    p = fit_params(llama, s)
    assert p.level is None and p.temperature == 0.9


def test_deleting_a_conversation_keeps_its_request_log(client):
    """A deleted conversation is gone, but its request log row stays (unlinked) for stats."""
    cid = send_chat(client, "bye")[0][1]["conversation_id"]
    assert client.delete(f"/api/conversations/{cid}").status_code == 204
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.get("/api/requests").json()[0]["conversation_id"] is None


def test_cache_hit_does_not_ask_the_router(make_client):
    """On a cache hit the router is not called, but the answer still says the router chose the model."""
    with make_client(cache_class=MEMORY_CACHE, router_class=FAKE_ROUTER) as client:
        send_chat(client, "route me once")
        n = len(fake_router.CALLS)
        ev = send_chat(client, "route me once")
        assert ev[-1][1]["cache_status"] == "hit"
        assert len(fake_router.CALLS) == n  # router not called on a hit
        assert model_event(ev)["routed_by"] == "router"  # the cached answer remembers who chose the model


def test_every_step_shown_in_the_ui_ends_with_a_status(client):
    """No step is left 'pending' after a chat, and steps without a plugin say 'Not connected'."""
    st = final_steps(send_chat(client, "hello"))
    assert list(st) == ["cache", "router", "model", "level", "max_tokens", "sampling", "generate", "save"]
    assert all(s["status"] != "pending" for s in st.values())
    assert st["router"]["value"] == "Not connected"
    assert st["cache"]["value"] == "Not connected"
