"""PixAI GraphQL API client for image generation with token rotation."""
from __future__ import annotations

import base64
import json as _json
import logging
import time
from pathlib import Path
from threading import Lock

import requests

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.pixai.art/graphql"
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "pixai.txt"
POLL_INTERVAL = 3
POLL_TIMEOUT = 120

# -- GraphQL templates --------------------------------------------------------

_CREATE_TASK = (
    "mutation($p: JSONObject!) { createGenerationTask(parameters: $p) { id } }"
)
_GET_TASK = "query($id: String!) { task(id: $id) { status outputs } }"
_GET_MEDIA = 'query($id: String!) { media(id: $id) { urls { variant url } } }'

# -- Default generation parameters --------------------------------------------

_DEFAULT_PARAMS: dict = {
    "modelId": "1861558740588989558",  # Haruka v2
    "width": 768,
    "height": 1280,
    "samplingSteps": 28,
    "samplingMethod": "Euler a",
    "cfgScale": 5,
    "clipSkip": 2,
    "priority": 1000,
    "lightning": False,
    "negativePrompts": (
        "worst quality, bad quality, low quality, lowres, "
        "anatomical nonsense, artistic error, bad anatomy, "
        "interlocked fingers, extra fingers, text, artist name, signature, "
        "bad feet, extra toes, ugly, poorly drawn, censor, blurry, watermark, "
        "simple background, transparent background, old, oldest, "
        "glitch, deformed, mutated, disfigured, long body, "
        "bad hands, missing fingers, extra digit, fewer digits, cropped, "
        "very displeasing, sketch, jpeg artifacts, username, "
        "censored, bar_censor, mosaic_censor, conjoined, bad ai-generated, "
        "long neck, skin blemishes, skin spots, acne, "
        "the wrong limb, error, black line, excess hands"
    ),
    "lora": {
        "1828916300199956210": 0.7,
        "1892009995685180258": 0.7,
    },
    "loraParameters": [
        {"weight": 0.7, "versionId": "1828916300199956210", "triggerWords": ""},
        {
            "weight": 0.7,
            "versionId": "1892009995685180258",
            "triggerWords": (
                "<LoRA//Boost/Body_aesthetics/5.2v> "
                "((perfect_anatomy, texture_details, beautiful_body, accessories))"
            ),
        },
    ],
    "controlNets": [],
    "seed": "",
    "upscale": 1.5,
    "upscaleDenoisingStrength": 0.6,
    "upscaleDenoisingSteps": 28,
    "upscaleSampler": "DPM++ SDE Karras",
    "promptHelper": {
        "withStage": True,
        "userWantToEnable": True,
        "enable": True,
        "forcePromptHelperDetectionSide": "client",
    },
}


def _token_exp(token: str) -> float:
    """Extract expiration timestamp from JWT payload."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(_json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return 0.0


def _clean_tokens(tokens: list[str]) -> list[str]:
    now = time.time()
    valid = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        exp = _token_exp(t)
        if exp and exp <= now:
            log.warning("pixai token expired (exp=%d), removed", int(exp))
            continue
        valid.append(t)
    return valid


class PixAIClient:
    def __init__(self, tokens: list[str]):
        self._tokens = _clean_tokens(tokens)
        self._index = 0
        self._lock = Lock()

    @property
    def available(self) -> bool:
        return bool(self._tokens)

    def set_tokens(self, tokens: list[str]) -> None:
        self._tokens = _clean_tokens(tokens)
        if self._index >= len(self._tokens):
            self._index = 0

    # -- internals ------------------------------------------------------------

    def _next_token(self) -> tuple[str, int]:
        """Return (token, 1-based index). Removes expired tokens on the fly."""
        with self._lock:
            now = time.time()
            # purge any tokens that expired since last clean
            before = len(self._tokens)
            self._tokens = [t for t in self._tokens if _token_exp(t) > now or _token_exp(t) == 0.0]
            if len(self._tokens) < before:
                log.warning("pixai removed %d expired token(s), %d remaining", before - len(self._tokens), len(self._tokens))
            if not self._tokens:
                raise RuntimeError("所有 PixAI token 已过期，请更新 token")
            self._index = self._index % len(self._tokens)
            token = self._tokens[self._index]
            idx = self._index + 1
            self._index = idx % len(self._tokens)
            return token, idx

    def _graphql(self, token: str, query: str, variables: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body: dict = {"query": query}
        if variables:
            body["variables"] = variables
        resp = requests.post(GRAPHQL_URL, headers=headers, json=body, timeout=30)
        if resp.status_code >= 400:
            log.error("pixai http %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(data["errors"][0].get("message", "PixAI API error"))
        return data["data"]

    def _create_task(self, token: str, prompt: str) -> str:
        params = {**_DEFAULT_PARAMS, "prompts": prompt, "extra": {"naturalPrompts": prompt}}
        data = self._graphql(token, _CREATE_TASK, {"p": params})
        return data["createGenerationTask"]["id"]

    def _poll_task(self, token: str, task_id: str) -> str:
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            task = self._graphql(token, _GET_TASK, {"id": task_id})["task"]
            log.debug("pixai task=%s status=%s outputs=%s", task_id, task.get("status"), task.get("outputs"))
            status = task["status"]
            if status == "completed":
                outputs = task.get("outputs") or {}
                media_id = outputs.get("mediaId") or outputs.get("media_id")
                if not media_id:
                    # try to extract from nested structures
                    imgs = outputs.get("imgs") or outputs.get("images") or []
                    if imgs and isinstance(imgs, list):
                        media_id = imgs[0].get("mediaId") or imgs[0].get("id")
                if not media_id:
                    raise RuntimeError(f"task completed but no mediaId in outputs: {outputs}")
                return media_id
            if status == "failed":
                # log the full task for debugging
                log.error("pixai task=%s failed, full outputs: %s", task_id, task.get("outputs"))
                raise RuntimeError(f"generation failed (task={task_id})")
            time.sleep(POLL_INTERVAL)
        raise TimeoutError("generation timed out")

    def _get_image_url(self, token: str, media_id: str) -> str:
        media = self._graphql(token, _GET_MEDIA, {"id": media_id})["media"]
        urls = media.get("urls") or []
        log.debug("pixai media=%s urls=%s", media_id, urls)
        # prefer PUBLIC, fall back to any available variant
        for u in urls:
            if u["variant"] == "PUBLIC":
                return u["url"]
        # fallback: use first available URL
        if urls:
            log.warning("pixai no PUBLIC variant, using %s instead", urls[0]["variant"])
            return urls[0]["url"]
        raise RuntimeError(f"no urls found for media {media_id}")

    @staticmethod
    def _read_suffix() -> str:
        try:
            return PROMPT_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # -- public API -----------------------------------------------------------

    def generate_image(self, prompt: str, *, _retries: int = 2) -> str:
        """Generate an image and return its URL. Rotates tokens, auto-retries on failure."""
        suffix = self._read_suffix()
        if suffix:
            prompt = f"{prompt}, {suffix}"
        last_exc: Exception | None = None
        for attempt in range(_retries + 1):
            token, idx = self._next_token()
            total = len(self._tokens)
            log.info("pixai generate token=%d/%d attempt=%d prompt=%s", idx, total, attempt + 1, prompt[:80])
            try:
                task_id = self._create_task(token, prompt)
                media_id = self._poll_task(token, task_id)
                return self._get_image_url(token, media_id)
            except Exception as exc:
                last_exc = exc
                log.warning("pixai token=%d/%d attempt=%d failed: %s", idx, total, attempt + 1, exc)
                if attempt < _retries:
                    time.sleep(1)
        raise RuntimeError(f"生图失败（已重试{_retries}次）: {last_exc}") from last_exc
