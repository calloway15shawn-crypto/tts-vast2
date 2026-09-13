"""Таблица языков и движков. Общая для клиента и сервера (без внешних зависимостей)."""

# code -> (название для Qwen3-TTS, название для Whisper, человекочитаемое)
LANGS = {
    "ru": {"qwen": "Russian", "whisper": "russian", "title": "русский"},
    "en": {"qwen": "English", "whisper": "english", "title": "английский"},
    "de": {"qwen": "German", "whisper": "german", "title": "немецкий"},
    "fr": {"qwen": "French", "whisper": "french", "title": "французский"},
    "es": {"qwen": "Spanish", "whisper": "spanish", "title": "испанский"},
    "pt": {"qwen": "Portuguese", "whisper": "portuguese", "title": "португальский"},
    "it": {"qwen": "Italian", "whisper": "italian", "title": "итальянский"},
    "pl": {"qwen": None, "whisper": "polish", "title": "польский"},
}

# Какие языки умеет каждый движок
ENGINE_LANGS = {
    "qwen": {"ru", "en", "de", "fr", "es", "pt", "it"},
    "voxcpm": {"ru", "en", "de", "fr", "es", "pt", "it", "pl"},
}

# Движок по умолчанию для каждого языка
DEFAULT_ENGINE = {
    "ru": "qwen", "en": "qwen", "de": "qwen", "fr": "qwen",
    "es": "qwen", "pt": "qwen", "it": "qwen",
    "pl": "voxcpm",
}

# Максимальная длина одного фрагмента для движка (символов)
ENGINE_MAX_CHARS = {"qwen": 300, "voxcpm": 260}


def resolve_engine(lang, overrides=None):
    """Вернуть движок для языка с учётом настроек пользователя или бросить ValueError."""
    if lang not in LANGS:
        raise ValueError(f"язык '{lang}' не поддерживается (есть: {', '.join(sorted(LANGS))})")
    engine = (overrides or {}).get(lang) or DEFAULT_ENGINE[lang]
    if engine not in ENGINE_LANGS:
        raise ValueError(f"неизвестный движок '{engine}' (есть: {', '.join(ENGINE_LANGS)})")
    if lang not in ENGINE_LANGS[engine]:
        raise ValueError(f"движок '{engine}' не умеет язык '{lang}'")
    return engine
