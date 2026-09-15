"""OpenAI-compatible chat client with native tool calling, plus a JSON-action fallback for models
that do not support tools. Backend-agnostic: llama-server, LM Studio, vLLM, Ollama, OpenAI, ..."""
from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from ..config import Config

JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = AsyncOpenAI(base_url=cfg.llm.base_url, api_key=cfg.llm.api_key or "not-needed", timeout=cfg.llm.timeout_s)
        self.model = cfg.llm.model
        self.supports_tools: bool | None = None if cfg.llm.supports_tools == "auto" else bool(cfg.llm.supports_tools)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   json_mode: bool = False, temperature: float | None = None) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages,
                                  "temperature": self.cfg.llm.temperature if temperature is None else temperature}
        if tools and self.supports_tools is not False:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if tools and self.supports_tools is None and ("tool" in msg or "function" in msg or "400" in msg):
                self.supports_tools = False
                kwargs.pop("tools", None); kwargs.pop("tool_choice", None)
                resp = await self.client.chat.completions.create(**kwargs)
            elif json_mode and ("response_format" in msg or "json" in msg):
                kwargs.pop("response_format", None)
                resp = await self.client.chat.completions.create(**kwargs)
            else:
                raise
        if tools and self.supports_tools is None:
            # probe result: if the model ever emits a tool call we know it works
            if resp.choices and resp.choices[0].message.tool_calls:
                self.supports_tools = True
        return resp.choices[0].message

    @staticmethod
    def extract_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        m = JSON_FENCE.search(text)
        cands = [m.group(1)] if m else []
        # first balanced {...}
        start = text.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        cands.append(text[start:i + 1])
                        break
        for c in cands:
            try:
                return json.loads(c)
            except json.JSONDecodeError:
                continue
        return None
