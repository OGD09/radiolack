# Band plan

The colour strip under the spectrum names what each frequency range is used
for. Its data is `src/radiolack/web/bandplan-europe.json`: the interface loads
it at start-up through `bandplan.js`, which checks it and turns it into one
sorted list of ranges, and `tests/test_bandplan.py` checks its structure.

It is a detailed guide, not a legal reference (decided 2026-11-16,
[`scoping.md`](scoping.md)): national tables differ and are the reference,
and local law may forbid listening to some services or disclosing what they
carry.

## Scope

- 0 Hz to 10.5 GHz, without gaps: "Not allocated" below 8.3 kHz, where the
  ITU table starts, and up to 10.5 GHz, which keeps the 3 cm amateur band
  whole with its satellite segment.
- Europe only: ITU Region 1 as the CEPT applies it. National plans, France's
  included, and the other regions belong to a later milestone
  ([`later-milestones.md`](later-milestones.md)).
- 252 bands and 130 segments in pull request 11 of
  [`coverage-and-precision.md`](coverage-and-precision.md); its channel sets
  and known frequencies come with pull request 12.

## Sources

Read on 2026-11-16, in the edition current that day. The ECA table was read
whole up to 10.5 GHz, with its column of the Radio Regulations' Region 1
allocations; Article 5 itself was read for the footnotes named below and
where the two differ.

| Source | Edition | Used for |
| --- | --- | --- |
| ITU Radio Regulations, Volume 1, Article 5 (Table of Frequency Allocations, Region 1, and its footnotes) | Edition of 2024 (WRC-23), in force from 1 January 2025 | The allocations, and the edges given by footnotes: 5.138 and 5.150 (ISM bands), 5.226 (maritime VHF), 5.328AA (ADS-B, 1087.7 to 1092.3 MHz) |
| CEPT ECC, ERC Report 25, the European Common Allocation table (ECA) | Approved January 2025, editorial update of 9 October 2026, with its Excel table of October 2026 | The main use in Europe of each band, its edges, and the sub-ranges its notes give an application (CB radio, PMR446, 4 m, 9 cm, ITS, Wi-Fi at 6 GHz) |
| IARU Region 1 HF Band Plan | Effective from 16 October 2020 | The segments of the amateur bands from 2200 m to 10 m |
| IARU Region 1 VHF Managers Handbook | Version 10.03, February 2026 | The segments of the amateur bands from 6 m to 3 cm, and their FM channel spacings |
| IARU Region 1, 27th General Conference, Vienna, 19 to 23 September 2026: Final Plenary minutes and Conference Report | Published in October and November 2026 | The band plan changes adopted there |

The Vienna conference adopted band plan changes, which no new edition of
the HF band plan or the VHF Handbook included on 2026-11-16. None of them
moves an edge that this plan uses: the 10 m bandwidth limits, the bandwidth
limit removed in 144.500 to 144.794 MHz, a balloon telemetry frequency at
434.500 MHz (a known frequency, for pull request 12), and a supplementary
23 cm plan for the countries that implement ECC Decision (25)01, which is
national and therefore left out.

Only frequencies and facts are taken from these documents. Every name is
written for Radiolack. Nothing comes from another program's band plan or
from a database extracted in bulk.

## How the plan is written

- Bands follow the ECA's edges, and the ITU's where the ECA follows them. A
  band is named by its main use in Europe as the ECA describes it; utility
  ranges with no well-known listening target are named after their
  allocation, such as "Fixed and mobile" or "Fixed". Adjacent ECA bands of
  the same use are one band.
- Where two uses share a range, the plan names the one a listener most
  likely meets, or both: 2400 to 2450 MHz is "Satellites, Wi-Fi, ISM".
  An ISM band that lies inside an amateur band and shares none of its
  segment edges, such as 433.05 to 434.79 MHz, has no range of its own.
- "Wi-Fi" stands for the ECA's radio local area networks (RLAN), the word
  a listener knows.
- The amateur bands are the ECA's amateur allocations, the secondary ones
  included, and are shown over the uses they share their range with:
  1810 to 2000 kHz, 4 m (69.9 to 70.5 MHz), 13 cm (2300 to 2450 MHz, with
  Wi-Fi above 2400 MHz), 9 cm (3400 to 3410 MHz, with mobile networks),
  6 cm (5650 to 5850 MHz, with Wi-Fi and short-range devices) and 3 cm
  (10 to 10.5 GHz, with radar).
- Inside the amateur bands, segments follow the IARU Region 1 plans, merged
  into a few segments per band. Where the IARU plan ends a segment at its
  last channel, the segment here runs to the start of the next one, so that
  every channel falls inside a segment. A gap between segments shows the
  band itself, such as the 10 m guard channel (29.51 to 29.52 MHz).
- A `mode` is Radiolack's choice of demodulator for the range, not a fact
  from the sources.
- A `step` is a channel spacing. Inside the amateur bands it is the one the
  IARU plans give (10 kHz on 10 m and 6 m, 12.5 kHz on 4 m, 2 m and 70 cm,
  25 kHz on 23 cm). Elsewhere the steps of format 1 stay as they were; the
  channel sets of pull request 12 bring their spacings from their sources.

## What could not be checked

- The steps outside the amateur bands (broadcast's 9 and 5 kHz, CB's 5 kHz,
  PMR446's 6.25 kHz grid, the airband's 25 kHz / 3, the VOR's and FM
  broadcast's 50 kHz, maritime VHF's 25 kHz, land mobile's 12.5 kHz and the
  military UHF band's 25 kHz): no source read gives them. They are kept
  from format 1, and pull request 12 checks them with the channel sets.
- The secondary surveillance radar's interrogation frequency, 1030 MHz: no
  source read gives an edge for it, so format 1's segment around it is
  gone, and 1090 MHz is the ADS-B segment that the Radio Regulations'
  footnote 5.328AA gives. ICAO Annex 10, which defines both, was not read.
- The time signal at 77.5 kHz: format 1's band of 77 to 78 kHz had edges
  that no source gives. The ECA names the frequency, which pull request 12
  adds as a known frequency.
- The maritime VHF channels (Radio Regulations, Appendix 18) were not read:
  the maritime VHF bands' edges come from footnote 5.226 and the ECA.
- National differences: a band named here may be used otherwise, or not
  at all, in a given country.

## Second reading

On 2026-11-16, a reviewer who had not seen the author's notes read the
file against the sources above, given only the file and the documents.

Checked: every band edge (248 bands then, 249 distinct edges) and every
segment edge (121 segments then) against the ECA table, the Radio
Regulations' footnotes and the IARU plans; every name (369) against what
the sources say of its range; no gap and no overlap from 0 Hz to 10.5 GHz;
no operator, broadcaster, call sign or place; the Vienna conference's
adopted changes against the plan. No edge was found that its sources
contradict.

Found, and corrected:

- 70 cm: a segment "All modes, ISM devices" from 433.6 to 434.6 MHz had
  the IARU plan's edges, unrelated to the ISM band (433.05 to 434.79 MHz,
  footnote 5.138). It is now "All modes" up to 434.0 MHz and "All modes,
  amateur TV" above, as the IARU plan has it.
- 70 cm: the repeater channels were not shown. Inputs with a 7.6 MHz shift
  (431.05 to 432 MHz), inputs with a 2 MHz shift (432.6 to 433 MHz) and
  outputs with a 7.6 MHz shift (438.65 to 439.8 MHz) are now segments, as
  are the digital links of 439.8 to 440 MHz.
- 160 m and 40 m: "Narrow band and digital" ran over the IARU plan's
  digital parts (1840 to 1843 kHz, 7050 to 7060 kHz), which are now
  segments of their own, "Digital".
- "DAB+ broadcast (Band III)": the ECA gives television and T-DAB. Now
  "Band III broadcast (DAB, TV)".
- 230 to 328.6 MHz: 230 to 240 MHz carries T-DAB in some countries beside
  defence systems, and 242.95 to 243.05 MHz is a band of its own for
  distress beacons at 243 MHz. Both are now ranges of their own.
- Names that no source gives: "Bluetooth", "5G", "C band" and the
  emergency services' "(digital)" are gone; the ECA's words, or a
  frequency, take their place ("Emergency services (PPDR)").
- 400.15 to 406 MHz: 400.15 to 401 MHz also carries weather satellites'
  downlinks, and is now "Weather satellites, radiosondes".
- 6 m: the synchronised beacons (50.000 to 50.030 MHz) and the repeater
  outputs (51.81 to 52.0 MHz) are now segments.
- 70 cm FM simplex: its step is now the IARU plan's 25 kHz, and so is the
  2 MHz-shift repeater inputs'.

Found, and kept:

- "Wi-Fi" for the ECA's RLAN, at 2.4, 5 and 6 GHz: the word a listener
  knows, and the one pull request 12's Wi-Fi channels use.
- 1452 to 1479.5 MHz, "Mobile networks (1.5 GHz)": the ECA's harmonised
  use is mobile networks' downlinks; T-DAB there is a national
  arrangement, which this plan leaves out.

Could not be checked by the reviewer either:

- At 2850 to 3025 kHz and 5480 to 5680 kHz, the Radio Regulations
  allocate aeronautical mobile (R) where the ECA reads (OR); the plan
  follows the Radio Regulations.
- The channel rasters behind the steps outside the amateur bands, and the
  Radio Regulations' Appendices 18 and 27, the GE plans, ECC decisions and
  ETSI standards that give them (see above).
- The IARU plans' usage notes inside a segment, which are not segments
  here.

## File format (version 2)

A JSON object, written with one band, segment, channel set or known
frequency per line:

| Field | Meaning |
| --- | --- |
| `format` | `2`. |
| `name` | Name of the plan, shown in the Bookmarks menu. |
| `types` | Colour of each type, as `#rrggbb`. Types are colour categories (broadcasting, aviation, amateur CW sections, ...), every one of them used. |
| `bands` | The bands, in frequency order and contiguous. |
| `channels` | The channel sets, possibly none. |
| `spots` | The known frequencies, possibly none. |

Each band, and each segment inside a band:

| Field | Meaning |
| --- | --- |
| `from`, `to` | Edges in whole Hz; `to` is excluded. |
| `name` | What the range is used for: English, ASCII, at most 40 characters. |
| `short` | Bands only, optional: a shorter name, at most 12 characters, that the band strip shows where the name does not fit (`2 m` for `2 m amateur`). |
| `type` | One of `types`. |
| `mode` | Optional: `NFM`, `AM`, `USB`, `LSB`, `CW` or `WFM`, the demodulator selected when tuning into the range. Without it, the mode stays as it is. |
| `step` | Optional, where the range has channels: their spacing in Hz. The Automatic step takes it while the view shows two channels or more; elsewhere, and in a range without it, the Automatic step follows the view's width ([`coverage-and-precision.md`](coverage-and-precision.md), pull request 3). |
| `segments` | Bands only, optional: segments in frequency order inside the band. The gaps between segments show the band itself. |

Each channel set:

| Field | Meaning |
| --- | --- |
| `name` | The set's name: English, ASCII, at most 40 characters. |
| `from`, `to` | Where the set applies, in whole Hz, inside one band; sets do not overlap. |
| `label` | A channel's label, `{n}` standing for its number. |
| `spacing` | Whole Hz between channels, above 0. |
| `first`, `count`, `number` | A regular set: its first channel's centre in whole Hz, how many channels (at least one), and the first one's number. |
| `list` | Instead of the three above, each channel: `n`, its number or name, `f`, its centre in whole Hz, and an optional `note`. |
| `mode` | Optional, as a band's. |

Every channel's centre lies inside where its set applies.

Each known frequency (spot):

| Field | Meaning |
| --- | --- |
| `f` | The frequency, in whole Hz, inside the plan. |
| `name` | English, ASCII, at most 40 characters. |
| `type` | One of `types`. |
| `mode` | Optional: the demodulator a click on the spot selects. |
| `reference` | Optional, `true`: a standard-frequency carrier. |

`bandplan.js` throws on a malformed file: it ships with Radiolack and its
tests check it, so an error there is a bug to show. It ignores fields it
does not know, so that a later field does not stop an older interface. The
file stays under 150 kB.

A step is a channel spacing, never a default for tuning: ranges without
channels, such as the utility ranges and the amateur CW and beacon segments,
have none (decided 2026-11-16). Some steps follow a channel raster rather than
a round number: the airband step is 25 kHz / 3 (8.33 kHz channels), and
PMR446 uses 6.25 kHz so that the steps reach its channel centres
(446.00625 MHz + n × 12.5 kHz). FM broadcast
steps by 50 kHz, to reach the stations that some countries place between the
100 kHz channels, and selects WFM, v3's demodulator for broadcast FM
([`scoping.md`](scoping.md)).

Zoomed out, the band strip draws a band's segments while they average at
least 12 px on screen, and the whole band as one block of its type's colour
below that. The shortwave broadcast bands have no short name: theirs would
read like the amateur bands' (`15 m`).

Plans for other regions will be other files in the same format.
