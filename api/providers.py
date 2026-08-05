"""
LLM provider layer. One entry point — call_llm(prompt, log) — that dispatches to
whichever provider is configured in the secrets store. Returns raw text; the
agent parses + guardrails it. Each provider degrades to a clear error (never a
crash) so the agent can fall back to the heuristic.

Providers:
  • openai    — raw HTTPS to /v1/chat/completions (no SDK dependency)
  • bedrock   — boto3 bedrock-runtime invoke_model (SigV4 handled by botocore)
  • anthropic — anthropic SDK if installed, else raw HTTPS to the Messages API
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from . import secrets_store


class ProviderError(Exception):
    pass


def active_provider() -> str:
    return secrets_store.load().get("provider", "heuristic")


def call_llm(prompt: str, log, json_mode: bool = True) -> dict:
    """json_mode=True forces JSON output (mapping agent). Set False for free-form
    text/code (pixel-clone agent) — otherwise OpenAI rejects the request.

    Returns {"text": str, "usage": {"provider", "model", "prompt_tokens",
    "completion_tokens", "total_tokens"}} — token counts are None where a
    provider/model doesn't report them, never fabricated."""
    cfg = secrets_store.load()
    prov = cfg.get("provider", "heuristic")
    if prov == "openai":
        return _openai(cfg["openai"], prompt, log, json_mode)
    if prov == "bedrock":
        return _bedrock(cfg["bedrock"], prompt, log)
    if prov == "anthropic":
        return _anthropic(cfg["anthropic"], prompt, log)
    raise ProviderError("no LLM provider configured")


# ------------------------------------------------------------- OpenAI ----

def _openai(c, prompt, log, json_mode=True):
    key = c.get("api_key")
    if not key:
        raise ProviderError("OpenAI API key not set")
    model = c.get("model") or "gpt-4o"
    log("info", f"AI stage: OpenAI {model}")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"OpenAI HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}")
    except Exception as e:
        raise ProviderError(f"OpenAI call failed: {e}")
    u = data.get("usage") or {}
    return {"text": data["choices"][0]["message"]["content"],
            "usage": {"provider": "openai", "model": model,
                      "prompt_tokens": u.get("prompt_tokens"),
                      "completion_tokens": u.get("completion_tokens"),
                      "total_tokens": u.get("total_tokens")}}


# ------------------------------------------------------------- Bedrock ----

def _bedrock(c, prompt, log):
    ak, sk = c.get("access_key_id"), c.get("secret_access_key")
    if not (ak and sk):
        raise ProviderError("Bedrock credentials not set")
    region = c.get("region") or "us-east-1"
    model = c.get("model") or "anthropic.claude-3-5-sonnet-20240620-v1:0"
    log("info", f"AI stage: Bedrock {model} ({region})")
    try:
        import boto3
    except ImportError:
        raise ProviderError("boto3 not installed for Bedrock")
    try:
        client = boto3.client(
            "bedrock-runtime", region_name=region,
            aws_access_key_id=ak, aws_secret_access_key=sk,
        )
        if model.startswith("anthropic."):
            payload = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:  # Titan / other text models
            payload = {"inputText": prompt, "textGenerationConfig": {"maxTokenCount": 1200}}
        resp = client.invoke_model(modelId=model, body=json.dumps(payload))
        out = json.loads(resp["body"].read())
        if model.startswith("anthropic."):
            text = "".join(b.get("text", "") for b in out.get("content", []))
            u = out.get("usage") or {}
            pt, ct = u.get("input_tokens"), u.get("output_tokens")
        else:  # Titan / other text models
            result0 = out.get("results", [{}])[0]
            text = result0.get("outputText", "")
            pt, ct = out.get("inputTextTokenCount"), result0.get("tokenCount")
        return {"text": text, "usage": {"provider": "bedrock", "model": model,
                "prompt_tokens": pt, "completion_tokens": ct,
                "total_tokens": (pt + ct) if pt is not None and ct is not None else None}}
    except Exception as e:
        raise ProviderError(f"Bedrock call failed: {e}")


# ------------------------------------------------------------- Anthropic ----

def _anthropic(c, prompt, log):
    key = c.get("api_key")
    if not key:
        raise ProviderError("Anthropic API key not set")
    model = c.get("model") or "claude-sonnet-5"
    log("info", f"AI stage: Anthropic {model}")
    body = json.dumps({
        "model": model, "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"Anthropic HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}")
    except Exception as e:
        raise ProviderError(f"Anthropic call failed: {e}")
    u = data.get("usage") or {}
    pt, ct = u.get("input_tokens"), u.get("output_tokens")
    return {"text": "".join(b.get("text", "") for b in data.get("content", [])),
            "usage": {"provider": "anthropic", "model": model,
                      "prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": (pt + ct) if pt is not None and ct is not None else None}}
