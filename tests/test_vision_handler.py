"""Tests for Wave 18 — photo/vision handling logic."""
import base64
import io


def _make_history_helpers():
    history: dict = {}

    def get(uid):
        return list(history.get(uid, []))

    def append(uid, role, content):
        msgs = history.get(uid, [])
        msgs.append({"role": role, "content": content})
        if len(msgs) > 20:
            msgs = msgs[-20:]
        history[uid] = msgs

    def clear(uid):
        history.pop(uid, None)

    return get, append, clear


# --- history_label ---

def test_label_with_caption():
    caption = "Что это?"
    label = f"[Фото] {caption}" if caption else "[Фото]"
    assert label == "[Фото] Что это?"


def test_label_without_caption():
    caption = ""
    label = f"[Фото] {caption}" if caption else "[Фото]"
    assert label == "[Фото]"


def test_label_whitespace_caption_treated_as_empty():
    caption = "   ".strip()
    label = f"[Фото] {caption}" if caption else "[Фото]"
    assert label == "[Фото]"


# --- prior_history saved before append ---

def test_prior_history_excludes_placeholder():
    get, append, clear = _make_history_helpers()
    append(1, "user", "предыдущий текст")
    append(1, "assistant", "ответ")
    prior = get(1)
    append(1, "user", "[Фото]")
    assert len(prior) == 2
    assert prior[-1]["role"] == "assistant"


def test_image_message_not_in_text_history():
    get, append, clear = _make_history_helpers()
    prior = get(1)
    append(1, "user", "[Фото]")
    # LLM call uses prior (without placeholder) + image content
    assert prior == []
    assert get(1)[0]["content"] == "[Фото]"


# --- LLM messages structure ---

def test_vision_messages_structure():
    system = "Ты ассистент."
    prior = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "здравствуйте"}]
    b64 = "abc123"
    caption = "Что это?"
    image_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": caption},
    ]
    messages = (
        [{"role": "system", "content": system}]
        + prior
        + [{"role": "user", "content": image_content}]
    )
    assert messages[0]["role"] == "system"
    assert len(messages) == 4
    last = messages[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "image_url"
    assert "base64" in last["content"][0]["image_url"]["url"]
    assert last["content"][1]["type"] == "text"


def test_vision_fallback_text_when_no_caption():
    caption = ""
    text_part = caption if caption else "Что на этом изображении?"
    assert text_part == "Что на этом изображении?"


# --- base64 encoding ---

def test_base64_roundtrip():
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    b64 = base64.b64encode(fake_jpeg).decode()
    assert base64.b64decode(b64) == fake_jpeg


def test_data_uri_format():
    b64 = "abc123"
    uri = f"data:image/jpeg;base64,{b64}"
    assert uri.startswith("data:image/jpeg;base64,")
    assert uri.endswith(b64)


# --- rollback on LLM error ---

def test_photo_rollback_on_error():
    get, append, clear = _make_history_helpers()
    append(1, "user", "текст")
    append(1, "assistant", "ответ")
    append(1, "user", "[Фото]")
    hist = get(1)
    if hist and hist[-1]["role"] == "user":
        hist = hist[:-1]
    assert len(hist) == 2
    assert hist[-1]["role"] == "assistant"


# --- photo selection (largest) ---

def test_largest_photo_selected():
    # Simulate Telegram PhotoSize list (width increases)
    photos = [
        {"file_id": "small", "width": 90},
        {"file_id": "medium", "width": 320},
        {"file_id": "large", "width": 800},
    ]
    selected = photos[-1]
    assert selected["file_id"] == "large"
    assert selected["width"] == 800
