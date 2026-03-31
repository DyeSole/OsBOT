from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

HF_API_ROOT = "https://api-inference.huggingface.co/models"


class HFImageClient:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key.strip()
        self.model = model.strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.model)

    def apply_settings(self, api_key: str, model: str) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()

    def generate_image(self, prompt: str) -> bytes:
        if not self.available:
            raise RuntimeError("Hugging Face image API 未配置")

        resp = requests.post(
            f"{HF_API_ROOT}/{self.model}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "image/png",
            },
            json={"inputs": prompt},
            timeout=180,
        )
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise RuntimeError(f"huggingface http {resp.status_code}: {snippet}")
        content_type = (resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            raise RuntimeError(f"huggingface returned json: {resp.text[:300]}")
        if len(resp.content) < 1000:
            raise RuntimeError(f"huggingface image too small ({len(resp.content)} bytes)")
        return resp.content
