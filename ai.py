"""Talking to the AI chosen in Preferences: Ollama (local), Gemini or Groq (free tiers), or Claude."""
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
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


# ---------------------------------------------------------------- Claude Code: the student's own Claude plan, no API key
CC_PATHS = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
            "~/.npm-global/bin/claude", "~/.bun/bin/claude")
CC_MODELS = {"sonnet": "Claude Sonnet", "opus": "Claude Opus", "haiku": "Claude Haiku"}
API_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
CC_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"


def _cc_env():
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # with a key set, Claude Code would bill the API instead of the Claude plan
    extra = [os.path.expanduser(p) for p in ("~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin")]
    env["PATH"] = os.pathsep.join(extra + ["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    return env


def find_claude_code():
    found = shutil.which("claude", path=_cc_env()["PATH"])
    if found:
        return found
    for p in CC_PATHS:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def claude_code_info():
    exe = find_claude_code()
    info = {"installed": bool(exe), "path": exe.replace(os.path.expanduser("~"), "~") if exe else "", "version": "",
            "install": CC_INSTALL}
    if exe:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, env=_cc_env())
            info["version"] = (r.stdout or r.stderr).strip().split("\n")[0][:60]
        except Exception:
            pass
    return info


def claude_code_ask(prompt, model="sonnet", timeout=900):
    """Run Claude Code once, non-interactively, signed in with the student's Claude plan.
    The prompt goes in on stdin; Claude Code runs in an empty folder with its file and web tools turned off,
    so it only writes text back."""
    exe = find_claude_code()
    if not exe:
        raise RuntimeError("Claude Code isn't installed. See Preferences › AI › Class notes.")
    instruction = ("The input is a complete writing task. Follow its instructions exactly and reply with only what it "
                   "asks for, with no preamble. Don't use any tools.")
    cmd = [exe, "-p", instruction, "--output-format", "json", "--model", model if model in CC_MODELS else "sonnet",
           "--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task,Agent,Read,Glob,Grep"]
    with tempfile.TemporaryDirectory(prefix="densem-claude-") as work:
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=work,
                               env=_cc_env())
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude Code took too long to answer.") from None
    out = (r.stdout or "").strip()
    data = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if data is None and out.startswith("{"):
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    text = str((data or {}).get("result") or "") if data else out
    failed = r.returncode != 0 or bool((data or {}).get("is_error"))
    if failed:
        low = (text + " " + (r.stderr or "")).lower()
        if "log in" in low or "login" in low or "not logged" in low or "authenticat" in low or "/login" in low:
            raise RuntimeError("Claude Code isn't signed in. Open Terminal, type claude, press Return and sign in "
                               "with your Claude account, then try again.")
        if "limit" in low and ("usage" in low or "reached" in low or "rate" in low):
            raise RuntimeError("Your Claude plan's usage limit is reached for now. Try again later.")
        raise RuntimeError("Claude Code couldn't write it: " + (text or r.stderr or "unknown error").strip()[:300])
    if not text.strip():
        raise RuntimeError("Claude Code answered with nothing.")
    return text.strip()


def lesson_ai(cfg):
    """Which AI writes class notes: 'claude_code', 'claude_api' or 'same' (the AI used for everything else)."""
    v = cfg.get("lesson_ai") or "same"
    return v if v in ("claude_code", "claude_api", "same") else "same"


def lesson_ready(cfg):
    w = lesson_ai(cfg)
    if w == "claude_code":
        return bool(find_claude_code())
    if w == "claude_api":
        return bool(cfg.get("api_key"))
    return ai_ready(cfg)


def lesson_name(cfg):
    w, m = lesson_ai(cfg), cfg.get("lesson_model") or "sonnet"
    if w == "claude_code":
        return f"{CC_MODELS.get(m, 'Claude')} (Claude Code)"
    if w == "claude_api":
        return CC_MODELS.get(m, "Claude")
    return NAMES.get(cfg.get("provider"), "The AI")


def lesson_budget(cfg):
    """How many words of transcript the class-notes AI can take."""
    w = lesson_ai(cfg)
    if w in ("claude_code", "claude_api"):
        return 40000
    return {"ollama": 6000, "groq": 3500, "gemini": 40000, "claude": 40000}.get(cfg.get("provider"), 6000)


def ask_lesson(cfg, prompt, max_tokens=4000):
    w, m = lesson_ai(cfg), cfg.get("lesson_model") or "sonnet"
    if w == "claude_code":
        return claude_code_ask(prompt, m)
    if w == "claude_api":
        return ask_ai({**cfg, "provider": "claude", "claude_model": API_MODELS.get(m, API_MODELS["sonnet"])}, prompt,
                      max_tokens, long=True)
    return ask_ai(cfg, prompt, max_tokens, long=True)
