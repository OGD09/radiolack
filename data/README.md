# data/

Personal data directory. Everything here except this README is excluded
from version control.

| File | Role |
| --- | --- |
| `airband-peaks.json` | History migrated from `~/code/pc_repair/output/airband-peaks.json`: 304 validated listens on 30 frequencies, 29 squelch memories, 227 listens with telemetry. |
| `observations.json` | Working store, created by `radiolack import` (or `--create-store`). It evolves with every new observation. |
| `observations.json.lock` | Lock of the process writing the store. |
| `rtl_tcp.log` | Output of the `rtl_tcp` child started by `scan`/`watch`. |
| `gqrx-radiolack.conf` | Separate Gqrx profile created by `radiolack gqrx-profile`. |

`airband-peaks.json` is an import source to preserve: RadioLack only reads
it, refuses to use it as a store, and never replaces, regenerates or deletes
it. Its SHA-256 at migration was
`df6b04929d85bff5ba524c1fd86448675409de7614579b2f9ab386de19e680cd`; this
fingerprint identifies the initial copy only and does not constrain the
working store.
