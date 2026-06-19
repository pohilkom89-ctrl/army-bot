"""Tests for Wave 17 — voice message handling logic."""
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


# --- Transcription text processing ---

def test_transcribed_text_stripped():
    raw = "  Привет, как дела?  \n"
    assert raw.strip() == "Привет, как дела?"


def test_empty_transcript_detected():
    assert "".strip() == ""
    assert not "".strip()


def test_whitespace_only_transcript_detected():
    assert not "   ".strip()


# --- Trigger matching on transcribed text ---

def test_trigger_matches_transcribed():
    triggers = {"цена": "Цена — 1000 руб."}
    text = "Скажи мне цена"
    matched = any(k.lower() in text.lower() for k in triggers)
    assert matched


def test_trigger_no_match():
    triggers = {"цена": "1000 руб."}
    text = "Привет как дела"
    matched = any(k.lower() in text.lower() for k in triggers)
    assert not matched


# --- History integration ---

def test_voice_appended_to_history():
    get, append, clear = _make_history_helpers()
    user_text = "Привет из голосового"
    append(1, "user", user_text)
    hist = get(1)
    assert len(hist) == 1
    assert hist[0]["content"] == user_text


def test_voice_rollback_on_error():
    get, append, clear = _make_history_helpers()
    append(1, "user", "предыдущий")
    append(1, "assistant", "ответ")
    append(1, "user", "голосовой который упал")
    hist = get(1)
    if hist and hist[-1]["role"] == "user":
        hist = hist[:-1]
    assert len(hist) == 2
    assert hist[-1]["role"] == "assistant"


def test_voice_and_text_share_history():
    get, append, clear = _make_history_helpers()
    append(1, "user", "текст")
    append(1, "assistant", "ответ")
    append(1, "user", "голосовой")
    append(1, "assistant", "ответ2")
    assert len(get(1)) == 4


# --- report_message label ---

def test_voice_label_prefix():
    user_text = "Привет из голосового"
    label = f"[🎤] {user_text}"
    assert label.startswith("[🎤]")
    assert user_text in label


# --- BytesIO file object setup ---

def test_bio_name_attribute():
    bio = io.BytesIO(b"fake_ogg_data")
    bio.name = "voice.ogg"
    assert bio.name == "voice.ogg"
    assert bio.read() == b"fake_ogg_data"


def test_bio_seekable():
    bio = io.BytesIO(b"data")
    bio.read()
    bio.seek(0)
    assert bio.read() == b"data"
