import os
import requests
from typing import Optional, Dict

LLM_BACKEND = os.environ.get("LLM_BACKEND", "mock")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
VLLM_URL = os.environ.get("VLLM_BASE_URL", "http://vllm:8000")


def _mock_summarize(text: str, length: str = "short") -> str:
    # Very simple heuristic summarizer: return the first N characters or first sentence
    if not text:
        return ""
    # Return first sentence if available
    for sep in (".\n", "\n", ". "):
        parts = text.split(sep)
        if parts and len(parts[0]) > 20:
            return parts[0].strip() + ("." if not parts[0].strip().endswith('.') else "")
    # fallback to truncation
    limit = 200 if length == "long" else 100
    return (text[:limit] + "...") if len(text) > limit else text


def _mock_rewrite(text: str, style: Optional[Dict] = None) -> str:
    # Very naive rewrite: adjust tone by simple replacements
    if not text:
        return ""
    tone = style.get("tone") if style else None
    if tone == "professional":
        return text.replace("yo ", "").replace("like ", "") + "\n\n(edited: professional)"
    if tone == "concise":
        return text.split(".")[0] + ".\n\n(edited: concise)"
    return text + "\n\n(edited: mock)"


def summarize(text: str, length: str = "short", model: Optional[str] = None) -> str:
    backend = (model and model.lower()) or LLM_BACKEND
    if backend == "mock":
        return _mock_summarize(text, length=length)

    if backend == "ollama":
        try:
            data = {"model": model or "default", "prompt": text, "length": length}
            resp = requests.post(f"{OLLAMA_URL}/v1/generate", json=data, timeout=30)
            resp.raise_for_status()
            return resp.json().get("output", "")
        except Exception:
            return _mock_summarize(text, length=length)

    if backend == "vllm":
        try:
            data = {"prompt": text, "max_tokens": 200}
            resp = requests.post(f"{VLLM_URL}/generate", json=data, timeout=30)
            resp.raise_for_status()
            return resp.json().get("text", "")
        except Exception:
            return _mock_summarize(text, length=length)

    return _mock_summarize(text, length=length)


def rewrite(text: str, style: Optional[Dict] = None, model: Optional[str] = None) -> str:
    backend = (model and model.lower()) or LLM_BACKEND
    if backend == "mock":
        return _mock_rewrite(text, style=style or {})

    if backend == "ollama":
        try:
            prompt = {"instruction": "Rewrite the following text", "text": text, "style": style}
            resp = requests.post(f"{OLLAMA_URL}/v1/rewrite", json=prompt, timeout=30)
            resp.raise_for_status()
            return resp.json().get("output", "")
        except Exception:
            return _mock_rewrite(text, style=style or {})

    if backend == "vllm":
        try:
            data = {"prompt": text, "style": style}
            resp = requests.post(f"{VLLM_URL}/rewrite", json=data, timeout=30)
            resp.raise_for_status()
            return resp.json().get("text", "")
        except Exception:
            return _mock_rewrite(text, style=style or {})

    return _mock_rewrite(text, style=style or {})
