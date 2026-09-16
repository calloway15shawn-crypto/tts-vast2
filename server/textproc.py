"""Работа с текстом: очистка, предпроверка, нарезка на фрагменты, сравнение с распознанным.

Модуль без внешних зависимостей: его использует и клиент на Windows, и сервер.
"""
import re
import unicodedata

ROMAN_RE = re.compile(r"\b(?=[MDCLXVI]{2,}\b)M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\b")
DIGIT_RE = re.compile(r"\d")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
RU_ABBR_RE = re.compile(r"(?<![А-Яа-яЁё])(?:т\. ?е\.|т\. ?д\.|т\. ?п\.|и др\.|см\.|напр\.|г\.|гг\.|в\. ?в\.|до н\. ?э\.|н\. ?э\.|тыс\.|млн\.?|млрд\.?|км\.?)(?=\s|$|[,;:])")
MARKUP_RE = re.compile(r"[#*_`~<>\[\]{}|\\]")

PAUSE_PARAGRAPH = 0.8   # сек. после абзаца
PAUSE_SENTENCE = 0.35   # сек. между предложениями
PAUSE_SPLIT = 0.12      # сек. если предложение пришлось разрезать


def clean_text(raw):
    """Убрать BOM, разметку, лишние пробелы. Абзацы разделяются пустой строкой."""
    text = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\t", " ")
    text = MARKUP_RE.sub(" ", text)
    lines = [re.sub(r" {2,}", " ", ln).strip() for ln in text.split("\n")]
    # Каждая непустая строка считается абзацем
    paragraphs = [ln for ln in lines if ln]
    return "\n\n".join(paragraphs)


def preflight(raw, lang):
    """Проверить сценарий до запуска. Возвращает (ошибки, предупреждения)."""
    errors, warnings = [], []
    text = raw.replace("\ufeff", "")
    if not text.strip():
        errors.append("файл пустой")
        return errors, warnings
    for no, line in enumerate(text.splitlines(), 1):
        if DIGIT_RE.search(line):
            frag = _around(line, DIGIT_RE.search(line).start())
            errors.append(f"строка {no}: цифры «{frag}» — напишите числа словами в нужном падеже")
        if any(unicodedata.category(ch) == "Mn" for ch in line):
            warnings.append(
                f"строка {no}: знаки ударения — Qwen3-TTS произносит их как лишний звук "
                "(«за́мок» читается «заумок»), уберите их"
            )
        for m in ROMAN_RE.finditer(line):
            warnings.append(f"строка {no}: римские цифры «{m.group(0)}» — лучше написать словами")
        if lang in ("ru",):
            latin = [w for w in LATIN_WORD_RE.findall(line) if not ROMAN_RE.fullmatch(w)]
            abbr = RU_ABBR_RE.findall(line)
            if abbr:
                warnings.append(
                    f"строка {no}: сокращения ({', '.join(abbr[:3])}) — напишите полностью, иначе модель прочитает по буквам"
                )
            if latin:
                warnings.append(
                    f"строка {no}: латиница ({', '.join(latin[:3])}) — модель может прочитать неверно, "
                    f"лучше написать кириллицей"
                )
        for sent in re.split(r"(?<=[.!?…])\s+", line):
            if len(sent) > 450:
                warnings.append(f"строка {no}: очень длинное предложение ({len(sent)} симв.) — будет разрезано")
    return errors, warnings


def _around(line, pos, width=18):
    start, end = max(0, pos - width), min(len(line), pos + width)
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def _split_sentences(paragraph):
    """Разбить абзац на предложения. Разрез только если следующее начинается с заглавной."""
    parts = re.split(r"(?<=[.!?…])\s+", paragraph)
    sentences = []
    for part in parts:
        if not part:
            continue
        first_letter = next((ch for ch in part if ch.isalpha()), "")
        if sentences and first_letter and not first_letter.isupper():
            sentences[-1] += " " + part   # «т. е. что-то» — не новое предложение
        else:
            sentences.append(part)
    return sentences


def _split_long(sentence, max_chars):
    """Разрезать слишком длинное предложение по знакам препинания, затем по пробелам."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces = re.split(r"(?<=[,;:—–])\s+", sentence)
    out, cur = [], ""
    for piece in pieces:
        if len(piece) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            words, buf = piece.split(" "), ""
            for w in words:
                if buf and len(buf) + 1 + len(w) > max_chars:
                    out.append(buf)
                    buf = w
                else:
                    buf = f"{buf} {w}".strip()
            if buf:
                out.append(buf)
            continue
        if cur and len(cur) + 1 + len(piece) > max_chars:
            out.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        out.append(cur)
    return out


def split_chunks(text, max_chars=300, min_chars=60):
    """Нарезать очищенный текст на фрагменты для озвучки.

    Возвращает список словарей: {"text": ..., "pause": пауза после фрагмента в секундах}.
    """
    chunks = []
    for paragraph in clean_text(text).split("\n\n"):
        units = []  # (кусок, пауза после)
        for sentence in _split_sentences(paragraph):
            parts = _split_long(sentence, max_chars)
            for i, part in enumerate(parts):
                units.append((part, PAUSE_SPLIT if i < len(parts) - 1 else PAUSE_SENTENCE))
        cur_text, cur_pause = "", PAUSE_SENTENCE
        for part, pause in units:
            if cur_text and len(cur_text) + 1 + len(part) > max_chars and len(cur_text) >= min_chars:
                chunks.append({"text": cur_text, "pause": cur_pause})
                cur_text = part
            else:
                cur_text = f"{cur_text} {part}".strip()
            cur_pause = pause
        if cur_text:
            chunks.append({"text": cur_text, "pause": PAUSE_PARAGRAPH})
    if chunks:
        chunks[-1]["pause"] = 0.0
    return chunks


def norm_words(s):
    """Нормализация для сравнения: нижний регистр, ё→е, без пунктуации и знаков ударения."""
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("N"):
            out.append(ch)
        elif cat == "Mn":
            continue          # знак ударения: на сравнение не влияет, слово не разрывает
        elif ch in "'’ʼ":
            continue
        else:
            out.append(" ")
    return "".join(out).split()


def wer(reference, hypothesis):
    """Доля ошибок по словам (расстояние Левенштейна / число слов эталона)."""
    ref, hyp = norm_words(reference), norm_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def format_tc(seconds):
    """Секунды -> ЧЧ:ММ:СС."""
    seconds = int(round(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
