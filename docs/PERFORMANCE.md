# What a call costs in CPU

Rule 70 asks for a CPU target verified by profiling. This is the target, the
measurement, and the thing the measurement says to do next — which is
nothing, and why.

Reproduce with `make profile`.

## The target

**One connected call under 6% of a core**, so sixteen concurrent calls leave
a single-core container at or under the 60% the rule names. Nothing in this
project had a figure to size a deployment with before this.

## The measurement

Measured 2026-11-12, 180 seconds of call audio through the real session and
the real Live adapter, with the socket faked. No network, no key, no money.

| | |
|---|---|
| per 20 ms frame | **15.5 µs** |
| one call | **0.08% of a core** |
| target | under 6% |
| at 60% of a core | ~775 calls, arithmetically |

**Seventy-five times under budget.** The CPU of this service is not a
problem and is not close to being one.

### Where it goes

```
963000  0.048  src/voice/vad.py:38(<genexpr>)      the per-sample loop
  3000  0.042  {built-in method builtins.sum}       summing it
  3002  0.008  json/encoder.py:204(iterencode)      the frame envelope
  3000  0.003  {built-in method binascii.b2a_base64}
```

`frame_rms` is about 80% of it: 320 multiply-adds per frame, fifty frames a
second, in Python. Everything else — the base64 of 640 bytes, the JSON
envelope, the whole turn policy — is the remaining fifth.

### Why it is not being optimised

Because it is 0.08% against a 6% target. `audioop.rms` would do the same
work in C and is the obvious fix, and it is deprecated in 3.11 and gone in
3.13 — so taking it would buy a 75× surplus and a migration. The profile is
written down here instead: whoever needs the headroom later knows exactly
which function to take, and does not have to find it again.

## What this does NOT say

**775 calls per core is arithmetic, not capacity.** The event loop is a
single thread, so a process carries what one core can carry, and what
actually binds first is none of this: open sockets, memory per session, and
Poorvika's own upstream services, which every call reaches through on nearly
every turn.

The honest sizing statement is that **CPU is not the constraint**, and that
the constraint has not been measured because it depends on the host, which
is not chosen (gap 9 in `RULES.md`).

## In production

A profile on a laptop says what a call costs here, today. `/metrics` carries
the version anybody is actually alerted on:

| | |
|---|---|
| `process_cpu_seconds_total` | user plus system, read at scrape time |
| `process_resident_memory_bytes` | from `/proc/self/statm` |

Read at scrape time rather than sampled on a timer, because a gauge only as
fresh as a background task is a gauge that lies after the task dies.

## The harness

`src/tools/profile_call.py`. Two things in it exist because the first
version got them wrong:

- **Every frame is counted, and it raises if any were skipped.**
  `Conversation.run()` cancels whichever pump is still going when the other
  finishes, and a model that ran out of script first profiled a tenth of the
  work and reported it as the whole. That read as a service ten times
  cheaper than it is.
- **The real adapter is in the path**, with the socket faked through the
  `connect` seam it already had. Faking the model instead skipped the
  per-frame base64 and JSON entirely — a third of the real cost.
