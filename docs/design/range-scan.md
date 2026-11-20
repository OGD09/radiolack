# Range scan

Designed with the owner on 2026-11-20, after 0.18.0. [`scoping.md`](scoping.md)
holds its summary among the milestones, and its decision of 2026-11-20.

Goal: one scan across a range of frequencies that either finds the signals
there or also stops to listen to each, for a number of passes or without
end, and keeps each scan under a name with the frequencies it found.

Why: the owner did not see the difference between the sweep and the
signals scan, which are two halves of one task. The sweep steps the
receiver across a range but listens to nothing; the signals scan listens
to the active signals, but only within the receiver's span; and they
cannot run together. The owner also asked for a number of passes instead
of a scan that runs until stopped, and for named scans whose frequencies
can be read again and deleted, in the scan's own tab.

Rules it keeps (later-milestones.md, "Rules they share"): receive-only;
tests need no receiver and reach no network; protocol version 6 stays,
an older page ignoring what it does not know; each pull request brings its
documentation and tests.

## Decisions

- **One scan for a range, listening as a switch.** The range scan replaces
  the sweep and the signals scan. "Stop to listen" off, it is the sweep of
  today; on, it listens to each signal it finds, as the signals scan does,
  window after window. A range within the receiver's span, which the
  "Current window" button gives, never tunes the receiver: the signals
  scan of today.
- **Passes: a number, or endless.** From 1 to 99, or endless until
  stopped, as today.
- **Named scans, kept.** Each range scan is kept under a name with the
  frequencies it found, until deleted, and listed in the scan's tab.
- **The Signals tab is detection alone**: its list, its threshold, its
  marks on the spectrum. Its scan button goes.
- **Unchanged**: the memory scan of the ticked bookmarks, in the
  Bookmarks menu, and the History tab, which keeps every channel found,
  whatever the scan, for 90 days.

## Design

### The scan

- **Range**: From and To, the sweep's range of today, saved. "Current
  window" sets them to the part of the receiver's span that detection
  searches, the centre frequency plus or minus 0.4 times the sample rate,
  rounded to the kHz. A range that lies within that part of the span when
  the scan starts takes that one window, at the receiver's centre
  frequency, and never tunes the receiver: it runs with any source, the
  simulated band included. A wider range takes the sweep's windows of
  today and needs a receiver that tunes.
- **Time per window**: as the sweep's of today, 0.25 to 2 s, the time that
  detection searches each window, twice, once its samples come.
- **Stop to listen, off**: the scan goes on to the next window once the
  window's searches end, as the sweep does.
- **Stop to listen, on**: once the window's searches end, the scan takes
  the signals active there at that moment as the window's queue, and
  listens to each in turn with the VFO alone, as the signals scan does:
  the one that waited longest first, in the band plan's mode, a check that
  the squelch opens within 0.25 s, then listening while it stays open,
  "Resume after silence" once it closes, and "Listen at most" before it
  leaves a signal for another of the queue. Once every signal of the queue
  was tuned to, it goes on to the next window. A signal found during the
  window's visit after its searches ended waits for the next pass; in a
  one-window scan, that pass begins at once.
- **Passes**: a pass ends with the last window. After the number of passes
  set, the scan stops by itself, as Stop does, with the reason `done`;
  endless, it goes on until stopped, as today.
- **Stop**: the receiver and the VFO come back where the scan found them,
  unless the scan is on a signal (checking, listening or waiting for the
  resume time), where they stay, so that what is heard goes on: the memory
  scan's rule. Tuning by hand, choosing another receiver, a receiver that
  refuses a window and detection turned off stop it as they stop the sweep
  and the signals scan today; Ignore on the signal heard leaves it for the
  next one.
- **Detection**: the scan needs it, and uses its threshold, which the
  scan's tab shows as it shows it today.

### Named scans

- **Name**: a field above Start, at most 60 characters on one line. Left
  empty, the scan takes its range and start, as "430.000-440.000 MHz,
  2026-11-20 14:05". A kept scan can be renamed.
- **What a kept scan holds**: its name, start and end, range, whether it
  listened, the passes set and done, and why it ended; and for each
  channel found, on the frequency that the lists show it on (the band
  plan's channel, or the 100 Hz): when it was first and last found, how
  many searches found it, its highest margin above the noise and, when the
  scan listened, how long the squelch stayed open there. Never audio or
  samples.
- **Storage**: `scans.json` in the settings folder, like the observations
  (store.py): whole-file writes, readable by the user only, an unreadable
  file set aside. At most 100 scans, the oldest leaving first, and 1,000
  channels each, the least recently found leaving first. A running scan is
  written every 10 s and at its end, so that a power cut loses 10 s at
  most. A scan on the simulated band is not kept, as the History keeps
  none of it.
- **Who**: every interface, paired devices included, sees, renames and
  deletes the kept scans, as every interface clears the History.

### Interface

The Scan menu's tabs become Signals, Range and History.

- **Signals**: "Detection on", the threshold, "Mark on the spectrum", the
  list with Ignore and Restore. No scan button.
- **Range**: Name; From, To and "Current window"; Time per window; Passes,
  a number with an "Endless" box, ticked by default; the threshold, as
  today; "Stop to listen", and under it, enabled while it is on, "Listen
  at most" and "Resume after silence"; Start or Stop, with the state:
  "Pass 2 of 5, window 3 of 17", and "listening 145.500 MHz" while it
  listens. Under them, "Past scans", the latest first: name, date, range
  and how many channels; the running one first, updated as it goes. A
  click on one opens its channels, by frequency, each with Tune and
  Bookmark as in the History, and its Rename and Delete; "Clear all" under
  the list. Delete and "Clear all" take a second press within 5 s, as a
  recording's Delete does.
- **History**: unchanged.

### Protocol and start options

- **Scanner state**: the sweep keeps its name and its fields, and gains
  the settings `sweep_listen` (true or false) and `sweep_passes` (1 to 99,
  or null for endless), saved with the others, and `sweep.passes_set`
  while it runs; the running scan's name is the kept scans' (pull request
  4: the `scans` state, where the running one has no end, rather than a
  `sweep.name`). While it listens, the `signals` object gives
  its state and the signal tuned to, as the signals scan's does today.
  `stopped` gains `done`.
- **Commands**: `{"type": "scanner", "scan": "sweep", "name": "..."}`
  starts it, the name optional. `scan: "signals"`, which an older page
  sends, starts it over the current window with listening, leaving the
  saved range and switch as they are. New messages list the kept scans for
  every interface, send a kept scan's channels to the interface that asks,
  and rename and delete them.
- **Start options**: `--scan sweep` keeps starting it with its saved
  settings, and `--scan signals` over the current window with listening,
  as the service on lenovuntu may use either. The output names it "range
  scan".

## Pull requests

1. This design.
2. The engine: listening, passes and `done` in the sweep, the protocol's
   new settings, the one-window range, tested on the fake rtl_tcp and the
   simulated band. The signals scan and the interface stay as they are.
3. The interface: the Range tab and the Signals tab without its scan;
   `scan: "signals"` and `--scan signals` become the range scan over the
   current window, and the engine's separate signals scan goes; README.md,
   protocol.md and the acceptance tests that name the old tabs (B4, D10,
   R1 to R5, S1).
4. Named scans: the name, the kept scans and their file, their messages,
   and "Past scans".

## Done when

On roamer with the 2 m dipole: a range scan of the repeaters' part of the
band, listening, stops on a conversation, goes on through its windows and
stops by itself after 3 passes with the receiver back where it was; the
same scan with "Current window" never moves the receiver; its name and
channels show again after a restart and go with Delete; `--scan sweep` and
`--scan signals` still start the service on lenovuntu; and every test
passes in CI.

## Limits

- One signal at a time: while the scan listens in one window, the others
  are not searched.
- A signal is listened to once it was found in a window's searches: a
  burst shorter than them, or one that begins after them, waits for the
  next pass.
- A busy window holds the scan for up to "Listen at most" per signal of
  its queue, and longer while each conversation lasts after it.
- Kept scans hold channels and times, not what was said: recording stays
  in the Record menu.
- Two histories remain: the History, every channel for 90 days, and the
  kept scans, one list per scan.
