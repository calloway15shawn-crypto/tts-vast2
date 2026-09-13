"""Настройки озвучки и проверки: значения по умолчанию и их проверка.

Модуль без внешних зависимостей: его использует и клиент на Windows, и сервер.
Клиент проверяет config.yaml до аренды машины, сервер — ещё раз при приёме задачи.
"""

DEFAULTS = {
    "max_wer": 0.12,         # допустимая доля расхождений по словам
    "max_attempts": 3,       # попыток на фрагмент
    "clone_mode": "xvec",    # xvec — только тембр; icl — тембр + манера (нужна расшифровка образца)
    "format": "mp3",
    "temperature": 0.9,
    "min_cps": 6.0,          # темп речи, символов в секунду: слишком медленно = подозрительно
    "max_cps": 30.0,
    "engines": {},           # переопределение движка для языка, например {"ru": "voxcpm"}
}

CLONE_MODES = ("xvec", "icl")
FORMATS = ("mp3", "wav")


def _number(values, key, cast, low, high):
    try:
        value = cast(values[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key}: нужно число, а не «{values[key]}»") from None
    if not low <= value <= high:
        raise ValueError(f"{key}: значение {value} вне допустимых пределов от {low} до {high}")
    return value


def validate(settings=None, **overrides):
    """Дополнить настройки значениями по умолчанию и проверить. Бросает ValueError."""
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("настройки должны быть парами «ключ: значение»")
    s = {**DEFAULTS, **(settings or {}), **overrides}
    s["max_wer"] = _number(s, "max_wer", float, 0.0, 1.0)
    s["max_attempts"] = _number(s, "max_attempts", int, 1, 10)
    s["temperature"] = _number(s, "temperature", float, 0.05, 2.0)
    s["min_cps"] = _number(s, "min_cps", float, 0.5, 100.0)
    s["max_cps"] = _number(s, "max_cps", float, 0.5, 100.0)
    if s["min_cps"] >= s["max_cps"]:
        raise ValueError(f"min_cps ({s['min_cps']}) должен быть меньше max_cps ({s['max_cps']})")
    if s["clone_mode"] not in CLONE_MODES:
        raise ValueError(f"clone_mode: нужно {' или '.join(CLONE_MODES)}, а не «{s['clone_mode']}»")
    if s["format"] not in FORMATS:
        raise ValueError(f"format: нужно {' или '.join(FORMATS)}, а не «{s['format']}»")
    if not isinstance(s["engines"], dict):
        raise ValueError("engines: нужны пары вида «ru: voxcpm»")
    return s
