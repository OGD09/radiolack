# Band plan

The colour strip under the spectrum names what each frequency range is used
for. Its data is `src/radiolack/web/bandplan-europe.json`: the interface loads
it at start-up through `bandplan.js`, which checks it and turns it into one
sorted list of ranges, and `tests/test_bandplan.py` checks its structure.

## Scope and sources

- 9 kHz to 6.425 GHz, without gaps: the receivers in scope reach about 6 GHz.
- Bands follow the ITU Radio Regulations, Article 5, for Region 1, named by
  their main use in Europe as the CEPT European Common Allocation table (ERC
  Report 25, November 2025) and the harmonised European bands describe it.
  Utility ranges with no well-known listening target are named after their
  allocation, such as "Fixed and mobile" or "Fixed".
- Inside the amateur bands, segments follow the IARU Region 1 band plans,
  merged into a few segments per band: the HF band plan of 16 October 2020,
  and the VHF Managers Handbook, version 10.03 (February 2026), for VHF, UHF
  and microwaves. Where the IARU plan ends a segment at its last channel, the
  segment here runs to the start of the next one, so that every channel falls
  inside a segment.
- Where two uses share a range, the plan names the one a listener most likely
  meets, or both: 433.6 to 434.6 MHz is "All modes, ISM devices".
- It is a guide, not a legal reference. National tables differ and are the
  reference, and local law may forbid listening to some services or
  disclosing what they carry.
- Last checked against these sources on 2026-11-09.

## File format (version 1)

A JSON object, written with one band or segment per line:

| Field | Meaning |
| --- | --- |
| `format` | `1`. |
| `name` | Name of the plan, shown in the Bands panel. |
| `types` | Colour of each type, as `#rrggbb`. Types are colour categories (broadcasting, aviation, amateur CW sections, ...), every one of them used. |
| `bands` | The bands, in frequency order and contiguous. |

Each band, and each segment inside a band:

| Field | Meaning |
| --- | --- |
| `from`, `to` | Edges in whole Hz; `to` is excluded. |
| `name` | What the range is used for: English, ASCII, at most 40 characters. |
| `short` | Bands only, optional: a shorter name, at most 12 characters, that the band strip shows where the name does not fit (`2 m` for `2 m amateur`). |
| `type` | One of `types`. |
| `mode` | Optional: `NFM`, `AM`, `USB`, `LSB` or `CW`, the demodulator selected when tuning into the range. Without it, the mode stays as it is. |
| `step` | Optional: tuning step in Hz, used by the Automatic step. The default is 100 Hz. |
| `segments` | Bands only, optional: segments in frequency order inside the band. The gaps between segments show the band itself. |

Some steps follow a channel raster rather than a round number: the airband
step is 25 kHz / 3 (8.33 kHz channels), and PMR446 uses 6.25 kHz so that the
steps reach its channel centres (446.00625 MHz + n × 12.5 kHz). FM broadcast
steps by 50 kHz, to reach the stations that some countries place between the
100 kHz channels; it has no mode until v3's WFM demodulator, decided on
2026-11-14 ([`scoping.md`](scoping.md)), gives it WFM.

Zoomed out, the band strip draws a band's segments while they average at
least 12 px on screen, and the whole band as one block of its type's colour
below that. The shortwave broadcast bands have no short name: theirs would
read like the amateur bands' (`15 m`).

Plans for other regions will be other files in the same format.
