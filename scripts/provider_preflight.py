from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def compact_error(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http": response.status_code, "body": response.text[:600]}
    err = payload.get("error", payload) if isinstance(payload, dict) else payload
    if not isinstance(err, dict):
        return {"http": response.status_code, "body": str(err)[:600]}
    return {
        "http": response.status_code,
        "code": err.get("code"),
        "status": err.get("status"),
        "message": err.get("message"),
        "details": err.get("details"),
    }


def call_generate(key: str, model: str) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with exactly OK."}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8},
    }
    try:
        response = requests.post(url, params={"key": key}, json=body, timeout=20)
    except requests.RequestException as exc:
        return {"capability": "inference", "model": model, "ok": False, "network_error": type(exc).__name__}
    if response.status_code >= 400:
        return {"capability": "inference", "model": model, "ok": False, **compact_error(response)}
    return {"capability": "inference", "model": model, "ok": True, "http": response.status_code}


def call_search(key: str, model: str) -> dict[str, Any]:
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model,
        "input": "Search the web for the official OpenAI homepage and answer with only its domain.",
        "tools": [{"type": "google_search"}],
        "response_format": {"type": "text"},
    }
    try:
        response = requests.post(url, params={"key": key}, json=body, timeout=30)
    except requests.RequestException as exc:
        return {"capability": "google_search", "model": model, "ok": False, "network_error": type(exc).__name__}
    if response.status_code >= 400:
        return {"capability": "google_search", "model": model, "ok": False, **compact_error(response)}
    return {"capability": "google_search", "model": model, "ok": True, "http": response.status_code}


def classify(results: list[dict[str, Any]]) -> str:
    inference = [r for r in results if r["capability"] == "inference"]
    search = [r for r in results if r["capability"] == "google_search"]
    if any(r.get("ok") for r in inference) and any(r.get("ok") for r in search):
        return "healthy"
    if any(r.get("ok") for r in inference) and search and all(not r.get("ok") for r in search):
        return "search_grounding_unavailable"
    if inference and all(not r.get("ok") for r in inference) and all(r.get("http") == 429 for r in inference):
        return "project_quota_or_billing_mapping_blocked"
    if any(r.get("http") in {401, 403} for r in results):
        return "authentication_or_project_access_error"
    return "provider_degraded"


def main() -> int:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print(json.dumps({"provider": "gemini", "status": "missing_api_key"}))
        return 2
    inference_models = [
        os.getenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite"),
        os.getenv("GEMINI_IR_MODEL", "gemini-3.6-flash"),
    ]
    search_models = [os.getenv("GEMINI_IR_MODEL", "gemini-3.6-flash")]
    results = [call_generate(key, m) for m in dict.fromkeys(inference_models)]
    results += [call_search(key, m) for m in dict.fromkeys(search_models)]
    output = {"provider": "gemini", "status": classify(results), "results": results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    # Diagnostic workflow should finish successfully even when the provider is unhealthy.
    return 0


if __name__ == "__main__":
    sys.exit(main())
