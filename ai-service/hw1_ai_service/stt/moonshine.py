"""Moonshine wrapper (primary engine per the plan).

Verified against moonshine-voice 0.1.1 (Aug 2026, moonshine-ai/moonshine):
  - Models are NOT bundled and do NOT auto-download from the library API.
    Fetch once with the package's CLI:
        moonshine-voice download --stt --language en
    (it prints the downloaded model path + arch)
  - Transcriber(model_path=<directory>, model_arch=<ModelArch>, ...)
  - Batch decode: transcribe_without_streaming(audio_f32, sample_rate)
    -> Transcript with .lines[].text
  - English models are MIT; non-English are non-commercial-licensed.

Config `stt.model` accepts either:
  - a language code (e.g. "en") -> resolved via get_model_for_language()
    against the downloaded model store, or
  - a model DIRECTORY path (the one the downloader printed).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("stt.moonshine")

_DOWNLOAD_HINT = ("run `moonshine-voice download --stt --language en` once, "
                  "then set stt.model to 'en' (or to the printed model path)")


class MoonshineSTT:
    def __init__(self, model: str) -> None:
        try:
            import moonshine_voice  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "stt.engine=moonshine but the 'moonshine-voice' package is not "
                "installed (pip install moonshine-voice)") from exc
        self._mv = moonshine_voice
        self.threads = 0
        self._transcriber = self._load(model or "en")
        log.info("moonshine ready (%s)", model or "en")

    # -- loading -----------------------------------------------------------

    def _load(self, spec: str):
        path = Path(os.path.expanduser(spec))
        if path.is_dir():
            return self._from_dir(path)
        if len(spec) <= 5 and "/" not in spec:
            return self._from_language(spec)
        raise RuntimeError(
            f"stt.model {spec!r} is neither an existing model directory nor a "
            f"language code — {_DOWNLOAD_HINT}")

    def _from_language(self, lang: str):
        getter = getattr(self._mv, "get_model_for_language", None)
        if getter is None:
            raise RuntimeError(
                f"this moonshine-voice version has no get_model_for_language; "
                f"{_DOWNLOAD_HINT}")
        try:
            found = getter(lang)
        except Exception as exc:
            raise RuntimeError(
                f"no downloaded moonshine model for language {lang!r} "
                f"({exc}) — {_DOWNLOAD_HINT}") from exc
        return self._build(found)

    def _build(self, found):
        mv = self._mv
        if isinstance(found, mv.Transcriber):
            return found
        if isinstance(found, (tuple, list)) and len(found) >= 2:
            return mv.Transcriber(model_path=str(found[0]), model_arch=found[1])
        for path_attr in ("model_path", "path"):
            path = getattr(found, path_attr, None)
            if path:
                arch = getattr(found, "model_arch", None) or getattr(found, "arch", None)
                kwargs = {"model_arch": arch} if arch is not None else {}
                return mv.Transcriber(model_path=str(path), **kwargs)
        if isinstance(found, (str, Path)):
            return self._from_dir(Path(found))
        raise RuntimeError(
            f"unrecognized get_model_for_language() return type {type(found)!r} "
            f"— point stt.model at the model directory instead")

    def _from_dir(self, path: Path):
        kwargs = {}
        # Scan the FULL path for the tier name — real downloads land in
        # .../model/medium-streaming-en/quantized_26_07_30, where the final
        # directory name says nothing about the architecture.
        arch = self._infer_arch(str(path))
        if arch is not None:
            kwargs["model_arch"] = arch
        return self._mv.Transcriber(model_path=str(path), **kwargs)

    def _infer_arch(self, path_text: str):
        """Map a model path to a ModelArch enum member, tolerantly
        (enum member naming has shifted between releases)."""
        model_arch = getattr(self._mv, "ModelArch", None)
        if model_arch is None:
            return None
        lname = path_text.lower()
        for tier in ("tiny", "small", "medium", "base"):
            if tier in lname:
                for candidate in (f"{tier.capitalize()}Streaming",
                                  f"{tier.upper()}_STREAMING",
                                  tier.upper(), tier.capitalize()):
                    arch = getattr(model_arch, candidate, None)
                    if arch is not None:
                        return arch
        return None

    # -- inference ---------------------------------------------------------

    def transcribe(self, pcm: bytes, rate: int) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            transcript = self._transcriber.transcribe_without_streaming(
                audio, sample_rate=rate)
        except TypeError:
            # Some builds ctypes-convert strictly from sequences
            transcript = self._transcriber.transcribe_without_streaming(
                audio.tolist(), sample_rate=rate)
        lines = getattr(transcript, "lines", None)
        if lines is None:
            return str(transcript).strip()
        return " ".join(
            line.text.strip() for line in lines if getattr(line, "text", "")
        ).strip()

    def set_threads(self, n: int) -> None:
        # Thread caps apply at session creation in current builds; record the
        # intent. Best-effort by contract.
        self.threads = n
