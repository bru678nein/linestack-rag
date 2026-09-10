"""Responsibility: talking to a generation model, local or OpenAI, behind one shape.

Owns: loading a local instruct model with transformers on Apple's GPU (MPS),
the OpenAI chat alternative, and the rule that picks between them from the
model name.

Mirrors `linestack/retrieval/embedding.py` on purpose. Local is the default so
the whole pipeline -- crawl, embed, retrieve, answer -- runs with no account
and no key (ADR-0017 for embeddings, ADR-0022 for this). torch and
transformers are already installed by the `[local]` extra for the embedder, so
local generation adds a model download and no new dependency.

Does not own: prompts, context, or deciding whether an answer is supported.
Both clients expose one coroutine, `complete(messages, max_tokens=...,
temperature=...) -> str`, and nothing above this module knows which it has.
"""

from __future__ import annotations

import asyncio
import re
import time

from linestack.config import settings

# Model names routed to the OpenAI chat API. A prefix match rather than the
# embedder's explicit set, because OpenAI's chat model names churn far faster
# than its embedding model names do.
OPENAI_CHAT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4")


class GenerationFailed(RuntimeError):
    """The model could not be loaded or did not produce an answer."""


def uses_openai_chat(model: str) -> bool:
    return model.startswith(OPENAI_CHAT_PREFIXES)


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_reasoning(text: str) -> str:
    """Remove a reasoning block if the model emitted one anyway.

    Qwen3 thinks out loud by default, inside `<think>...</think>`, before it
    answers. Reasoning is switched off at the chat template
    (`enable_thinking=False`), and this is the second line: a reasoning block
    that reached the answer would be read by the citation checker as claims,
    and one that the token limit cut off mid-thought has no closing tag at all.
    """
    text = _THINK_RE.sub("", text)
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


class LocalGenerator:
    """A Hugging Face instruct model on this machine.

    Loads lazily, on the first `complete`, for the reason the embedder does:
    a run that answers nothing should load nothing (see the harness's
    `_Embedder`, which learned this the hard way). `load_seconds` is recorded
    separately so a first answer's latency is not reported as generation.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.name = model_name or settings.generation_model
        self.load_seconds = 0.0
        self._tokenizer = None
        self._model = None
        self._device = "cpu"

    def _load(self):
        if self._model is None:
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as exc:  # pragma: no cover - install guidance
                raise GenerationFailed(
                    f"{self.name} is a local model but transformers is not "
                    f"installed. Run: uv pip install -e '.[local]'. Or set "
                    f"GENERATION_MODEL to an OpenAI model and provide "
                    f"OPENAI_API_KEY."
                ) from exc
            started = time.perf_counter()
            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._tokenizer = AutoTokenizer.from_pretrained(self.name)
            self._model = AutoModelForCausalLM.from_pretrained(
                self.name,
                # Half precision on the GPU halves memory on a 16 GB machine
                # that is also holding the embedder. CPU stays full precision:
                # float16 matmuls are slow or unsupported there.
                dtype=torch.float16 if self._device == "mps" else torch.float32,
            ).to(self._device)
            self._model.eval()
            self.load_seconds = time.perf_counter() - started
        return self._tokenizer, self._model

    def _generate(
        self, messages: list[dict[str, str]], max_tokens: int, temperature: float
    ) -> str:
        import torch

        tokenizer, model = self._load()
        prompt = tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
            # Qwen3's template reads this; templates that do not simply ignore
            # an unused variable.
            enable_thinking=False,
        )
        inputs = tokenizer(prompt, return_tensors="pt").to(self._device)
        options = {
            "max_new_tokens": max_tokens,
            # Greedy at temperature 0, so the same context gives the same
            # answer and a before-and-after compares like with like.
            "do_sample": temperature > 0,
            "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id,
        }
        if temperature > 0:
            options["temperature"] = temperature
        with torch.no_grad():
            output = model.generate(**inputs, **options)
        generated = output[0][inputs["input_ids"].shape[1] :]
        return strip_reasoning(tokenizer.decode(generated, skip_special_tokens=True))

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        # Off the event loop: a local generate() is seconds of blocking work.
        return await asyncio.to_thread(
            self._generate, messages, max_tokens, temperature
        )


class OpenAIGenerator:
    """The OpenAI chat API, for when a key exists.

    **[unverified]** This machine has no key by design (ADR-0017), so this path
    has not been exercised against the real API. It is kept in the same shape as
    the local one so that switching is a configuration change, and it should be
    checked the first time a key is available.
    """

    def __init__(self, model_name: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.name = model_name or settings.generation_model
        self.load_seconds = 0.0
        self._client = AsyncOpenAI(api_key=settings.require_openai_key())

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self.name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return strip_reasoning(response.choices[0].message.content or "")


def build_generator(model: str | None = None):
    """The generator for the configured model. Local unless it is an OpenAI one."""
    name = model or settings.generation_model
    return OpenAIGenerator(name) if uses_openai_chat(name) else LocalGenerator(name)
