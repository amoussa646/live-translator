"""Sentence translation with TranslateGemma 4B (MLX, runs on the Mac's GPU, offline).

Always call from ONE thread (the server uses a single-worker executor):
MLX streams are per-thread.
"""
import logging
import time

from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

log = logging.getLogger("translator")

LANGS = {"en": "English", "ru": "Russian", "ar": "Arabic", "de": "German", "fr": "French"}
EOT = "<end_of_turn>"


class Translator:
    def __init__(self, model_dir: str):
        t = time.time()
        self.model, self.tok = load(model_dir)
        self.sampler = make_sampler(temp=0.0)  # deterministic: same sentence -> same translation
        self.translate("Hello, how are you?", "en", "ru")  # compile Metal kernels now, not on sentence 1
        log.info("Translator ready in %.1fs", time.time() - t)

    def translate(self, text: str, src: str, tgt: str) -> str:
        text = text.strip()
        if not text or src == tgt:
            return text
        content = [{"type": "text", "source_lang_code": src, "target_lang_code": tgt, "text": text}]
        prompt = self.tok.apply_chat_template([{"role": "user", "content": content}], add_generation_prompt=True)
        out = ""
        max_tokens = min(512, 48 + 4 * len(text.split()))
        for chunk in stream_generate(self.model, self.tok, prompt=prompt, max_tokens=max_tokens, sampler=self.sampler):
            out += chunk.text
            if EOT in out:
                break
        return out.replace(EOT, "").strip()
