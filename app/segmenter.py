"""Turns the growing committed transcript into complete sentences (append-only).

Translating only finished sentences keeps the translation column stable: it never
flickers or rewrites itself, at the cost of waiting for the sentence to end.
"""
import os
import re
import time

# Sentence end: . ! ? … ؟ (Arabic) followed by optional closing quotes/brackets,
# then whitespace or the end of the committed text.
SENT_END = re.compile(r'[.!?…؟]+["»”’)\]]*(?=\s|$)')

STOPWORDS = {
    "en": set("the and is are was were to of in that it you i we this for with on not be have what do can will they he she".split()),
    "de": set("der die das und ist nicht ich du wir sie es ein eine zu mit auf für den dem von sind war haben wie was auch aber".split()),
    "fr": set("le la les et est des une un je tu nous vous il elle pas que qui pour dans sur avec ce cette sont du au mais".split()),
}


def detect_lang(text: str, default: str = "en") -> str:
    """Cheap script + stopword detection for the five supported languages."""
    if re.search(r"[؀-ۿ]", text):
        return "ar"
    if re.search(r"[Ѐ-ӿ]", text):
        return "ru"
    low = text.lower()
    words = re.findall(r"[^\W\d_]+", low)
    score = {k: sum(w in v for w in words) for k, v in STOPWORDS.items()}
    if re.search(r"[äöüß]", low):
        score["de"] += 2
    if re.search(r"[éèêàçùœâî]", low):
        score["fr"] += 2
    best = max(score, key=score.get)
    return best if score[best] > 0 else default


class Segmenter:
    def __init__(self, max_words: int = 28, idle_flush: float = 1.5, hard_idle: float = 4.0):
        self.max_words = max_words
        self.idle_flush = idle_flush
        self.hard_idle = hard_idle
        self.prev = ""
        self.done = 0
        self.last_change = time.monotonic()

    def update(self, committed: str, has_buffer: bool) -> list[str]:
        now = time.monotonic()
        if committed != self.prev:
            if not committed.startswith(self.prev[: self.done]):
                # Committed text was revised (rare): never re-emit what we already sent.
                self.done = min(self.done, len(os.path.commonprefix([committed, self.prev])))
            self.prev = committed
            self.last_change = now
        out = []
        while True:
            cut = self._cut(committed[self.done:])
            if cut is None:
                break
            sentence = committed[self.done: self.done + cut].strip()
            self.done += cut
            if sentence:
                out.append(sentence)
        rest = committed[self.done:].strip()
        idle = now - self.last_change
        # Speaker paused without punctuation: ship what we have.
        if rest and ((not has_buffer and idle >= self.idle_flush) or idle >= self.hard_idle):
            out.append(rest)
            self.done = len(committed)
        return out

    def _cut(self, pending: str):
        if not pending.strip():
            return None
        candidates = []
        m = SENT_END.search(pending)
        if m:
            candidates.append(m.end())
        nl = pending.find("\n")
        if nl >= 0:
            candidates.append(nl + 1)
        if candidates:
            return min(candidates)
        word_ends = [w.end() for w in re.finditer(r"\S+", pending)]
        if len(word_ends) > self.max_words:  # very long run-on: cut at a comma if possible
            limit = word_ends[self.max_words - 1]
            comma = max(pending.rfind(",", 0, limit), pending.rfind("،", 0, limit))
            return comma + 1 if comma > len(pending) // 4 else limit
        return None

    def pending(self) -> str:
        return self.prev[self.done:].strip()

    def flush(self) -> list[str]:
        rest = self.pending()
        self.done = len(self.prev)
        return [rest] if rest else []
