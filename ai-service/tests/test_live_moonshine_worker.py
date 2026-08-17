from __future__ import annotations

from dataclasses import dataclass
import threading

from hw1_ai_service.stt.live import LiveMoonshineWorker


@dataclass
class _Line:
    text: str
    line_id: int = 1
    is_complete: bool = False
    start_time: float = 0.0


@dataclass
class _Transcript:
    lines: list[_Line]


@dataclass
class _Event:
    line: _Line


class _Stream:
    def __init__(self, owner) -> None:
        self.owner = owner
        self.listener = None

    def _call(self, name: str) -> None:
        self.owner.calls.append(name)
        self.owner.thread_ids.append(threading.get_ident())

    def add_listener(self, listener) -> None:
        self._call("add_listener")
        self.listener = listener

    def start(self) -> None:
        self._call("start")

    def add_audio(self, audio, sample_rate=16_000) -> None:
        self._call("add_audio")
        self.owner.chunk_samples.append(len(audio))
        self.owner.sample_rates.append(sample_rate)
        self.owner.add_started.set()
        if self.owner.add_release is not None:
            self.owner.add_release.wait(2.0)
        assert self.listener is not None
        self.listener(_Event(_Line(
            self.owner.partial_text,
            is_complete=self.owner.partial_complete)))

    def stop(self):
        self._call("stop")
        if self.owner.stop_none:
            return None
        if self.owner.erase_on_stop:
            # Reproduces the 2026-08-11 artifact: a "" update lands on the
            # completed line at stop time, and the stop transcript carries
            # the erased line.
            assert self.listener is not None
            self.listener(_Event(_Line("", is_complete=True)))
        return _Transcript([_Line(self.owner.final_text, is_complete=True)])

    def close(self) -> None:
        self._call("stream_close")


class _Transcriber:
    def __init__(self, owner) -> None:
        self.owner = owner
        self._hw1_live_identity = {"model": "fake"}

    def create_stream(self, update_interval=None):
        self.owner.calls.append("create_stream")
        self.owner.thread_ids.append(threading.get_ident())
        self.owner.update_intervals.append(update_interval)
        return _Stream(self.owner)

    def close(self) -> None:
        self.owner.calls.append("transcriber_close")
        self.owner.thread_ids.append(threading.get_ident())


class _Factory:
    def __init__(self, *, final_text="hello world", partial_text="hello",
                 partial_complete=False, erase_on_stop=False,
                 stop_none=False, block_add=False) -> None:
        self.final_text = final_text
        self.partial_text = partial_text
        self.partial_complete = partial_complete
        self.erase_on_stop = erase_on_stop
        self.stop_none = stop_none
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.chunk_samples: list[int] = []
        self.sample_rates: list[int] = []
        self.update_intervals: list[float] = []
        self.add_started = threading.Event()
        self.add_release = threading.Event() if block_add else None

    def __call__(self):
        self.calls.append("factory")
        self.thread_ids.append(threading.get_ident())
        return _Transcriber(self)


def _begin(worker: LiveMoonshineWorker) -> None:
    worker.on_begin({"sample_rate": 16_000})


def test_worker_coalesces_physical_frames_and_owns_all_native_calls():
    factory = _Factory()
    worker = LiveMoonshineWorker(
        factory, update_interval_s=1.0, queue_chunks=8)
    main_thread = threading.get_ident()
    worker.start(2.0)
    _begin(worker)

    assert worker.offer_pcm(b"\x01\x02" * 500)
    assert worker.offer_pcm(b"\x03\x04" * 500)
    assert worker.offer_pcm(b"\x05\x06" * 500)
    assert worker.offer_pcm(b"\x07\x08" * 500)
    assert worker.offer_pcm(b"\x09\x0a" * 500)
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"], result
    assert result["valid_empty"] is False
    assert result["text"] == "hello world"
    assert result["audio"] == {
        "offered_bytes": 5000,
        "enqueued_bytes": 5000,
        "processed_bytes": 5000,
    }
    assert factory.chunk_samples == [2048, 452]
    assert factory.sample_rates == [16_000, 16_000]
    assert factory.update_intervals == [1.0]
    assert set(factory.thread_ids).__len__() == 1
    assert factory.thread_ids[0] != main_thread
    assert factory.calls[0] == "factory"
    assert factory.calls[-2:] == ["stream_close", "transcriber_close"]
    assert result["queue"]["capacity_chunks"] == 8
    assert result["queue"]["capacity_bytes"] == 32 * 1024
    assert result["queue"]["capacity_ms"] == 1024
    # Hypothesis history: every changed partial recorded with its trigger
    # line; the stop() transcript's line structure recorded separately.
    partials = result["stream"]["partials"]
    assert partials, "hypothesis history must not be empty"
    assert all(p["text"] for p in partials)
    assert partials[-1]["line_id"] == 1
    assert result["stream"]["partials_dropped"] == 0
    assert result["stream"]["stop_lines"] == [
        {"line_id": 1, "start_time": 0.0, "complete": True,
         "text": "hello world"}]
    assert result["stream"]["live_lines"] and \
        result["stream"]["live_lines"][0]["line_id"] == 1


def test_valid_empty_is_distinct_from_worker_failure():
    worker = LiveMoonshineWorker(_Factory(final_text="", partial_text=""))
    worker.start(2.0)
    _begin(worker)
    assert worker.offer_pcm(b"\x00\x00" * 2048)
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"] is True
    assert result["valid_empty"] is True
    assert result["text"] == ""
    assert result["failure_reasons"] == []
    # No non-empty complete hypothesis ever existed, so nothing to recover:
    # this is genuine no-speech evidence, not an erased final.
    assert result["stream"]["stop_text_empty"] is True
    assert result["stream"]["final_recovered"] is False
    assert result["stream"]["final_recovered_from_t"] is None


def test_stop_erasure_recovers_last_complete_hypothesis():
    question = "What is the capital of France?"
    worker = LiveMoonshineWorker(_Factory(
        final_text="", partial_text=question,
        partial_complete=True, erase_on_stop=True))
    worker.start(2.0)
    _begin(worker)
    assert worker.offer_pcm(b"\x01\x02" * 2048)
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"] is True
    assert result["valid_empty"] is False
    assert result["text"] == question
    assert result["stream"]["stop_text_empty"] is True
    assert result["stream"]["final_recovered"] is True
    assert result["stream"]["final_recovered_from_t"] is not None
    # The erasure evidence itself stays intact in the record.
    assert result["stream"]["partials"][-1]["text"] == ""
    assert result["stream"]["stop_lines"][-1]["text"] == ""


def test_queue_overflow_is_explicit_and_producer_never_waits():
    factory = _Factory(block_add=True)
    worker = LiveMoonshineWorker(factory, queue_chunks=1)
    worker.start(2.0)
    _begin(worker)

    assert worker.offer_pcm(b"\x01\x02" * 2048)
    assert factory.add_started.wait(1.0)
    assert worker.offer_pcm(b"\x03\x04" * 2048)
    assert worker.offer_pcm(b"\x05\x06" * 2048) is False
    factory.add_release.set()
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"] is False
    assert result["queue"]["overflowed"] is True
    assert "audio_queue_overflow" in result["failure_reasons"]
    assert result["audio"]["offered_bytes"] == 3 * 4096
    assert result["audio"]["processed_bytes"] == 2 * 4096


def test_missing_stop_result_is_not_valid_empty():
    worker = LiveMoonshineWorker(_Factory(stop_none=True))
    worker.start(2.0)
    _begin(worker)
    assert worker.offer_pcm(b"\x00\x00" * 2048)
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"] is False
    assert result["valid_empty"] is False
    assert "missing_stop_result" in result["failure_reasons"]


def test_wrong_sample_rate_fails_without_blocking_transport_producer():
    worker = LiveMoonshineWorker(_Factory())
    worker.start(2.0)
    worker.on_begin({"sample_rate": 48_000})
    assert worker.offer_pcm(b"\x00\x00" * 2048) is False
    worker.end_input()
    result = worker.wait(2.0)

    assert result["valid"] is False
    assert "sample_rate:48000!=16000" in result["failure_reasons"]
    assert result["audio"]["offered_bytes"] == 4096
    assert result["audio"]["processed_bytes"] == 0
