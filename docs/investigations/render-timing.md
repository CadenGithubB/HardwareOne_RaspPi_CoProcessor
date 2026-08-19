# Render timing — when did the viewer actually see it?

**Question:** text was sent to a display peripheral. When was it visible, was any
of it lost, and which knob changes the answer?

**Profile fields:** `HW_RENDER_SINK`, `HW_PROBE`, `HW_CFG`, `HW_EVID_ROOT`.

## 1. Name the milestone, always

Four different events get called "when it rendered". Never conflate them:

1. **Command accepted** — the near device received and attempted the command.
   This is *not* an acknowledgement from the display. Past reports called this an
   "ACK" and drew wrong conclusions from it.
2. **Peer echo** — the display's protocol-level response for that command. It
   proves receipt. It does not prove a pixel was painted.
3. **Completion event** — the display's own "done" event. The strongest protocol
   proxy for viewer-visible completion, still not a direct observation.
4. **Human observation** — a person reporting what they saw, and the exact last
   visible word.

Only (4) settles a "the text looks cut off" complaint. (3) is the best number
you can automate. Every timing figure records which milestone it measured.

Derive durations from the device's bracketed monotonic timestamp, not from host
serial-receive wall time — debug output buffers, and its host timestamps drift
relative to the real device event.

## 2. Separate the three candidate explanations

For any "text appears truncated" symptom:

| Candidate | Test | Ruled out when |
|---|---|---|
| The sender truncated the payload | inspect the sent envelope and the peer responses | full payload sent, all responses received |
| A fixed capacity ceiling on the display | length staircase with ample time | more text appears with more time |
| Cadence — the display paints slower than you wait | vary the pacing knob, hold length fixed | timing changes with the knob alone |

The third is the one that hides: with a slow paint cadence, a payload that fully
arrived still looks cut off, and every upstream component looks innocent under
inspection because it *is* innocent.

## 3. The reversal matrix — proving a knob is causal

Do not test A then B. Test **A → B → A** at two different payload lengths, with
valid configuration echoes captured at each transition:

| Trial | Setting | Length | Send→completion | Peer-response→completion |
|---|---|---|---:|---:|
| 1 | baseline | short | | |
| 2 | candidate | short | | |
| 3 | baseline | short | | |
| … | | long | | |

A reversible change under the bracketing baselines establishes that this knob
controls this cadence *in the tested state*. Report the effect as a percentage
against the mean of the two bracketing baseline runs.

What this does **not** establish, and must be written down as such: the
power-on default, persistence across a power cycle of the peripheral, the exact
physical unit of the setting (two-point slopes are effective rates, not field
values), or safety on paths you did not test.

## 4. Capacity staircase

To find a genuine capacity boundary, hold the setting fixed and step the payload
length with *ample* upper time bounds. Use **numbered word markers** in the
payload and record the exact last visible marker — not merely "cut" or
"complete". A repeated final marker despite more time identifies a real
boundary; continued advancement rejects it.

Randomize or descend the delays rather than always ascending, so an ordering
effect cannot masquerade as a threshold.

## 5. Right-censoring

If the trial ended before the completion event, the measurement is **censored**,
not a value. A whole A/B can be invalidated this way — every endpoint censored
means the comparison is unavailable, regardless of how the numbers look. Give a
long-payload trial a generous timeout and state the timeout you used.

## 6. Close with the claims table

| Claim | Status | Reason |
|---|---|---|
| … | Established / Strongly supported / Not supported / Rejected / Unknown | evidence |

Include the rejections — "the host truncated the payload: rejected, full
envelopes were sent and all responses arrived" is what stops the next person
re-investigating a dead theory. When a production setting is chosen with
unresolved unknowns, record it as chosen-with-accepted-uncertainty and list the
unknowns rather than letting the choice imply they were closed.
