"""Speech to text, on your Mac.

Two engines run the same Whisper models:
- "mlx": Whisper on the Apple GPU through MLX (mlx-whisper). Used on Apple Silicon when it's installed.
  Fast enough for the large-v3-turbo model during a live lecture.
- "cpu": faster-whisper on the CPU. Used on Intel Macs and other systems, when MLX isn't installed or
  couldn't load, or when Preferences › General › Speech engine says so.
"""
import importlib.util
import os
import platform
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from store import MODELS

SR = 16000
BLOCK = 1600  # 0.1 s
BOILER = re.compile(r"^(thank you\.?|thanks for watching.*|subtitles by.*|you\.?|\.+)$", re.I)
APPLE = sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")
TURBO = "large-v3-turbo"
TURBO_RAM_GB = 15  # a 16 GB Mac reports a little under 16
# Speech model name (as in Preferences) -> the same model converted for MLX
MLX_REPOS = {
    "base.en": "mlx-community/whisper-base.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
    TURBO: "mlx-community/whisper-large-v3-turbo",
}
WINDOW = 28 * SR  # a recording is read in pieces just under Whisper's 30 s window


def ram_gb():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0


def mlx_available():
    if not APPLE:
        return False
    try:
        return bool(importlib.util.find_spec("mlx_whisper") and importlib.util.find_spec("mlx"))
    except Exception:
        return False


def upgrade_default(cfg):
    """Once, when the Apple GPU engine first becomes available: a Mac with 16 GB or more moves from the old
    default model (small or base) to large-v3-turbo. A model the student picked themselves is left alone, and
    an 8 GB Mac keeps its model (it just runs on the GPU now). Returns True if the settings changed."""
    if cfg.get("stt_upgraded") or not mlx_available():
        return False
    cfg["stt_upgraded"] = True
    if cfg.get("whisper") in ("small.en", "base.en", "small", "base") and ram_gb() >= TURBO_RAM_GB:
        cfg["whisper"] = TURBO
    return True


def quiet_cut(buf, length, window=3 * SR):
    """Cut point inside the last `window` samples of buf[:length], at the quietest 0.1 s."""
    start = max(length // 2, length - window)
    region = buf[start:length]
    frames = len(region) // BLOCK
    if frames < 2:
        return length
    rms = np.sqrt(np.mean(region[: frames * BLOCK].reshape(frames, BLOCK) ** 2, axis=1))
    return start + int(np.argmin(rms)) * BLOCK + BLOCK // 2


def speech_span(audio):
    """(first, last) sample of the speech in `audio`; (0, 0) when nobody is talking.
    Whisper invents words for silence, so the MLX engine only gets the part with speech in it. Uses the
    voice detector that ships with faster-whisper; if that isn't usable, a plain loudness check."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        found = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=400))
        if not found:
            return 0, 0
        return max(0, int(found[0]["start"])), min(len(audio), int(found[-1]["end"]))
    except Exception:
        loud = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        return (0, len(audio)) if loud > 0.003 else (0, 0)


def _mlx_folder(name):
    """The folder of an MLX model, downloading it once into the app's models folder."""
    from huggingface_hub import snapshot_download

    def whole(p):
        return bool(p) and os.path.exists(os.path.join(p, "config.json")) and any(
            os.path.exists(os.path.join(p, w)) for w in ("weights.safetensors", "weights.npz"))

    repo = MLX_REPOS[name]
    try:  # already downloaded: no internet needed
        p = snapshot_download(repo_id=repo, cache_dir=MODELS, local_files_only=True)
        if whole(p):
            return p
    except Exception:
        pass
    p = snapshot_download(repo_id=repo, cache_dir=MODELS)
    if not whole(p):
        raise RuntimeError("the model download is incomplete")
    return p


def _mlx_load(name):
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder

    path = _mlx_folder(name)
    ModelHolder.get_model(path, mx.float16)
    return path


def _mlx_unload():
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.model = ModelHolder.model_path = None
        (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
    except Exception:
        pass


def _mlx_segments(path, audio, prompt, lang):
    """Runs on the MLX thread: the speech in one piece of audio (up to ~30 s) as [(start, end, text)]."""
    import mlx_whisper

    lo, hi = speech_span(audio)
    if hi - lo < SR // 3:
        return []
    out = mlx_whisper.transcribe(
        audio[lo:hi],
        path_or_hf_repo=path,
        verbose=None,
        temperature=(0.0, 0.2, 0.4),
        condition_on_previous_text=False,
        initial_prompt=prompt or None,
        language=lang,
    )
    off, found = lo / SR, []
    for s in out.get("segments") or []:
        text = str(s.get("text", "")).strip()
        if not text or BOILER.match(text):
            continue
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.8:
            continue
        if s.get("compression_ratio", 0) > 2.6:  # the same words over and over: made up, not heard
            continue
        found.append((off + float(s["start"]), off + float(s["end"]), text))
    return found


class Transcriber:
    def __init__(self, hub):
        self.hub = hub
        self.model = None   # the faster-whisper model, or the folder of the MLX model
        self.name = None
        self.engine = None  # the engine actually in use: "mlx" or "cpu"
        self.loaded = None  # what was asked for when the model was loaded: (engine, name)
        self.load_lock = threading.Lock()
        # MLX keeps its GPU state per thread, so everything MLX does (loading, live speech, imports) runs on
        # this one thread, one job at a time.
        self.mlx = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")

    def wanted(self):
        name = self.hub.cfg["whisper"]
        if self.hub.cfg["language"] != "en" and name.endswith(".en"):
            name = name[:-3]
        return name

    def wanted_engine(self):
        if self.hub.cfg.get("stt_engine") == "cpu":
            return "cpu"
        return "mlx" if self.wanted() in MLX_REPOS and mlx_available() else "cpu"

    def key(self):
        return self.wanted_engine(), self.wanted()

    def _say(self, **state):
        self.hub.model = state
        self.hub.emit("model", self.hub.model)

    def load(self):
        with self.load_lock:
            engine, name = want = self.key()
            if self.model is not None and self.loaded == want:
                return True
            self._say(state="loading", name=name, engine=engine)
            model, note = None, ""
            if engine == "mlx":
                try:
                    model = self.mlx.submit(_mlx_load, name).result()
                except Exception as e:
                    traceback.print_exc()
                    note = f"The Apple GPU engine couldn't load this model ({str(e)[:120]}), so the CPU engine is used."
                    engine = "cpu"
            if engine == "cpu":
                if self.engine == "mlx":
                    self.mlx.submit(_mlx_unload).result()
                try:
                    from faster_whisper import WhisperModel

                    model = WhisperModel(name, device="cpu", compute_type="int8", download_root=MODELS,
                                         cpu_threads=min(8, os.cpu_count() or 4))
                except Exception as e:
                    self._say(state="error", name=name, engine=engine, error=str(e)[:300])
                    return False
            self.model, self.name, self.engine, self.loaded = model, name, engine, want
            self._say(state="ready", name=name, engine=engine, **({"note": note} if note else {}))
            return True

    def _lang(self):
        lang = self.hub.cfg["language"]
        return None if lang == "auto" else lang

    def segments(self, audio, prompt):
        """The speech in one piece of audio (up to ~30 s) as [(start, end, text)], times in seconds."""
        if self.engine == "mlx":
            return self.mlx.submit(_mlx_segments, self.model, audio, prompt, self._lang()).result()
        segments, _ = self.model.transcribe(
            audio,
            language=self._lang(),
            beam_size=3,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        out = []
        for s in segments:
            text = s.text.strip()
            if not text or BOILER.match(text):
                continue
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
                continue
            out.append((s.start, s.end, text))
        return out

    def transcribe(self, audio, prompt):
        """Live speech: [(start, text)] for the last few seconds from the microphone."""
        return [(a, text) for a, _, text in self.segments(audio, prompt)]

    def transcribe_file(self, path, prompt, progress=None):
        """A whole recording (audio or video) as [{"t", "text"}]. progress(done_seconds, total_seconds)."""
        if self.engine != "mlx":
            segments, info = self.model.transcribe(path, language=self._lang(), beam_size=3, vad_filter=True,
                                                   condition_on_previous_text=True, initial_prompt=prompt or None)
            total = float(getattr(info, "duration", 0) or 0)
            out = []
            for sg in segments:
                text = sg.text.strip()
                if not text:
                    continue
                out.append({"t": round(sg.start, 1), "text": text})
                if progress:
                    progress(sg.end, total)
            return out
        from faster_whisper.audio import decode_audio

        audio = decode_audio(path, sampling_rate=SR)
        total, out, pos = len(audio) / SR, [], 0
        while pos < len(audio):
            end = min(len(audio), pos + WINDOW)
            if end < len(audio):
                end = pos + quiet_cut(audio[pos:end], end - pos)
            tail = " ".join(x["text"] for x in out[-4:])[-200:]
            for a, _, text in self.segments(audio[pos:end], f"{prompt} {tail}".strip()):
                out.append({"t": round(pos / SR + a, 1), "text": text})
            pos = end
            if progress:
                progress(pos / SR, total)
        return out
