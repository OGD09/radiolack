# Band plan

The colour strip under the spectrum names what each frequency range is used
for. Its data is `src/radiolack/web/bandplan-europe.json`, Europe's plan,
`bandplan-france.json`, France's ([France](#france), below),
`bandplan-americas.json`, the Americas' ([Americas](#americas), below), or
`bandplan-asia-pacific.json`, Asia-Pacific's ([Asia-Pacific](#asia-pacific),
below): the engine's
`band_plan` setting names the plan in use, Europe's unless chosen
otherwise, which the
Bookmarks menu chooses, among those that `bandplan.PLANS` lists, each in
`web/bandplan-ID.json` ([`later-milestones.md`](later-milestones.md),
section 4). The interface loads the plan that the engine names through
`bandplan.js`, which checks it and turns it into one sorted list of ranges,
and `tests/test_bandplan.py` checks its structure.

It is a detailed guide, not a legal reference (decided 2026-11-16,
[`scoping.md`](scoping.md)): national tables differ and are the reference,
and local law may forbid listening to some services or disclosing what they
carry.

## Scope

- 0 Hz to 10.5 GHz, without gaps: "Not allocated" below 8.3 kHz, where the
  ITU table starts, and up to 10.5 GHz, which keeps the 3 cm amateur band
  whole with its satellite segment.
- Europe: ITU Region 1 as the CEPT applies it; France's plan is Europe's
  changed where France's national table differs ([France](#france)). The
  Americas: ITU Region 2 ([Americas](#americas)). Asia-Pacific: ITU
  Region 3 ([Asia-Pacific](#asia-pacific)).
- 252 bands and 130 segments in pull request 11 of
  [`coverage-and-precision.md`](coverage-and-precision.md), and 16 channel
  sets (680 channels) and 141 known frequencies in pull request 12.

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
| IARU Region 1, 27th General Conference, Vienna, 19 to 23 September 2026: Final Plenary minutes and Conference Report | Published in October and November 2026 | The band plan changes adopted there, and the balloon telemetry centre at 434.500 MHz |

Pull request 12 read these, on 2026-11-16, for its channel sets and known
frequencies, besides the IARU plans and the ECA above:

| Source | Edition | Used for |
| --- | --- | --- |
| ITU Radio Regulations, Volume 2, Appendix 15 (frequencies for distress and safety, GMDSS) | Edition of 2024 (Rev.WRC-23) | The maritime distress, safety and information frequencies, 121.5 and 123.1 MHz |
| ITU Radio Regulations, Volume 2, Appendix 18 (VHF maritime mobile band) | Edition of 2024 (Rev.WRC-19) | The maritime VHF channels and their 25 kHz spacing |
| ITU Radio Regulations, Volume 1, Article 5 | Edition of 2024 | The standard frequencies of 2.5 to 25 MHz, from the allocations named after them |
| CEPT ECC Decision (11)03, CB radio | Approved 24 June 2011, amended 17 June 2016 | CB's band, 26.960 to 27.410 MHz, its 10 kHz channel separation and the channels it leaves out |
| ETSI EN 300 433, CB radio equipment | V2.1.1 (2016-05) | CB's channel numbers and carrier frequencies (table 1) |
| CEPT ECC Decision (15)05, PMR 446 | Approved 3 July 2015, amended 2 March 2018 | PMR446's band, its 12.5 kHz analogue channels and their lowest carrier, 446.00625 MHz |
| ITU, Final Acts of the Regional Radiocommunication Conference RRC-06 (the GE06 Agreement) | Geneva, 2006, read in the copy that the Slovenian regulator AKOS publishes | The UHF television channels 21 to 48 and their assigned frequencies (table A.3.1-2), the T-DAB blocks 5A to 12D (table A.3.1-15) |
| ETSI EN 300 175-2, DECT physical layer | V2.9.1 (2022-03) | DECT's ten carriers in 1880 to 1900 MHz (clause 4.1.1) |
| Recommendation ITU-R TF.768, standard frequencies and time signals, and its attachment of station characteristics | TF.768-7 (04/2011, corrected 2022); the attachment updated 6 March 2026 | The time signals at 60, 77.5 and 162 kHz and the standard frequencies of 4.996, 9.996 and 14.996 MHz, all kept within a few parts in 10^12 |
| Recommendation ITU-R RA.314, preferred bands for radio astronomy | RA.314-11 (12/2023), table 1 | The spectral lines below 10.5 GHz, the hydrogen line among them |
| Galileo Open Service Signal-in-Space Interface Control Document | Issue 2.1, November 2023 | Galileo's carrier frequencies E1, E5, E5a, E5b and E6 |

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
  25 kHz on 23 cm). Where a channel set gives the spacing, the range has no
  step of its own: CB (10 kHz, EN 300 433), PMR446 (12.5 kHz, ECC Decision
  (15)05) and maritime VHF (25 kHz, Appendix 18). The other steps outside
  the amateur bands are format 1's, unsourced (below).

## Channel sets and known frequencies

- A **channel set** is a raster that a regulation, a harmonised standard
  or the IARU Region 1 plans number across Europe. Each channel is written
  as its source gives it; `from` and `to` are the edges the source gives
  for the set (CB, PMR446, DAB, UHF TV, DECT), or half a spacing beyond its
  first and last channels. The sets: CB radio, 10 m FM repeaters, 6 m FM
  and its repeater outputs, 2 m FM, 70 cm repeater inputs, FM, and the two
  groups of repeater outputs, 23 cm repeater inputs and FM, maritime VHF,
  DAB in Band III, PMR446, UHF TV and DECT.
- The amateur FM channels carry the IARU designators: F, V and U followed
  by the channel's number from 51.000, 145.000 and 430.000 MHz (10, 12.5
  and 12.5 kHz each), an R before a repeater output; on 23 cm and 10 m, the
  RM, SM, RS and RH channels of the plans. Only the channels in the plans'
  FM and repeater segments for the whole of Region 1 are listed: the
  national systems the 70 cm plan endorses (430.025 to 430.375, 431.225 to
  431.600 and 431.625 to 431.975 MHz inputs, 432.825 to 433.200 MHz
  outputs) are left out.
- Maritime VHF lists each channel at its frequency from ship stations,
  and at its frequency from coast stations where the two differ, under one
  number, with the channels of Appendix 18 whose frequency no two-digit
  channel has (1027, 1028, 2006, ASM 1 and 2, AIS 1 and 2). The other
  four-digit channels share a frequency with a two-digit one and are not
  repeated. The set spans the land mobile range between the ship and coast
  frequencies, as the channel numbers run across it.
- PMR446's channels are numbered 1 to 16 in frequency order: ECC Decision
  (15)05 and EN 303 405 give the raster, not the numbers.
- DECT's channels are numbered as EN 300 175-2 numbers its RF channels,
  0 at 1897.344 MHz down to 9 at 1881.792 MHz.
- A **known frequency** (spot) is a single frequency a source names: the
  GMDSS frequencies of Appendix 15, the aeronautical emergency and search
  and rescue frequencies, survival craft's 243 MHz (footnote 5.256), the
  time signals and standard frequencies whose carriers TF.768's
  attachment lists for Europe or the Radio Regulations allocate, the
  spectral lines of RA.314, Galileo's carriers, and the amateur centres of
  activity and calling frequencies of the IARU plans. A spot on a channel
  of a set is not written: the channel's note names it (maritime VHF's
  channel 16, 2 m's 145.500 MHz).
- `reference` marks the time signals and standard frequencies: their
  carriers are the standard.
- A spot's or a set's `mode` is Radiolack's choice of demodulator, as a
  range's: the IARU plans' sideband rule for the amateur centres (LSB
  below 10 MHz, USB above), USB for the maritime and aeronautical voice
  frequencies, none for data, digital voice and television.
- No broadcaster, operator, local repeater, call sign or place is named: a
  time signal is named after its frequency, not its station.

## What could not be checked

- The steps outside the amateur bands that no channel set gives
  (broadcast's 9 and 5 kHz, the airband's 25 kHz / 3, the VOR's and FM
  broadcast's 50 kHz, land mobile's 12.5 kHz and the military UHF band's
  25 kHz): no source read gives them. They are kept from format 1. Pull
  request 12's sets replaced CB's 5 kHz, PMR446's 6.25 kHz grid and
  maritime VHF's 25 kHz with their sources' spacings.
- Left out of the channel sets, as no source read numbers them or gives
  their raster: the Wi-Fi channels (IEEE 802.11 was not read; ETSI's RLAN
  standards and the ECA give bands, not channels), the short-range devices
  at 433 MHz (ERC Recommendation 70-03 gives a band, not channels), the
  4 m FM channels and 10 m FM simplex channels (the IARU plans give their
  spacing, not their numbers), the T-DAB blocks 13A to 13F (the Wiesbaden
  1995 arrangement, revised in Constanta in 2007, was not read), the 6 m
  repeater outputs 500 kHz below their inputs (50.710 to 50.890 MHz, below
  the F designators' first channel), and the 2 m repeater inputs at
  144.975 and 144.9875 MHz (below V00).
- Left out as a set by choice: Band III television, whose channels share
  their range with the DAB blocks, and sets do not overlap.
- Left out of the known frequencies: 1030 and 1090 MHz (ICAO Annex 10 was
  not read; the ADS-B segment of footnote 5.328AA stays), the GPS and
  GLONASS carriers (their interface documents were not read: 1575.42 MHz is
  named after Galileo's E1 only), the 66 2/3 kHz time signal (not a whole
  hertz), and the HF telex distress frequency 4177.5 kHz, which the ECA
  names and the 2024 Appendix 15 no longer lists.
- The hydrogen line is at 1420.406 MHz, to the kilohertz, as RA.314 gives
  it.
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
  knows, and the one the milestone's design uses.
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

### Channel sets and known frequencies

On 2026-11-16, a second reviewer who had not seen the author's notes read
the channel sets and known frequencies of pull request 12 against the
sources named above, given only the file, the list of sources and their
texts, and compared whole tables by script.

Checked, and found right: every channel of the 16 sets (680) and every
known frequency (141). CB's 40 channels against EN 300 433's table 1, with
channels 23 to 25 out of order, its edges and spacing against ECC Decision
(11)03; maritime VHF's 93 entries against Appendix 18, each "Ship station"
and "Coast station" note on its side of its channel; the DAB blocks, UHF TV
channels and DECT carriers against GE06 and EN 300 175-2; the 461 amateur
channels against the VHF Handbook's numbering; every centre of activity of
the HF plan and every centre or calling frequency of the VHF Handbook from
50 MHz to 10.5 GHz; Appendix 15's 32 entries; 121.5 and 123.1 MHz; RA.314's
12 lines below 10.5 GHz; Galileo's five carriers; `reference` on the 12
time signals and standard frequencies alone; no duplicate, no call sign,
place or operator. No frequency, number or designator was found wrong.

Found, and corrected:

- 490 kHz was "NAVTEX (national languages)": no source read says so (the
  Radio Regulations name NAVTEX on 490 kHz, footnote 5.79A). Now "NAVTEX
  (490 kHz)".
- 243 MHz was "Distress (243 MHz)": footnote 5.256 names survival craft
  stations. Now "Survival craft (243 MHz)".
- Maritime channel 06 noted search and rescue with aircraft alone:
  Appendix 18 gives it for inter-ship use first. Now "Inter-ship; aircraft
  search and rescue".
- 23 cm SM20 was "FM calling": the VHF Handbook says FM centre of
  activity. Now "FM centre of activity".
- 50.050 MHz was "CW centre (future)": the plan says international. Now
  "CW international centre (future)".
- 2400.500 MHz was "Narrow band centre": it is the centre of the
  alternative segment for countries without 2320 MHz. Now "Narrow band
  centre (alternative)".

Found, and kept:

- The 70 cm repeater inputs and outputs with a 7.6 MHz shift lie in the
  VHF Handbook's sub-regional block; the plan labels them "Repeater Input
  Region 1" and IARU Region 1 endorses the system, so they stay.
- The time signals at 60, 77.5 and 162 kHz and the standard frequencies of
  4.996, 9.996 and 14.996 MHz come from one station each, and some HF
  standard frequencies from stations outside Europe: their names are
  generic, their frequencies those of TF.768's attachment and the Radio
  Regulations' allocations, and a listener in Europe may hear them, so
  they stay.
- Galileo is a system's name, not an operator's; 1575.42 and 1176.45 MHz
  are also GPS's, whose interface document was not read.
- PMR446 numbered 1 to 16 by order, as no source numbers its channels.
- "CW QRP centre" and "CW QRS centre", where the HF plan says QRP or QRS
  centre of activity in its CW segments: the word tells them from the SSB
  QRP centres.
- Maritime VHF's mode is NFM for the whole set, its digital channels
  included; the DAB set's spacing is the 1.712 MHz between blocks of one
  television channel, while the list gives every block where it lies; the
  23 cm RS designators repeat in the VHF Handbook itself at 1242 and
  1270 MHz, which are left out.

Listed by the reviewer as given by the sources and not in the file, left
for later: Appendix 18's single-frequency and VDES numbers whose
frequencies other numbers already carry; the 70 cm repeater inputs of
432.600 to 432.975 MHz with a 2 MHz shift and the Internet voice gateways
of 433.950 to 434.050 MHz; the 6 m gateways of 50.540 to 50.580 MHz; the
4 m FM and digital channels; the 2 m DV gateways of 144.8125 to
144.8625 MHz; the 23 cm repeaters R20 to R68 and the 25 kHz channels of
1299.775 to 1299.975 MHz; UHF TV channels 49 to 69 (694 to 790 MHz, still
shared with broadcasting in the ECA); PMR446's digital 6.25 kHz raster;
8364 kHz; the standard-frequency allocations at 20 kHz and 400.1 MHz; and
TF.768's single European stations at 20.5 to 25.5, 66 2/3, 198 and
225 kHz.

Could not be checked by the reviewer either: whether the HF standard
frequencies are on the air and heard in Europe; the IMO's use of 490 kHz;
the modes, which the sources give only for 121.5, 123.1 MHz and 2182 kHz;
the Vienna conference's changes beyond its minutes.

## France

`bandplan-france.json`, "France (metropolitan)", is Europe's plan changed
where France's national table differs in a way a listener sees
([`later-milestones.md`](later-milestones.md), section 4, pull request 4).

### Source

| Source | Edition | Used for |
| --- | --- | --- |
| ANFR, Tableau national de répartition des bandes de fréquences (TNRBF), annex to the Prime Minister's order of 18 April 2025, https://www.anfr.fr/gestion-des-frequences-sites/tnrbf/ | Consolidated after the modification of 26 May 2026 (Journal officiel of 27 May 2026), corrected rendition of 14 August 2026 | The services allocated in France and their users (affectataires), the Region 1 columns, sheets 1a to 70a, with their French notes and annexes 7 and 8 |

Read on 2026-11-21. The ANFR publishes the table on its site alone, whose
terms allow its information reproduced only whole and unchanged; French law
on public information (the CRPA, articles L321-1 to L322-1) lets it be
reused with its source and the date of its update, its meaning kept. As the
owner chose on 2026-11-21 ([`scoping.md`](scoping.md)), the plan takes the
table's facts alone, edges and the services allocated, as it takes the
Radio Regulations': every name is Radiolack's, and the table is named above
with its edition and address, never reproduced. Radiolack's plan is not the
table and the ANFR has no part in it.

### How the French plan is written

- Europe's bands, segments, channel sets and known frequencies, with their
  sources and names, wherever the table's Region 1 columns agree with them.
- Where France allocates another service than the one Europe's plan names,
  the range takes France's edges and is named after France's use, as the
  table's services and users give it: the name the plan already gives the
  same allocation nearby ("Military (UHF)" at 383.5 to 399.9 MHz, "Land
  mobile" at 160.975 to 161.475 MHz, with their mode and step), "Military
  mobile" for mobile allocated to defence alone, "Emergency services
  (PPDR)" for the Ministry of the Interior's mobile, a French note's
  designated use where one names it (railway radio at 1900 to 1910 MHz,
  F73a; government drones at 1910 to 1920 MHz, F74).
- Where France allocates no amateur service, there is no amateur band: 4 m
  (69.9 to 70.5 MHz) and 9 cm (3400 to 3410 MHz) go, with their segments
  and known frequencies, into the bands around them.
- Below 30 MHz, where signals cross borders, the part of a band that France
  does not allocate to the band's use, and that Europe's plan gives it, is a
  segment of the band named "Outside France" ("All modes, outside France"
  on 160 m), keeping the band's mode and step: the table gives France's own
  stations their place, not what is heard there. Above 30 MHz, the table's
  use takes the range.

### Where France differs

From the table's Region 1 columns, sheet by sheet:

- 1606.5 to 1625 kHz (sheet 5a): maritime and land mobile, no fixed service.
- 160 m (6a): amateur from 1810 to 1850 kHz; 1850 to 2000 kHz is fixed and
  mobile in France, a segment "All modes, outside France".
- The broadcasting bands of footnote 5.134, which France does not list for
  broadcasting, segments "Outside France" (13a to 20a): 5900 to 5950 kHz,
  9400 to 9500 kHz, 11600 to 11650 and 12050 to 12100 kHz, 13570 to 13600
  and 13800 to 13870 kHz, 15600 to 15800 kHz, 17480 to 17550 kHz, and 15 m
  broadcast whole (18900 to 19020 kHz).
- 13.36 to 13.41 MHz (17a): radio astronomy alone, no fixed service.
- 47 to 50 and 52 to 68 MHz (25a): broadcasting primary (Band I), land
  mobile secondary.
- 4 m (26a): no amateur allocation; 68 to 74.8 MHz is one "Mobile" band.
- 138 to 144 MHz (29a): aeronautical mobile (off-route) for defence alone,
  no land mobile.
- 160.975 to 161.475 MHz (32a): land mobile, as below 160.6 MHz, between
  the coast stations' maritime parts; no maritime channel lies there.
- Band III (35a): broadcasting up to 225 MHz, so the DAB set ends with
  block 12A; 225 to 240 MHz is defence's fixed and mobile, joined to the
  military UHF band up to 242.95 MHz.
- 380 to 399.9 MHz (36a): the Ministry of the Interior's mobile in 380 to
  383.5 and 390 to 393.5 MHz, defence's in the rest, as below 380 MHz.
- The 700 MHz band (42a, 43a, F45g): the Ministry of the Interior's
  blocks for its emergency networks, 698 to 703, 733 to 736, 753 to 758
  and 788 to 791 MHz; the 800 MHz band from 791 MHz.
- 870 to 874 MHz and 915 to 916.1 MHz (43a, 44a): defence's mobile; the
  short-range devices of annex 7 in 874 to 874.4 and 916.1 to 919.4 MHz.
- 1900 to 1920 MHz (54a): railway radio (F73a), then government drones
  (F74).
- 2010 to 2025 MHz (55a): mobile, which annex 8 gives temporary video links.
- 2290 to 2300 MHz (56a): mobile, defence telemetry and video links, no
  space operation.
- 9 cm (60a): no amateur allocation; mobile networks from 3400 MHz (F88a).
- 3.8 to 4.2 GHz (60a): local wideband networks (F89a) beside the
  satellite downlinks.
- 8.4 to 8.5 GHz (68a): fixed, no space research in Region 1.

`tests/test_bandplan.py` checks that the French plan is Europe's outside
these ranges, and the amateur bands that stay.

### Found, and kept

- France's amateur bands that keep Europe's edges: 2200 m, 630 m, 80 m,
  60 m (5351.5 to 5366.5 kHz, note F10a), 40 m to 10 m, 6 m (secondary,
  F21b), 2 m, 70 cm (secondary up to 434 MHz, primary above, the
  satellite service in 435 to 438 MHz alone), 23 cm, 13 cm, 6 cm and 3 cm.
  Their segments stay the IARU Region 1 plans'. The table's limits on
  power and sharing are not shown.
- 162.0375 to 162.05 MHz: France's maritime allocation ends at 162.05 MHz,
  12.5 kHz above Europe's AIS band; too narrow for a range of its own.
- The short-range devices at 862 to 870 MHz, the wireless microphones at
  1785 to 1805 MHz and the vehicle communications at 5.875 GHz, which the
  table allows through its annexes 7 and 8 inside other allocations.
- The table's French edges inside bands whose use does not change (869.2
  to 880, 1785 to 1880, 2310 and 2400 MHz, 5875, 6425 MHz).
- Band III up to 225 MHz, the allocation's edge, where note F38b keeps
  broadcasting below 224.792 MHz; the last DAB block ends at 224.704 MHz.
- Wi-Fi (6 GHz) from 5935 MHz, Europe's edge, where annex 7 starts it at
  5945 MHz.
- "Band I broadcast, land mobile": land mobile is secondary there, kept in
  the name as the one use a listener may meet.
- 1785 to 1800 MHz, defence's mobile, keeps Europe's wireless microphones,
  which annexes 7 and 8 allow there.
- Narrow French uses of annexes 7 and 8 (cordless phones at 26 and 41 MHz,
  model control, paging at 26 MHz, sound reporting channels, video links at
  2050 to 2110 MHz), as Europe's plan leaves out such uses.
- 23 cm: note F53b limits amateur use of 1258 to 1300 MHz from 27 June
  2028, under conditions the table does not give; nothing changes before.
- CB (26.96 to 27.41 MHz), which the table does not name: its box is fixed
  and mobile with the low-power devices' annex, which nothing in it
  contradicts.

### What could not be checked

- 47 to 68 MHz: the table's service column gives land mobile as secondary,
  while footnote 5.164, which it applies in France, makes it primary; the
  plan follows the column.
- A cell's status goes with its first user, as the table's chapter 8
  reads; the range at 160.975 to 161.475 MHz rests on that reading.
- The ARCEP's decision on the amateur service's conditions of use, and the
  REF's band plan, were not read: the amateur bands follow the table and
  the IARU Region 1 plans.
- Annexes 7 and 8: their edges were read, not their conditions.
- Whether 694 to 698 and 736 to 753 MHz, the ARCEP's mobile where annex 7
  names wireless microphones, carry mobile networks: they keep Europe's
  name.
- CB's edges and channels: the table does not name CB.
- Whether a service allocated is in use (Band I broadcasting): the table
  gives allocations and users, not use.

### Second reading

On 2026-11-21, a reviewer who had not seen the author's notes read the
French file against the table, given only the two plans' files, the
writing rules above and the table, and compared the files by script.

Checked: the 45 differences from Europe's plan then (37 ranges, the DAB
set, 7 known frequencies), edge by edge; every Region 1 row of sheets 1a
to 70a against the file; every amateur band from 2200 m to 3 cm and its
satellite segments; the file's structure, names and size; no operator,
broadcaster, call sign or place, and nothing of the table's wording. No
edge was found wrong.

Found, and corrected:

- The 700 MHz band's blocks for the Ministry of the Interior (F45g), shown
  as mobile networks: now "Emergency services (PPDR)", as at 380 MHz.
- 383.5 to 390 and 393.5 to 399.9 MHz were "Military mobile", without a
  mode, beside the same allocation named "Military (UHF)" below 380 MHz:
  now "Military (UHF)", in AM with its step.
- 160.975 to 161.475 MHz was "Fixed and mobile", beside the same
  allocation named "Land mobile" below 160.6 MHz: now "Land mobile".

Found, and kept: the points listed under "Found, and kept" above, from
Band III's edge to 23 cm's date.

## Americas

`bandplan-americas.json`, "Americas (ITU Region 2)", is the plan of ITU
Region 2: North, Central and South America and the Caribbean
([`later-milestones.md`](later-milestones.md), section 4, pull request 2).
Its broadcast FM has 75 µs of de-emphasis (`wfm_deemphasis_us`), which the
engine gives WFM while the plan is in use.

### Sources

Read on 2026-11-21, in the edition current that day.

| Source | Edition | Used for |
| --- | --- | --- |
| ITU Radio Regulations, Volume 1, Article 5 (Table of Frequency Allocations, Region 2 column, and its footnotes), https://www.itu.int/pub/R-REG-RR-2024 | Edition of 2024 (WRC-23), in force from 1 January 2025 | Every band's edges and allocation from 8.3 kHz to 10.5 GHz, the amateur allocations of Region 2, and the edges that footnotes give (5.138 and 5.150, ISM; 5.226, maritime VHF; 5.328AA, ADS-B) |
| ITU Radio Regulations, Volume 2, Appendices 15 and 18 | Edition of 2024 | The distress and safety frequencies and the maritime VHF channels, worldwide, as in Europe's plan |
| IARU Region 2 Band Plan, https://www.iaru-r2.org/wp-content/uploads/2020/02/IARU-Region-2-Band-plan.pdf (the same file as https://www.iaru.org/wp-content/uploads/2020/10/IARU-Region-2-Band-plan.pdf) | Revised September 2020, the edition the IARU Region 2 site published on 2026-11-21 | The segments of the amateur bands from 2200 m to 3 cm, their channel spacings and their centres of activity and calling frequencies |
| Recommendation ITU-R TF.768, and its attachment of station characteristics, https://www.itu.int/oth/R0A08000007/en | TF.768-7; the attachment updated 6 March 2026 | The time signals and standard frequencies of the stations in Region 2: 60 kHz, 2.5, 5, 10, 15, 20 and 25 MHz, and 3330, 7850 and 14670 kHz |
| United States, Code of Federal Regulations, Title 47 (eCFR, https://www.ecfr.gov/): sections 73.14 and 73.201 (AM and FM broadcast), 87.173 (aviation frequencies), 95.963 (CB), 95.563 and 95.1763 (FRS and GMRS), 95.2763 (MURS) | Current on 2026-11-21 | National: medium wave's 10 kHz channels from 540 kHz, FM broadcast's 100 channels of 200 kHz numbered 201 to 300, the airband's 25 kHz spacing, and the CB, FRS and GMRS channels and MURS frequencies |
| NOAA Weather Radio, https://www.weather.gov/nwr/ | Read on 2026-11-21 | National: the seven weather broadcast frequencies, 162.400 to 162.550 MHz |

The Radio Regulations and TF.768 are free downloads of the ITU; the IARU
plan is published for amateurs to use; Title 47 and NOAA's pages are works
of the United States government, in the public domain. Only frequencies
and facts are taken from them: every name is written for Radiolack, and
nothing comes from another program's band plan.

The IARU Region 2 General Assembly of 2025 revised the band plan, but no
new edition was published on 2026-11-21: the revisions went to the member
societies' reflector, which is not public. The plan follows the September
2020 edition, to be read again once a newer one is published.

### How the plan of the Americas is written

- By the rules of Europe's plan ([How the plan is written](#how-the-plan-is-written)),
  with Region 2's column of Article 5 in place of the ECA: a band is named
  by its allocation or its main use in Region 2, and adjacent allocations
  of the same use are one band.
- The amateur bands are Region 2's allocations, secondary ones included:
  160 m from 1800 to 2000 kHz, 80 m to 4000 kHz, 40 m to 7300 kHz, 6 m
  from 50 to 54 MHz, 2 m to 148 MHz, 1.25 m (220 to 225 MHz), 70 cm (430 to
  440 MHz), 33 cm (902 to 928 MHz, with ISM), 23 cm, 13 cm, 9 cm (3300 to
  3500 MHz), 5 cm (5650 to 5925 MHz) and 3 cm. Region 2 has no 4 m band.
  Their segments follow the IARU Region 2 plan, merged into a few per band;
  a segment the plan ends at its last channel runs to the next one.
- The IARU plan's steps: 10 kHz for the FM segments of 10 m and 6 m and
  the Internet gateways of 6 m, 2 m and 1.25 m, 20 kHz for 6 m's radio
  control and 1.25 m's repeaters, 12.5 kHz for 33 cm's repeaters and
  25 kHz for 23 cm's. The IARU Region 2 plan gives 2 m's and 70 cm's
  repeaters no channel spacing, so their segments have no step.
- The steps outside the amateur bands come from national rules, the United
  States' (below): 10 kHz on medium wave, 100 kHz for FM broadcast, whose
  200 kHz channels lie on the odd 100 kHz, and 25 kHz on the airband. The
  HF broadcasting bands keep Europe's 5 kHz, which no source read gives.
- Channel sets: CB (40 channels, the same table as Europe's), FM broadcast
  (channels 201 to 300, 88.1 to 107.9 MHz), maritime VHF (Appendix 18,
  as in Europe's plan) and FRS and GMRS (22 channels; FRS shares them all
  with GMRS).
- Known frequencies: Region 2's time signals and standard frequencies,
  marked `reference` for the automatic calibration; the worldwide ones of
  Europe's plan (distress and safety, aeronautical emergency, radio
  astronomy lines, Galileo's carriers); the IARU Region 2 plan's centres of
  activity and calling frequencies; MURS's five frequencies and the seven
  weather radio frequencies.

### National sources

No regional document numbers CB, FRS, GMRS or MURS, places the weather
radio frequencies, or gives broadcast and airband spacings for the whole
of Region 2. The plan takes them from the United States' rules, the one
national source read, and says so here: they are national, as the
licence-free services and their channels differ from one country to the
next. Canada's rules were not read (its weather radio page did not
answer).

### Left out

- National allocations of the amateur service beyond Region 2's table,
  such as 420 to 430 and 440 to 450 MHz for 70 cm: those ranges show their
  regional allocation, fixed and mobile.
- National channel plans: the 60 m channels, the 2 m and 70 cm repeater
  pairs, the television channels' numbers (the North American numbering
  was not read), the maritime VHF channels with an A suffix, the GMRS
  repeater pairs, mobile networks' and land mobile's national blocks.
- The IARU plan's local options and ATV carriers below 430 MHz.
- The station names of the time signals: a time signal is named after its
  frequency.

### What could not be checked

- The IARU Region 2 plan's revisions of 2025 (above).
- The typing errors of the September 2020 edition, read as meant: 10 m's
  repeater outputs written 29620 to 26690 kHz (29690), 2 m's last channel
  written 144.970 MHz (146.970), and 6 m's repeater offset written in Hz
  (kHz).
- Whether the steps that come from the United States' rules hold across
  the region.

### Second reading

On 2026-11-21, a reviewer who had not seen the author's notes read the
file against the sources above, given only the file, the writing rules
and the documents, and checked its structure by script.

Checked: every band edge from 8.3 kHz to 10.5 GHz against Region 2's
column of Article 5, and the edges that footnotes give; every amateur
segment and centre of activity from 2200 m to 3 cm against the IARU
Region 2 plan; CB, FM broadcast, FRS and GMRS and MURS against Title 47,
medium wave's and the airband's steps; the `reference` frequencies against
TF.768's Region 2 stations, and no other frequency marked; the file's
structure, names and size. No edge, channel or reference was found wrong.

Found, and corrected:

- The Internet gateways of 2 m (145.000 to 145.100 MHz) and 1.25 m
  (223.700 to 223.750 MHz) had no step, where the IARU plan gives 10 kHz
  channels, as on 6 m: now 10 kHz.
- 6 m's radio control (50.800 to 51.000 MHz) had no step, where the plan
  gives 20 kHz channels: now 20 kHz.
- 2 m's 144.900 to 145.000 MHz left out the plan's weak signal use: now
  "FM, digital voice, weak signal".
- The APRS frequency that the plan protects in part of the region was
  named after that part: now "APRS (protected)", without a place.

Found, and kept:

- FM broadcast's 100 kHz step, inside the FM broadcast set: the Automatic
  step takes the set's 200 kHz, and the range's step gives the signals
  found their channel, on the odd 100 kHz.
- "FRS and GMRS": the set holds the 22 frequencies that FRS shares with
  GMRS, the GMRS simplex channels among them; GMRS's repeater inputs at
  467.550 to 467.725 MHz are left out (above).
- 6 m's repeater segments end where the IARU plan ends them, 51.480 and
  51.980 MHz, as the plan does not say whether those are the last
  channels.
- Maritime VHF's edges come from footnote 5.226, and the AIS band ends at
  162.0375 MHz as in the table, as in Europe's plan.
- 3700 to 3800 MHz stays "Satellite downlinks": footnote 5.435B's mobile
  networks are national.
- 70 cm is 430 to 440 MHz, Article 5's allocation, where the IARU plan
  runs from 420 to 450 MHz (above).
- The naval VLF stations of TF.768, which send no plain carrier, are not
  known frequencies, as in Europe's plan.

## Asia-Pacific

`bandplan-asia-pacific.json`, "Asia-Pacific (ITU Region 3)", is the plan of
ITU Region 3: most of Asia, and Oceania
([`later-milestones.md`](later-milestones.md), section 4, pull request 3).
Its broadcast FM has 50 µs of de-emphasis.

### Sources

Read on 2026-11-21, in the edition current that day.

| Source | Edition | Used for |
| --- | --- | --- |
| ITU Radio Regulations, Volume 1, Article 5 (Table of Frequency Allocations, Region 3 column, and its footnotes), https://www.itu.int/pub/R-REG-RR-2024 | Edition of 2024 (WRC-23), in force from 1 January 2025 | Every band's edges and allocation from 8.3 kHz to 10.5 GHz, the amateur allocations of Region 3, and the edges and uses that footnotes give (5.138 and 5.150, ISM; 5.226, maritime VHF; 5.328AA, ADS-B; 5.313A, 5.317A, 5.341C, 5.346A and 5.433A, mobile networks) |
| ITU Radio Regulations, Volume 2, Appendices 15 and 18 | Edition of 2024 | The distress and safety frequencies and the maritime VHF channels, worldwide, as in Europe's plan |
| IARU Region 3 Band Plan, document R3-004, https://iaru-r3.org/wp-content/uploads/2025/01/R3-004-Band-Plans-IARU-Region-3.pdf, listed on https://www.iaru-r3.org/reference/standard-documents/ | Rev. 2, November 2024, adopted by the 19th IARU Region 3 Conference | The segments of the amateur bands from 2200 m to 3 cm, and their centres of activity and calling frequencies |
| Recommendation ITU-R TF.768, and its attachment of station characteristics, https://www.itu.int/oth/R0A08000007/en | TF.768-7; the attachment updated 6 March 2026 | The time signals and standard frequencies of the stations in Region 3: 40 and 60 kHz, 2.5, 5, 10 and 15 MHz |

The Radio Regulations and TF.768 are free downloads of the ITU; the IARU
Region 3 site says its materials may be subject to copyright, and the plan
is published for amateurs to use. Only frequencies and facts are taken
from them: every name is written for Radiolack, and nothing comes from
another program's band plan.

### How the plan of Asia-Pacific is written

- By the rules of Europe's plan ([How the plan is written](#how-the-plan-is-written)),
  with Region 3's column of Article 5 in place of the ECA, as the
  Americas' plan is written.
- The amateur bands are Region 3's allocations, secondary ones included:
  160 m from 1800 to 2000 kHz, 80 m to 3900 kHz, 40 m to 7200 kHz, 6 m
  from 50 to 54 MHz, 2 m to 148 MHz, 70 cm (430 to 440 MHz), 23 cm, 13 cm,
  9 cm (3300 to 3500 MHz), 5 cm (5650 to 5850 MHz) and 3 cm. Region 3 has
  no 4 m, 1.25 m or 33 cm band. Their segments follow the IARU Region 3
  plan, merged into a few per band; the international beacons' frequencies
  on 20 m to 12 m are segments of 1 kHz, the plan's guard band of 500 Hz
  on each side.
- The IARU Region 3 plan gives no channel spacing, so the amateur segments
  have no step.
- Mobile networks are named where a footnote identifies a band for IMT
  across the region or in most of its countries: 698 to 790 MHz (5.313A),
  790 to 960 MHz (5.317A), 1427 to 1518 MHz (5.341C and 5.346A),
  3500 to 3600 MHz (5.433A), and the bands that Europe's and the Americas'
  plans name so above 1.7 GHz.
- The steps outside the amateur bands are Europe's plan's, unsourced: 9 kHz
  on medium wave, 5 kHz in the HF broadcasting bands and 50 kHz for FM
  broadcast; the airband's is 25 kHz, as the Americas' rules give it, not
  Europe's 8.33 kHz.
- FM broadcast runs from 87 to 108 MHz, Region 3's broadcasting
  allocation. WFM's de-emphasis, 50 µs, is a choice: no source read gives
  one for the region, and a country may use 75 µs.
- The one channel set is maritime VHF (Appendix 18, as in Europe's plan).
- Known frequencies: Region 3's time signals and standard frequencies,
  marked `reference` for the automatic calibration; the worldwide ones of
  Europe's plan (distress and safety, aeronautical emergency, radio
  astronomy lines, Galileo's carriers); and the IARU Region 3 plan's
  centres of activity and calling frequencies, its suggested APRS
  frequencies on 2 m among them.

### Left out

- National allocations, the footnotes that name countries among them:
  broadcasting in 76 to 87 MHz (5.183), the 7100 to 7200 kHz fixed and
  mobile allocation (5.141B), 70 cm's national extensions, and the IMT
  identifications of a few countries (5.296A).
- The IARU plan's 5850 to 5925 MHz segment: Region 3's table has no
  amateur allocation there.
- National channel plans: CB, the licence-free services, FM broadcast
  channels, television channels, the 2 m and 70 cm repeater pairs.
- The APRS frequencies that the IARU plan names after member societies,
  and the digital modes' frequencies of its footnotes.
- The station names of the time signals, and the standard frequency that
  TF.768's attachment lists for a satellite, at 2599.675 MHz.

### What could not be checked

- The IARU Region 3 plan's overlapping ranges, read as meant: 10 m's
  repeater inputs (29510 to 29590 kHz) and FM simplex (written 29520 to
  29620 kHz, here from 29590 kHz), 20 m's narrow band ranges (14070 to
  14110 and 14100 to 14112 kHz, split here at the beacon's guard band), and
  13 cm's beacons (2304.3 to 2304.4 MHz) inside its next range (written
  from 2304.3 MHz, here from 2304.4 MHz). 40 m's digital voice centre,
  7070 kHz, is listed beside 7040 to 7060 kHz, outside that range: it
  stays at 7070 kHz.
- 30 m starts at 10110 kHz in the IARU plan and at 10100 kHz in Article 5:
  the band follows Article 5, its segments the plan.
- The steps outside the amateur bands, and WFM's de-emphasis (above).

### Second reading

On 2026-11-21, a reviewer who had not seen the author's notes read the
file against the sources above, given only the file, the writing rules
and the documents, and checked its structure by script.

Checked: every band edge from 8.3 kHz to 10.5 GHz against Region 3's
column of Article 5 and the footnotes that give edges; every amateur band,
segment, centre of activity and calling frequency against the IARU
Region 3 plan, Article 5 followed where the two differ; the `reference`
frequencies against TF.768's stations in Region 3, and no other frequency
marked; the worldwide known frequencies and maritime VHF against Europe's
plan, by script; the file's structure, names and size. No edge, segment
or reference was found wrong.

Found, and corrected:

- 1452 to 1492 MHz was named after broadcasting, where footnote 5.346A
  identifies it for mobile networks across Region 3: 1427 to 1518 MHz is
  now one band, "Mobile networks (1.5 GHz)", as in the other plans.
- 10 m's 29100 to 29300 kHz was named "All modes, AM", where the plan says
  all modes in 6 kHz: now "All modes (6 kHz)", still in AM.
- 15 m's 21150.5 to 21450 kHz left out the satellite uplinks that the plan
  allows there: now "CW, phone, satellite uplinks".
- The satellite's standard frequency and 40 m's digital voice centre are
  now stated above.

Found, and kept:

- 3900 to 4000 kHz is "75 m broadcast", where 3900 to 3950 kHz is also
  aeronautical mobile: broadcasting is the use a listener meets.
- Maritime VHF's edges come from footnote 5.226, as in the other plans.
- The steps outside the amateur bands: none plainly wrong for the region,
  they stay unsourced.

## File format (version 2)

A JSON object, written with one band, segment, channel set or known
frequency per line:

| Field | Meaning |
| --- | --- |
| `format` | `2`. |
| `name` | Name of the plan, shown in the Bookmarks menu. |
| `wfm_deemphasis_us` | The de-emphasis of broadcast FM where the plan applies, in µs: `50` or `75`. The engine reads it alone, for WFM's audio. |
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
| `from`, `to` | Where the set applies, in whole Hz, inside the plan; a set may span several bands, and sets do not overlap. |
| `label` | A channel's label, `{n}` standing for its number. |
| `spacing` | Whole Hz between channels, above 0. |
| `first`, `count`, `number` | A regular set: its first channel's centre in whole Hz, how many channels (at least one), and the first one's number. |
| `list` | Instead of the three above, each channel: `n`, its number or name, `f`, its centre in whole Hz, and an optional `note`. |
| `mode` | Optional, as a band's. |

Every channel's centre lies inside where its set applies, at least the
spacing from the next one: a frequency is on a channel within half the
spacing.

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
a round number: the airband step is 25 kHz / 3 (8.33 kHz channels). Where a
channel set applies, the Automatic step is the set's spacing and lands on
its channels, whatever their offset (PMR446's 446.00625 MHz + n × 12.5 kHz,
CB's channels at 5 kHz past the 10 kHz). FM broadcast
steps by 50 kHz, to reach the stations that some countries place between the
100 kHz channels, and selects WFM, v3's demodulator for broadcast FM
([`scoping.md`](scoping.md)).

Zoomed out, the band strip draws a band's segments while they average at
least 12 px on screen, and the whole band as one block of its type's colour
below that. The shortwave broadcast bands have no short name: theirs would
read like the amateur bands' (`15 m`).

Plans for other regions are other files in the same format.
