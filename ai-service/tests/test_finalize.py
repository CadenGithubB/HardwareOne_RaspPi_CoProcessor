"""Tests for stop()-erasure finalization and the offline replay bench.

Anchored on the real 2026-08-11 defect (exchange 1556bbff0000000a): moonshine
stop() returned two lines with the SECOND erased to "" while the running
partials held the full 'One two three four Five, six, seven, eight, nine, ten'.
The old finalization shipped only 'One two three four'.
"""

import json

from hw1_ai_service.stt.live import finalize_transcript
from hw1_ai_service.stt import replay


FULL = "One two three four Five, six, seven, eight, nine, ten"
STOP = "One two three four"


def _p(t, text, complete=True, line_id=1):
    return {"t": t, "line_id": line_id, "line_complete": complete, "text": text}


# --- finalize_transcript: the erasure class -------------------------------

def test_erasure_rescue_recovers_full_when_full_is_complete():
    # The full hypothesis was committed (complete), then stop() erased line 2,
    # which also lands in history as a later, shorter complete entry.
    partials = [_p(0.6, "One two three"), _p(0.9, FULL),
                _p(1.2, STOP)]  # <- the erasure, later + shorter
    new = finalize_transcript(STOP, partials, legacy=False)
    old = finalize_transcript(STOP, partials, legacy=True)
    assert new["text"] == FULL
    assert new["mode"] == "erasure_rescue"
    assert old["text"] == STOP  # pre-fix behaviour preserved
    assert old["mode"] == "stop"


def test_erasure_rescue_recovers_full_when_full_only_in_progress():
    # Defensive: if the full hypothesis was never marked complete but the
    # erased-short one was, we must still recover the full via the any-fallback.
    partials = [_p(0.9, FULL, complete=False), _p(1.2, STOP, complete=True)]
    new = finalize_transcript(STOP, partials, legacy=False)
    assert new["text"] == FULL
    assert new["mode"] == "erasure_rescue"


def test_erasure_rescue_is_punctuation_insensitive():
    stop = "One two three four"
    partials = [_p(1.0, "One, two, three, four! Five six.")]
    new = finalize_transcript(stop, partials, legacy=False)
    assert new["text"] == "One, two, three, four! Five six."


# --- finalize_transcript: guards (do no harm) -----------------------------

def test_divergent_partial_never_clobbers_a_clean_stop():
    partials = [_p(1.0, "a dog ran somewhere")]  # not a prefix of the stop
    new = finalize_transcript("the cat sat here", partials, legacy=False)
    assert new["text"] == "the cat sat here"
    assert new["mode"] == "stop"


def test_equal_or_shorter_partial_does_not_replace_stop():
    partials = [_p(1.0, "hello world")]
    new = finalize_transcript("hello world", partials, legacy=False)
    assert new["text"] == "hello world"
    assert new["mode"] == "stop"


def test_no_partials_leaves_stop_untouched():
    new = finalize_transcript("hello world", [], legacy=False)
    assert new == {"text": "hello world", "rescued_from_t": None, "mode": "stop"}


# --- finalize_transcript: FULL erasure of a never-complete hypothesis ------
# Field case 2026-08-11 13:14 (exchange 82f0f82e00000001): 9.09s of "one
# through ten", 6 running updates NONE marked complete, stop() returned one
# line erased to "" — complete-only rescue had nothing, shipped ''.

TEN = "One, two, three, four, five, six, seven, eight, nine, ten."


def test_full_erasure_of_incomplete_hypothesis_is_rescued():
    partials = [
        _p(1.5, "One, two,", complete=False),
        _p(3.0, "One, two, three, four, five,", complete=False),
        _p(8.5, TEN, complete=False),
        _p(9.2, "", complete=False),  # stop-time eraser: t >= input end
    ]
    new = finalize_transcript("", partials, input_ended_t=9.09)
    old = finalize_transcript("", partials, input_ended_t=9.09, legacy=True)
    assert new["text"] == TEN
    assert new["mode"] == "empty_rescue_incomplete"
    assert old["text"] == ""  # pre-fix behaviour preserved for the bench


def test_full_erasure_without_recorded_eraser_is_rescued():
    # The erasing update may never land in history (dropped/absent): the
    # hypothesis simply stood until stop returned empty.
    partials = [_p(8.5, TEN, complete=False)]
    new = finalize_transcript("", partials, input_ended_t=9.09)
    assert new["text"] == TEN
    assert new["mode"] == "empty_rescue_incomplete"


def test_mid_audio_retraction_is_not_rescued():
    # The model retracted WHILE audio still flowed (silence-hallucination
    # cleanup): trust the retraction.
    partials = [_p(2.0, "Thank you.", complete=False),
                _p(3.0, "", complete=False)]
    res = finalize_transcript("", partials, input_ended_t=9.0)
    assert res["text"] == ""
    assert res["mode"] == "empty"


def test_incomplete_rescue_requires_input_ended_t():
    partials = [_p(8.5, TEN, complete=False)]
    res = finalize_transcript("", partials)  # no timing info: conservative
    assert res["text"] == ""
    assert res["mode"] == "empty"


def test_complete_rescue_preferred_over_incomplete():
    partials = [_p(5.0, "the committed part", complete=True),
                _p(8.5, TEN, complete=False)]
    res = finalize_transcript("", partials, input_ended_t=9.09)
    assert res["mode"] == "empty_rescue"
    assert res["text"] == "the committed part"


def test_eraser_with_missing_timestamp_blocks_rescue():
    partials = [_p(8.5, TEN, complete=False),
                {"t": None, "line_id": 1, "line_complete": False, "text": ""}]
    res = finalize_transcript("", partials, input_ended_t=9.09)
    assert res["text"] == ""  # can't prove it was stop-time: stay conservative


# --- finalize_transcript: empty-stop rescue (unchanged behaviour) ----------

def test_empty_stop_rescue_active_in_both_modes():
    partials = [_p(0.8, "the whole thing")]
    for legacy in (True, False):
        res = finalize_transcript("", partials, legacy=legacy)
        assert res["text"] == "the whole thing"
        assert res["mode"] == "empty_rescue"


def test_no_speech_stays_empty():
    for legacy in (True, False):
        res = finalize_transcript("", [], legacy=legacy)
        assert res["text"] == ""
        assert res["mode"] == "empty"


# --- replay bench --------------------------------------------------------

def _make_snapshot(stop_lines, partials):
    return {"snapshot": {"stream": {"stop_lines": stop_lines,
                                    "partials": partials}}}


def test_stop_text_reconstructed_from_erased_lines():
    stream = {"stop_lines": [
        {"line_id": 1, "start_time": 0.0, "complete": True, "text": STOP},
        {"line_id": 2, "start_time": 1.0, "complete": True, "text": ""},
    ]}
    assert replay.stop_text_from_snapshot(stream) == STOP


def test_word_recall_measures_partial_vs_full():
    assert replay.word_recall(STOP, FULL) == 0.4          # 4 of 10 words
    assert replay.word_recall(FULL, FULL) == 1.0
    assert replay.word_recall("", FULL) == 0.0


def test_replay_finalize_round_trip(tmp_path, capsys):
    # A captured sample identical in shape to the real 000a erasure.
    sample = _make_snapshot(
        stop_lines=[
            {"line_id": 1, "start_time": 0.0, "complete": True, "text": STOP},
            {"line_id": 2, "start_time": 1.0, "complete": True, "text": ""},
        ],
        partials=[_p(0.9, FULL), _p(1.2, STOP)])
    (tmp_path / "000a.json").write_text(json.dumps(sample))
    (tmp_path / "000a.expected.txt").write_text(FULL)

    rc = replay.replay_finalize(tmp_path, expected_text=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1/1 sample(s) changed" in out
    assert "NEW final: 'One two three four Five, six, seven, eight, nine, ten'" \
        in out
    assert "recall   : OLD 0.40  ->  NEW 1.00" in out
