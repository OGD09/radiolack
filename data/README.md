# data/

Personal data of the airband explorer, the previous Radiolack, whose code
stays in the history under the tag `v0.1.0`. Everything here except this README
is excluded from version control. The rewritten Radiolack neither reads nor
writes this directory, and none of these files is ever written, replaced,
regenerated or deleted.

| File | Role |
| --- | --- |
| `airband-peaks.json` | History migrated from `~/code/pc_repair/output/airband-peaks.json`: 304 validated listens on 30 frequencies, 29 squelch memories, 227 listens with telemetry. |
| `observations*.json` | Working stores of the airband explorer, each with its `.lock` file. |
| `rtl_tcp.log` | Output of the `rtl_tcp` child started by the airband explorer. |
| `gqrx-radiolack*.conf` | Separate Gqrx profiles created by the airband explorer. |

`airband-peaks.json` is an import source to preserve. Its SHA-256 at
migration was
`df6b04929d85bff5ba524c1fd86448675409de7614579b2f9ab386de19e680cd`; this
fingerprint identifies the initial copy only.
