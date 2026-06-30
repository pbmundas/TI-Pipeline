"""
ollama_client.py
Thin wrapper around a local Ollama server's /api/chat endpoint.
This is the only file that talks to the model - everything else in the
pipeline just calls `OllamaClient.chat_json(...)` and gets a Python object
back. Swapping models, hosts, or even back to a hosted API later only
requires touching this file.
"""
import json
import logging
import re
import time

import requests

log = logging.getLogger("ti_pipeline.ollama")


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host, model, temperature=0.2, timeout=300):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def _post(self, payload):
        url = f"{self.host}/api/chat"
        log.info("Calling Ollama (%s)... this can take 30s-several minutes on CPU", self.model)
        start = time.monotonic()
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            raise OllamaError(
                f"Could not reach Ollama at {self.host}. Is `ollama serve` running "
                f"and is the model pulled (`ollama pull {self.model}`)?"
            ) from e
        elapsed = time.monotonic() - start
        log.info("Ollama responded in %.1fs", elapsed)
        if resp.status_code != 200:
            raise OllamaError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def chat_json(self, system_prompt, user_prompt, retries=2):
        """Send a chat request and parse the reply as JSON.
        Uses Ollama's native `format: json` mode to bias the model toward
        valid JSON, then defensively re-extracts/repairs on failure."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": self.temperature},
        }

        last_err = None
        for attempt in range(1, retries + 2):
            data = self._post(payload)
            content = data.get("message", {}).get("content", "")
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                last_err = e
                repaired = self._extract_json_block(content)
                if repaired is not None:
                    try:
                        return json.loads(repaired)
                    except json.JSONDecodeError as e2:
                        last_err = e2
                log.warning(
                    "Attempt %d: model did not return valid JSON, retrying", attempt
                )
        raise OllamaError(f"Model never returned valid JSON after retries: {last_err}")

    @staticmethod
    def _extract_json_block(text):
        """Best-effort recovery if the model wraps JSON in prose/fences."""
        fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if fence:
            return fence.group(1)
        brace = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if brace:
            return brace.group(1)
        return None
