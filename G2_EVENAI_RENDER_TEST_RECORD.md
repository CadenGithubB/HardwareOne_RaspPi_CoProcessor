# G2 EvenAI render test record

**Record date:** 2026-08-09  
**Hardware:** XIAO ESP32-S3 Sense, HardwareOne `0.99.82`; Even G2 right
temple `2.2.7.14`  
**Purpose:** preserve stable trial IDs and comparable numbers for the native
EvenAI question and answer renderer.

This is a measurement record, not a production configuration recommendation.
Its August 9 captures predate the exchange-ID command migration, so historical
text may name untagged `g2evenai ask/reply` commands exactly as they appeared in
those logs. Current firmware deliberately rejects those production mutations;
the maintained probe now reads the active ID, requires `exchange-id-v1`, and
emits `askid/replyid/replypartid/replyendid/exitid`. This grammar change does not
alter the preserved timing evidence.

The most important current result is that the visible cutoff is **not explained
by lost or truncated CM5/XIAO text**. All tested payloads reached the right
temple. Two same-connection `80 -> 40 -> 80` matrices, using 14- and
30-character replies, now show a reversible device-side cadence change when
only `streamSpeed` changes. That establishes that this field controls native
REPLY-to-`STREAM_COMPLETE` cadence—the available protocol proxy for reply
rendering—in the tested state and that 40 is faster than 80. It does **not** yet
establish the power-on default, persistence across a glasses power cycle, an
exact milliseconds-per-character unit, or whether the same relationship is
safe for long ASK/question rendering.

## Measurement conventions

Three different milestones appear in the captures and must not be conflated:

1. **XIAO command OK** is the Pi receiving the result of the UART command. It
   means the XIAO accepted and attempted the command. The original probe called
   this an "ACK," but it is not a glasses acknowledgement.
2. **G2 echo** is the right temple's flag-`0x00` protocol response for the same
   EvenAI command and magic value. It proves the glasses received the message;
   it still does not prove a pixel was painted.
3. **`STREAM_COMPLETE`** is the glasses' native completion event. It is the
   strongest available protocol proxy for wearer-visible completion, although
   it is not a direct observation of pixels.

Durations derived from the XIAO's bracketed monotonic millisecond field are
preferred over serial-receive wall timestamps. Debug lines can be buffered and
their wall timestamps can move relative to the event's actual device time.

## Evidence manifest

| Evidence | Source path | SHA-256 | What it contains |
|---|---|---|---|
| ASK-threshold console capture | `$HOME/.codex/attachments/1e973fed-b2f0-44be-b8b7-f480d7679a9c/pasted-text.txt` | `a8fa03286f83dc426a925014e66c678f05b9909600b3b36c365512d6f5dd441e` | Five wearer-rated 98-character ASK trials |
| Render-A/B console capture | `$HOME/.codex/attachments/1c113bb6-f304-4ac9-a60c-b7593a7ca53b/pasted-text.txt` | `37b31d37b622ce6dcc8dcf589b570e343d6694e7d029be0a60fe4b8a15c37d71` | Six 180-character one-shot/multipart trials |
| Initial XIAO serial capture | `$HOME/.codex/attachments/96d9aaa1-a155-477e-b538-31aa3c9eb4cd/pasted-text.txt` | `8421ac5171bd443ea4fa9e5b905dc7253322ba620d1acf5e99aa22a6f088aff9` | Native TX, G2 echoes, completion events, and device health for Results 1-3 |
| Fetched XIAO A/B log | `/home/$CM5_USER/g2-prefx/evenai-render-ab-20260809-081222.log`, 62,453 bytes | Not separately attached | Protocol markers for all six 180-character A/B sessions |
| 14-character speed-matrix console | `$HOME/.codex/attachments/c5f43925-f432-404b-ad1a-b5d573fbf1d4/pasted-text.txt` | `506bfb43d336c8faa79ada44a92e1c225d1a0c253b08d5aebfebd351b2fd04e9` | Sync/preflight plus the complete 80 -> 40 -> 80 runner output |
| 14-character speed-matrix serial | `$HOME/.codex/attachments/cb9504a7-68ac-4917-bbd2-2c432eb7e612/pasted-text.txt` | `3b7f726ade8cc456d74d9b419641da6a1325dfac3b5def589897822f486a0821` | Raw XIAO TX, G2 responses, CONFIG echoes, and completions; also contains earlier unrelated serial history |
| 30-character speed-matrix console | `$HOME/.codex/attachments/90dadde4-c3c4-43d0-85e5-d296380ce689/pasted-text.txt` | `20b40e86374cf49262d9f8b7f6e9d1e27d9a4d02a695d8acb4472450f5b9f080` | Complete second 80 -> 40 -> 80 runner output |
| 30-character speed-matrix serial | `$HOME/.codex/attachments/d7f34d5f-e1f7-472a-ad4c-4893f2ff7578/pasted-text.txt` | `9c3bf6e0d4fb52ce3ee5cf4b99165b10c08bd2d8f9aca356fb613e0d81a6b3dc` | Raw XIAO TX, G2 responses, CONFIG echoes, and completions for the second matrix |
| Pulled 14-character console log | `.scratch/g2-speed-ab-/speed-ab-14-20260809-085507.log`, 7,035 bytes | `888b60e6dc53d0ad8ced40a475dbbb8ef299d67f23b08ceae26e54078ccd4271` | Canonical runner output and structural verdict for the first speed matrix |
| Pulled 14-character XIAO log | `.scratch/g2-speed-ab-/evenai-speed-ab-20260809-085518.log`, 27,822 bytes | `400014ea52e5fddd0c4acd3bef977d3247f294b07e5a406ae242e574b199b50b` | Canonical native protocol and health evidence for the first speed matrix |
| Pulled 30-character console log | `.scratch/g2-speed-ab-/speed-ab-30-20260809-085507.log`, 6,954 bytes | `678611d3726cfc8e5c4fb4138837b1eecd792b4822c5029e16df4603e743e8bb` | Canonical runner output and structural verdict for the second speed matrix |
| Pulled 30-character XIAO log | `.scratch/g2-speed-ab-/evenai-speed-ab-20260809-085654.log`, 23,628 bytes | `309d2804de89dc5d4bbbcc47947cf82d11bc6968bccab5c0146a6f75971b8523` | Canonical native protocol and health evidence for the second speed matrix |

The captures remain ignored/untracked evidence rather than source-controlled
project data. This record keeps only derived timings, hashes, and non-sensitive
test strings.

## Result 1: 98-character question threshold

Every trial used the same 98-byte ASCII question:

> How quickly can these glasses display this complete recognized question
> before showing the answer?

Each trial used a fresh wake-word session. The wearer marked all five as
**cut**. The runner's scheduling was accurate: requested-versus-actual error was
only +1.1 to +4.1 ms. The native interval is longer than the requested interval
because the glasses usually echoed ASK before the Pi received XIAO command OK,
and `g2evenai reply` then transmitted ANALYSE before its REPLY packet.

| ID | Requested after XIAO ASK OK | Actual command delay | G2 ASK echo to ANALYSE TX (lower-bound opportunity) | G2 ASK echo to REPLY TX | Wearer result | 14-char REPLY echo to `STREAM_COMPLETE` |
|---|---:|---:|---:|---:|---|---:|
| ASK98-T1 | 2,000 ms | 2,001.4 ms | 2,204 ms | 2,356 ms | Cut | 1,099 ms |
| ASK98-T2 | 2,500 ms | 2,501.1 ms | 2,712 ms | 2,863 ms | Cut | 1,074 ms |
| ASK98-T3 | 3,000 ms | 3,001.5 ms | 3,244 ms | 3,396 ms | Cut | 1,097 ms |
| ASK98-T4 | 3,500 ms | 3,503.6 ms | 3,717 ms | 3,868 ms | Cut | 1,074 ms |
| ASK98-T5 | 4,000 ms | 4,004.1 ms | 4,240 ms | 4,392 ms | Cut | 1,075 ms |

The wearer reported that successively more of the question appeared as the
delay increased. That supports **continued time-based progression through at
least 4.24 seconds**, not a character ceiling already reached in the short
trials. It does not rule out a later page/capacity boundary because:

- the delays were strictly ascending, with one observation per condition;
- only `cut`/`complete` was recorded, not the exact last visible word; and
- even the longest trial did not reach completion.

The current Pi barrier nominally allocates `98 / 44 = 2.227` seconds after XIAO
command OK for this question; XIAO OK is not optical render start. The data
proves that nominal budget is unsafe in the current G2 state.

### Host/UART timing detail

| ID | ASK command RTT | REPLY command RTT | Requested-delay error |
|---|---:|---:|---:|
| ASK98-T1 | 250 ms | 552 ms | +1.4 ms |
| ASK98-T2 | 301 ms | 553 ms | +1.1 ms |
| ASK98-T3 | 300 ms | 402 ms | +1.5 ms |
| ASK98-T4 | 300 ms | 551 ms | +3.6 ms |
| ASK98-T5 | 351 ms | 551 ms | +4.1 ms |

These RTTs are operational overhead, not optical rendering time.

## Result 2: 180-character one-shot versus multipart A/B

Every answer used the exact same 180 ASCII characters. One-shot trials sent
one `REPLY{text=180,fTextEnd=1}`. Multipart trials sent 87 and 93 character
`fTextEnd=0` parts followed by a zero-byte `fTextEnd=1` finalizer.

Protocol accounting was exact:

- ASK responses: 6/6;
- ANALYSE responses: 6/6;
- expected REPLY responses: 12/12;
- COMM_RSP/errors: 0; and
- `STREAM_COMPLETE`: 0/6 before the probe forced EXIT.

Thus the visible cutoff was not caused by the Pi clipping the answer, the XIAO
builder truncating it, a missing BLE packet, or the G2 rejecting a command.

| ID | Run order | Mode | Reply payloads | ASK TX to G2 echo | First REPLY TX to forced EXIT TX | Final G2 echo to forced EXIT TX | `STREAM_COMPLETE` |
|---|---:|---|---|---:|---:|---:|---|
| RAB180-T1 | 1 | One-shot 1/3 | 180 final | 50 ms | 12.567 s | 12.524 s | None observed |
| RAB180-T2 | 2 | Multipart 1/3 | 87 + 93 + 0 final | 51 ms | 13.452 s | 12.459 s | None observed |
| RAB180-T3 | 3 | Multipart 2/3 | 87 + 93 + 0 final | 46 ms | 13.185 s | 12.305 s | None observed |
| RAB180-T4 | 4 | One-shot 2/3 | 180 final | **617 ms** | 12.620 s | 12.472 s | None observed |
| RAB180-T5 | 5 | One-shot 3/3 | 180 final | 86 ms | 12.559 s | 12.498 s | None observed |
| RAB180-T6 | 6 | Multipart 3/3 | 87 + 93 + 0 final | 50 ms | 13.307 s | 12.311 s | None observed |

These are **right-censored observations**: no completion was observed before
the probe forced EXIT. They do not provide a measured renderer rate or a valid
one-shot-versus-multipart winner. It is especially important not to divide 180
by those windows and call the result a measured characters-per-second value.

The contemporaneous 14-character controls motivate—but do not fit—this
conditional model:

```text
completion_ms ~= 105 + (characters - 1) * 80
```

If `streamSpeed` is a millisecond delay per ASCII display step and fixed
overhead remains about 105 ms, there are 13 gaps in a 14-character string and
the model reproduces its 1.1448-second mean. Because every post-CONFIG control
available at that point used the same 14-character length, those data could not
independently identify both the 105 ms intercept and the 80 ms slope. Result 4
subsequently adds 14- and 30-character controls at both 80 and 40. It confirms
strong length dependence but does not recover an exact 80- or
40-millisecond-per-character slope.

| Text | Conditional TX-to-completion prediction |
|---|---:|
| 14 characters | 1.1448 s measured mean; 1.145 s by construction |
| 98 characters | 7.865 s if the per-step hypothesis holds |
| 180 characters | 14.425 s if the per-step hypothesis holds |

All six A/B windows ended before the conditional 14.425-second prediction. The
zero-completion result is compatible with that hypothesis, but it neither
validates the model nor rules out a stall, fixed capacity, renderer-mode change,
or suppressed completion event.

Using Result 4's two-length speed-80 fits instead would put a 180-character
reply near 15.1 seconds, depending on whether TX or G2 response is used as the
origin. That post-hoc extrapolation reinforces that these 12.6-13.5 second
windows were too short; it still does not convert a right-censored trial into a
measured completion.

| ID | First REPLY TX to forced EXIT TX | Short of conditional 14.425 s prediction |
|---|---:|---:|
| RAB180-T1 | 12.567 s | 1.858 s |
| RAB180-T2 | 13.452 s | 0.973 s |
| RAB180-T3 | 13.185 s | 1.240 s |
| RAB180-T4 | 12.620 s | 1.805 s |
| RAB180-T5 | 12.559 s | 1.866 s |
| RAB180-T6 | 13.307 s | 1.118 s |

### A/B transport detail

| Mode | Trials | Mean Pi/XIAO command submission | Mean first TX to final G2 echo | Notes |
|---|---:|---:|---:|---|
| One-shot | 3 | 685.3 ms | 84.0 ms | One UART command, one EvenAI REPLY |
| Multipart | 3 | 1,402.6 ms | 956.3 ms | Three serialized UART commands |

The multipart test also changed pacing, not just message shape. Part 2 was
transmitted only 0.499-0.616 seconds after part 1. The finalizer was transmitted
0.794-0.940 seconds after part 1 and echoed by the G2 at 0.880-0.996 seconds.
Previously successful production streams waited roughly 1.30-2.62 seconds
before part 2 and 1.64-3.12 seconds before finalization. Immediate bulk
enqueue/finalization is therefore a separate confound to test.

The wearer reported that RAB180-T1, the first trial, appeared to show the most
text. That is useful qualitative evidence but cannot be converted into a
character count. RAB180-T1 was simultaneously the first post-reconnect trial,
a one-shot trial, and occurred before the XIAO declared the left plugin silent.
Later one-shot trials were not reported to reproduce the same endpoint, so the
current data does not establish a repeatable one-shot advantage.

During RAB180-T3, the XIAO marked the left temple plugin silent after three
unanswered plugin heartbeats. The right temple remained up and acknowledged all
EvenAI traffic, but RAB180-T4 through T6 are not clean dual-temple repetitions.

## Result 3: pre/post CONFIG evidence that motivated the reversal

The same 14-character answer, `Probe complete`, provides a control across the
manual CONFIG experiment. G2-response-to-completion is the cleanest device-side
comparison because it removes the BLE request/response interval:

| ID | G2 state | Text TX to `STREAM_COMPLETE` | G2 echo to completion |
|---|---|---:|---:|
| CTRL14-PRE | 05:07, before HardwareOne explicitly sent EvenAI CONFIG | 497 ms | 422 ms |
| CTRL14-POST-mean | 08:09, after accepted `streamSpeed=80` CONFIG | 1,144.8 ms | 1,083.8 ms |

The five post-CONFIG TX-to-completion measurements were 1,143, 1,146, 1,178,
1,125, and 1,132 ms (mean 1,144.8; median 1,143; sample SD 20.4 ms). Their G2
echo-to-completion values were 1,099, 1,074, 1,097, 1,074, and 1,075 ms (mean
1,083.8). Long pre-CONFIG production answers independently drained near 27.5
char/s, or roughly 36 ms/character, but those were different lengths and a
different production-stream context.

The strongest directly observed result is a 661.8 ms increase after G2 receipt:

```text
1083.8 ms post mean - 422 ms pre = 661.8 ms slower
```

Conditionally dividing that difference across the 13 gaps gives 50.91 ms/gap,
close to the hypothesized `80 - 30 = 50` change. That numerical agreement makes
CONFIG a strong lead, but it is not an independently fitted per-character rate.

Relevant chronology:

1. At 05:07, before this test sequence explicitly configured the G2, the
   14-character control completed 497 ms after text TX (422 ms after its G2
   echo) on the XIAO monotonic clock.
2. At 05:10, a raw field-13 `CONFIG{streamSpeed=80}` was accepted.
3. At 06:24, the corrected typed `g2aiconfig` again sent and received an
   accepted `CONFIG{streamSpeed=80}` echo.
4. At 08:09, the 14-character controls consistently took about 1.145 seconds
   from text TX (1.084 seconds from their G2 echoes),
   98-character questions were still incomplete after 4.24 seconds, and no
   180-character completion was observed before the forced exits.

This chronology by itself is strong correlational evidence that `streamSpeed`
affects cadence or selects a different progressive-renderer mode. Result 4 now
adds the missing same-connection reversal and establishes the direction of the
field's effect for the tested reply path. The chronology still cannot, by
itself or together with that reversal, prove that CONFIG accounts for every
part of the historical pre/post change: the single pre-CONFIG sample came from
an earlier connection with otherwise uncontrolled G2 state.

At the time of these measurements, the firmware builder repair did not
automatically transmit CONFIG on normal connection. The measured state changes
came from the **manual test commands**. Consequently, Pi CPU governor,
Moonshine, and LLM decoding cannot explain the raw slowdown of an
already-received 14-character G2 answer. After Result 4, the production choice
was made to have the Pi daemon submit field-only speed 40 at startup; that later
policy does not alter the provenance of these trials.

## Result 4: field-only 80 -> 40 -> 80 reversal at two lengths

The missing reversible control ran at 08:55-08:57. The Pi service was stopped,
the UART was free, preflight showed both temples up, and XIAO logging was
inactive at debug level 3 before the test. Each matrix used three fresh
wake-word sessions. Each condition submitted only the speed field—voice switch
and duplex mode were omitted—and the G2 echoed the requested speed under the
same CONFIG magic. Every condition had exactly one REPLY response and one
`STREAM_COMPLETE` before EXIT, with no parsed COMM_RSP error.

The 14-character capture has one health caveat: after the speed-40 CONFIG and
before that wake, the XIAO declared the left plugin silent after three
unanswered plugin heartbeats even though `g2status` still reported `L=up`.
The right temple remained responsive and produced every correlated event. The
30-character matrix independently reproduced the reversal while both plugins
remained responsive through every completion. No recorded loop-health stall
fell inside any of the six REPLY-TX-to-`STREAM_COMPLETE` intervals.

The two ASCII replies were:

- 14 characters: `Probe complete`
- 30 characters: `Probe complete. Timing sample.`

### Exact XIAO TX-to-completion measurements

All event times below are the XIAO's monotonic milliseconds, not serial wall
timestamps. REPLY TX is the native right-temple envelope transmission, not the
later UART command-OK line.

| ID | Characters | `streamSpeed` | CONFIG magic/echo | REPLY magic | REPLY TX | `STREAM_COMPLETE` | TX to completion | Result |
|---|---:|---:|---|---:|---:|---:|---:|---|
| SPD14-80A | 14 | 80 | 231 / 80 | 236 | 10,462,600 | 10,463,731 | 1,131 ms | Valid |
| SPD14-40 | 14 | 40 | 239 / 40 | 244 | 10,474,815 | 10,475,381 | 566 ms | Valid; left-plugin warning |
| SPD14-80B | 14 | 80 | 247 / 80 | 201 | 10,487,530 | 10,488,656 | 1,126 ms | Valid; left-plugin warning |
| SPD30-80A | 30 | 80 | 205 / 80 | 210 | 10,557,238 | 10,559,731 | 2,493 ms | Valid |
| SPD30-40 | 30 | 40 | 213 / 40 | 218 | 10,570,395 | 10,571,456 | 1,061 ms | Valid |
| SPD30-80B | 30 | 80 | 221 / 80 | 226 | 10,584,514 | 10,586,981 | 2,467 ms | Valid |

### Exact G2-response-to-completion measurements

This origin removes the request-to-response transport interval. It is the
cleaner device-side cadence comparison, but the G2 response still is not a
direct pixel observation.

| ID | Characters | `streamSpeed` | G2 REPLY response | `STREAM_COMPLETE` | G2 response to completion |
|---|---:|---:|---:|---:|---:|
| SPD14-80A | 14 | 80 | 10,462,632 | 10,463,731 | 1,099 ms |
| SPD14-40 | 14 | 40 | 10,474,907 | 10,475,381 | 474 ms |
| SPD14-80B | 14 | 80 | 10,487,607 | 10,488,656 | 1,049 ms |
| SPD30-80A | 30 | 80 | 10,557,307 | 10,559,731 | 2,424 ms |
| SPD30-40 | 30 | 40 | 10,570,432 | 10,571,456 | 1,024 ms |
| SPD30-80B | 30 | 80 | 10,584,556 | 10,586,981 | 2,425 ms |

The reversal is large and repeats at both lengths:

| Origin and length | Mean of bracketing speed-80 controls | Speed-40 result | Reduction at 40 | Relative speedup |
|---|---:|---:|---:|---:|
| TX, 14 characters | 1,128.5 ms | 566 ms | 562.5 ms (49.8%) | 1.99x |
| G2 response, 14 characters | 1,074 ms | 474 ms | 600 ms (55.9%) | 2.27x |
| TX, 30 characters | 2,480 ms | 1,061 ms | 1,419 ms (57.2%) | 2.34x |
| G2 response, 30 characters | 2,424.5 ms | 1,024 ms | 1,400.5 ms (57.8%) | 2.37x |

The bracketing TX-to-completion speed-80 controls differ by only 5 ms at 14
characters and 26 ms at 30 characters. This return toward the first 80 value,
after the intervening 40 value, strongly rejects ordinary monotonic drift as
the explanation. Within the tested G2 state and REPLY path, changing only
`streamSpeed` from 80 to 40 **causally and reversibly reduced native completion
time**.

### Length scaling and limits of the numeric interpretation

The 30-character string adds 16 ASCII characters. Dividing its added duration
by 16 gives these two-point effective slopes:

| Timing origin | Speed 80A | Speed 40 | Speed 80B | Mean of speed-80 fits |
|---|---:|---:|---:|---:|
| REPLY TX | 85.13 ms/additional character | 30.94 ms/additional character | 83.81 ms/additional character | 84.47 ms/additional character |
| G2 response | 82.81 ms/additional character | 34.38 ms/additional character | 86.00 ms/additional character | 84.41 ms/additional character |

This is strong evidence for a roughly length-proportional completion interval and
for 40 being faster than 80. It does **not** establish that the numeric field is
an exact millisecond delay per ASCII character: the fitted values do not equal
the configured numbers, only two strings were used, and punctuation, word
layout, fixed overhead, or event quantization may affect the count of native
display steps.

Other boundaries on the conclusion are important:

- the condition order was fixed rather than randomized, although bracketing 80
  on both sides of 40 controls the most concerning one-way drift;
- there was one speed-40 observation per length and two total text lengths;
- the second matrix followed the first and used a different string, so its
  two-point slope is not a fully randomized length experiment;
- `STREAM_COMPLETE` is a protocol proxy; these runs include no recorded wearer
  rating of the actual pixels;
- the test establishes the REPLY/answer path, not long ASK/question behavior;
  and
- the captures do not show the requested post-matrix glasses power cycle or a
  subsequent no-CONFIG baseline. The power-on default, CONFIG volatility, and
  whether the faster historical state returns without CONFIG remain unknown.

The 40-setting reply slope is close to the earlier roughly 36 ms/character
production observation, but that cross-context agreement is only suggestive.
It is not evidence that 40 is the stock default or yet a safe production value.

## What is and is not established

| Claim | Status | Reason |
|---|---|---|
| CM5/XIAO truncated the 98- or 180-byte payloads | Rejected | Full payload envelopes were sent and all G2 responses arrived |
| The 98-character question had already hit a fixed character ceiling | Not supported | More text appeared with each longer interval through 4.24 s |
| No later G2 page/capacity ceiling exists | Unknown | No exact last marker or completed long-length staircase yet |
| One-shot answers render faster than multipart answers | Unknown | All six A/B endpoints were right-censored; pacing also differed |
| Current 44 char/s question barrier is safe in the tested CONFIG-80 state | Rejected | 98 chars remained cut with 4.24 s of native opportunity |
| `streamSpeed` controls native REPLY completion cadence | Established for 40 versus 80 in the tested state | Both 14- and 30-character field-only matrices reversed 80 -> 40 -> 80 with valid CONFIG echoes and completions |
| 40 reaches reply `STREAM_COMPLETE` faster than 80 | Established for the tested strings/path | Speed 40 reduced G2-response-to-completion by 55.9% and 57.8% relative to the bracketing speed-80 means |
| Manual CONFIG 80 fully explains the historical optical slowdown | Strongly supported, not fully closed | The pre/post change and reversible direction agree, but the lone pre-CONFIG control was on an earlier uncontrolled connection and no post-power-cycle no-CONFIG baseline has run |
| `streamSpeed` is exactly milliseconds per character | Not established | Two-point effective response slopes were about 84.4 at setting 80 and 34.4 at setting 40, not exact field values |
| Speed 40 is proven safe for every production path | Unknown | Long ASK behavior, default/persistence, capacity, and wearer-visible endpoints remain untested |
| Production renderer setting | Speed 40 selected with accepted uncertainty | The user chose the repeatably faster REPLY setting after the reversal; the Pi daemon submits only field 2 at startup and exposes `0` as a no-submit test opt-out |
| Pi STT or LLM made already-received G2 text paint more slowly | Rejected for this symptom | G2 completion timing occurs after payload receipt and is device-side |

## Next no-camera test sequence

The field-only reversal is complete and does not need to be repeated. The user
has selected speed 40 for production despite the explicitly retained ASK and
persistence unknowns, so the next required gate uses that state:

1. **Long ASK/question progression at speed 40.** Repeat the 98-character
   question with randomized or descending delays and ample upper bounds. Use
   numbered word markers and record the exact last visible marker, not only
   `cut`/`complete`. This is the direct gate for the Pi question-render barrier;
   the completed REPLY reversal cannot substitute for it.
2. **Question-length staircase at speed 40.** Test 98, 140, 180, and 220 byte
   questions with ample time. A repeated final marker despite more time
   identifies a true capacity boundary; continued advancement or completion
   rejects it.
3. **Optional power-cycle/no-CONFIG baseline.** Set the Pi configuration to
   `deliver.g2_stream_speed: 0` before daemon startup, stop the daemon before
   power-cycling the glasses, and do not send raw CONFIG from the probe. Three
   repeats at 14 and 30 characters would identify the native default and CONFIG
   volatility, but this experiment is no longer a prerequisite for selecting
   40. Configuration zero is a host opt-out, not a value sent to the glasses.
4. **Valid long-answer A/B, if still needed.** Give the 180-character one-shot
   at least 20 seconds. Compare multipart twice: immediate enqueue and paced
   parts. Record both `STREAM_COMPLETE` and the exact last visible word. Start
   only with both temples responsive.

Do not change the production Pi question barrier from the reply-path reversal
alone. Complete the long ASK gate first. Speed 40 is now the selected production
lever, with the unresolved scope above documented rather than treated as
closed.
