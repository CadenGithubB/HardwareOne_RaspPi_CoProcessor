"""sherpa-onnx streaming Zipformer wrapper (fallback engine per the plan).

pip install sherpa-onnx. `model` in config points at the unpacked model
directory (encoder/decoder/joiner .onnx + tokens.txt), e.g. the
sherpa-onnx-streaming-zipformer-en-2023-06-26 int8 release.

Streaming-capable by design; P0 uses it batch-style (feed all, drain).
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("stt.zipformer")


def _one(d: Path, pattern: str) -> Path:
    """Exactly one file matching pattern in the model dir, or a clear error.

    (Was missing entirely — ZipformerSTT crashed with NameError on
    construction; caught by the run_checks.sh F821 gate, 2026-08-11.)"""
    matches = sorted(d.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(
            f"zipformer model dir must contain exactly one {pattern!r} "
            f"(found {len(matches)} in {d})")
    return matches[0]


class ZipformerSTT:
    def __init__(self, model_dir: str, threads: int = 2) -> None:
        try:
            import sherpa_onnx  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "stt.engine=zipformer but 'sherpa-onnx' is not installed "
                "(pip install sherpa-onnx)") from exc
        d = Path(model_dir).expanduser()
        if not d.is_dir():
            raise RuntimeError(f"zipformer model dir not found: {d}")
        self._sherpa = sherpa_onnx
        self.threads = threads
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(_one(d, "tokens*.txt")),
            encoder=str(_one(d, "encoder*.onnx")),
            decoder=str(_one(d, "decoder*.onnx")),
            joiner=str(_one(d, "joiner*.onnx")),
            num_threads=threads,
            sample_rate=16000,
            feature_dim=80,
        )
        log.info("zipformer ready: %s (threads=%d)", d.name, threads)

    def transcribe(self, pcm: bytes, rate: int) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(rate, audio)
        # Flush the decoder with a tail of silence, then signal end-of-input.
        stream.accept_waveform(rate, np.zeros(int(rate * 0.6), dtype=np.float32))
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return self._recognizer.get_result(stream).strip()

    def set_threads(self, n: int) -> None:
        # num_threads is fixed at recognizer construction in sherpa-onnx;
        # a rebuild mid-flight is not worth it. Best-effort by contract.
        self.threads = n
