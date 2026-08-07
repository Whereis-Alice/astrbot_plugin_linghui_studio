"""Optional prompt translation and refinement using a separate chat model."""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot import logger


class PromptProcessor:
    """Best-effort prompt processing.

    A processor outage must never block image generation, so failures always
    fall back to the original prompt.  It intentionally uses a separate
    OpenAI-compatible endpoint/model instead of consuming drawing-channel
    capacity.
    """

    def __init__(self, config: Any):
        self.config = config

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    @staticmethod
    def _endpoint(base_url: str) -> str:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    async def prepare(self, prompt: str) -> str:
        prompt = str(prompt or "").strip()
        if not prompt:
            return prompt

        translate = self._as_bool(self.config.get("enable_prompt_translation", False))
        optimize = self._as_bool(self.config.get("enable_prompt_optimization", False))
        if not translate and not optimize:
            return prompt

        result = prompt
        # Translation first gives the optimizer a consistent language target.
        if translate:
            result = await self._run("translation", result)
        if optimize:
            result = await self._run("optimization", result)
        return result

    async def _run(self, operation: str, prompt: str) -> str:
        key_name = "prompt_translation_model" if operation == "translation" else "prompt_optimization_model"
        model = str(self.config.get(key_name, "") or "").strip()
        endpoint = self._endpoint(self.config.get("prompt_processor_base_url", ""))
        api_key = str(self.config.get("prompt_processor_api_key", "") or "").strip()
        if not endpoint or not api_key or not model:
            logger.warning("Linghui prompt %s is enabled but its endpoint, API key, or model is missing.", operation)
            return prompt

        if operation == "translation":
            system = str(
                self.config.get(
                    "prompt_translation_system_prompt",
                    "Translate the image-generation prompt into concise English. Preserve all constraints, "
                    "proper nouns, composition, aspect ratio, and safety requirements. Output only the prompt.",
                )
                or ""
            )
        else:
            system = str(
                self.config.get(
                    "prompt_optimization_system_prompt",
                    "Rewrite this as a precise image-generation prompt. Preserve the user's intent and safety "
                    "constraints. Do not add sexual, violent, copyrighted-character, or unsupported claims. "
                    "Output only the improved prompt.",
                )
                or ""
            )

        timeout_seconds = max(5, int(self.config.get("prompt_processor_timeout", 30) or 30))
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400:
                        logger.warning("Linghui prompt %s failed with HTTP %s", operation, response.status)
                        return prompt
            if not isinstance(body, dict):
                return prompt
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                )
            content = str(content or "").strip()
            if not content or len(content) > 16_000:
                return prompt
            return content
        except Exception as exc:
            logger.warning("Linghui prompt %s failed; using original prompt: %s", operation, exc)
            return prompt
