"""Talking to the AI chosen in Preferences: Ollama (local), Gemini or Groq (free tiers), or Claude."""
import json
import re
import ssl
import time
import urllib.error
import urllib.request

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    SSL_CTX = ssl.create_default_context()

OLLAMA = "http://127.0.0.1:11434"
NAMES = {"ollama": "Ollama", "gemini": "Gemini", "groq": "Groq", "claude": "Claude"}
OPENAI_STYLE = {  # free-tier cloud services with an OpenAI-compatible API
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini_key", "gemini_model"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "groq_key", "groq_model"),
}


class Offline(Exception):
    pass


def ai_ready(cfg):
    p = cfg.get("provider")
    if p == "ollama":
        return bool(cfg["ollama_model"])
    if p == "claude":
        return bool(cfg["api_key"])
    if p in OPENAI_STYLE:
        return bool(cfg[OPENAI_STYLE[p][1]])
    return False


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a : b + 1])


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json", **headers}
    )
    ctx = SSL_CTX if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.load(r)


def ask_ai(cfg, prompt, max_tokens=700, json_mode=False, long=False):
    """Send one prompt to the AI chosen in Preferences and return its text.

    long=True is for big jobs (building a whole lesson): bigger context and timeouts.
    """
    p = cfg["provider"]
    name = NAMES.get(p, p)
    messages = [{"role": "user", "content": prompt}]
    try:
        if p == "ollama":
            body = {
                "model": cfg["ollama_model"],
                "messages": messages,
                "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": 0.3, "num_ctx": 32768 if long else 8192, "num_predict": max_tokens * 2},
            }
            if json_mode:
                body["format"] = "json"
            text = _post(OLLAMA + "/api/chat", body, {}, 1200 if long else 240).get("message", {}).get("content", "")
        elif p == "claude":
            data = _post(
                "https://api.anthropic.com/v1/messages",
                {"model": cfg["claude_model"], "max_tokens": max_tokens, "messages": messages},
                {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"},
                300 if long else 60,
            )
            text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
        elif p in OPENAI_STYLE:
            url, key_field, model_field = OPENAI_STYLE[p]
            data = _post(
                url,
                {"model": cfg[model_field], "messages": messages, "max_tokens": max(2048, max_tokens), "temperature": 0.3},
                {"Authorization": "Bearer " + cfg[key_field]},
                300 if long else 60,
            )
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        else:
            raise RuntimeError("No AI is selected.")
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
            err = err[0] if isinstance(err, list) else err
            msg = err.get("error")
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except Exception:
            msg = None
        if e.code in (401, 403):
            raise RuntimeError(f"{name} rejected the API key. Check it in Preferences.")
        if e.code == 429:
            raise RuntimeError(f"{name} free limit reached for now. Using offline summaries until it resets.")
        if e.code == 404 and p == "ollama":
            raise RuntimeError(f'The Ollama model "{cfg["ollama_model"]}" isn\'t downloaded. Download it in Preferences.')
        raise RuntimeError(f"{name} error: {msg or 'HTTP ' + str(e.code)}")
    except (urllib.error.URLError, TimeoutError, OSError):
        if p == "ollama":
            raise Offline("Ollama isn't running. Open the Ollama app; using offline summaries meanwhile.")
        raise Offline("No internet: using offline summaries until it's back.")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def ollama_info():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            data = json.load(r)
        return {"running": True, "models": sorted(m["name"] for m in data.get("models", []))}
    except Exception:
        return {"running": False, "models": []}


def ollama_pull(hub, model):
    """Download an Ollama model, reporting progress to the window."""
    req = urllib.request.Request(
        OLLAMA + "/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            last = 0.0
            for line in r:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    hub.emit("pull", {"model": model, "state": "error", "text": d["error"]})
                    return
                pct = round(100 * d["completed"] / d["total"]) if d.get("total") and d.get("completed") else None
                if time.time() - last > 0.5:
                    last = time.time()
                    hub.emit("pull", {"model": model, "state": "progress", "text": d.get("status", ""), "pct": pct})
        hub.emit("pull", {"model": model, "state": "done", "text": "Downloaded"})
    except Exception as e:
        hub.emit("pull", {"model": model, "state": "error", "text": f"Download failed: {e}"})
