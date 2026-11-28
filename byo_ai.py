"""
Gọi model AI bằng API key riêng của user (ai_credentials).

Chỉ là một lời gọi HTTP trả về văn bản: model KHÔNG có công cụ nào chạy trên máy chủ (không shell,
không đọc file), khác với Antigravity / Claude Code CLI. Mọi ngữ cảnh (cây thư mục, file cấu hình, log)
đã nằm sẵn trong prompt do Vibe Core dựng.

Hỗ trợ:
  • openai, openai_compatible: POST {base_url}/chat/completions (OpenRouter, DeepSeek, Groq, Ollama...)
  • anthropic: POST https://api.anthropic.com/v1/messages
  • gemini:    POST .../models/{model}:generateContent
Thông báo lỗi luôn che API key (không lặp lại key, không in header).
"""

from typing import Any, Dict, Optional

import requests

from ai_credentials import PROVIDERS

MAX_OUTPUT_TOKENS = 8192
ANTHROPIC_VERSION = "2023-06-01"


class ByoAIError(RuntimeError):
    pass


def _redact(text: str, key: str) -> str:
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
    return text[:300]


def _post(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int, key: str) -> Dict[str, Any]:
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise ByoAIError(f"Không kết nối được API AI: {_redact(e, key)}")
    if res.status_code >= 400:
        try:
            body = res.json()
            detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
            detail = detail or body
        except ValueError:
            detail = res.text
        raise ByoAIError(f"API AI trả lỗi HTTP {res.status_code}: {_redact(detail, key)}")
    try:
        return res.json()
    except ValueError:
        raise ByoAIError("API AI trả về dữ liệu không phải JSON.")


def complete(cred: Dict[str, Any], prompt: str, timeout: int = 90, system: Optional[str] = None) -> str:
    """Gửi một prompt, trả về văn bản model trả lời"""
    provider, key, model = cred["provider"], cred["api_key"], cred["model"]
    if provider in ("openai", "openai_compatible"):
        base = (cred.get("base_url") or PROVIDERS["openai"]["base_url"]).rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        data = _post(f"{base}/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": model, "messages": messages}, timeout, key)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ByoAIError("Phản hồi chat/completions không đúng định dạng.")
    if provider == "anthropic":
        payload: Dict[str, Any] = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS,
                                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        data = _post(f"{PROVIDERS['anthropic']['base_url']}/v1/messages",
                     {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, payload, timeout, key)
        parts = [b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"]
        if not parts:
            raise ByoAIError("Phản hồi Anthropic không có nội dung văn bản.")
        return "".join(parts)
    if provider == "gemini":
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(f"{PROVIDERS['gemini']['base_url']}/models/{model}:generateContent",
                     {"x-goog-api-key": key}, payload, timeout, key)
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise ByoAIError("Phản hồi Gemini không có nội dung (có thể bị chặn bởi bộ lọc an toàn).")
        return "".join(p.get("text", "") for p in parts)
    raise ByoAIError(f"Hãng AI '{provider}' không được hỗ trợ.")


def test_connection(cred: Dict[str, Any], timeout: int = 30) -> str:
    """Gọi thử một câu ngắn để kiểm tra key và tên model trước khi lưu"""
    reply = complete(cred, "Trả lời đúng một từ: OK", timeout=timeout)
    return reply.strip()[:60]
