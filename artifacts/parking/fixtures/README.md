# Parking source fixtures

Snapshots of Rowan's authoritative parking sources, taken 2026-08-23, so the test
suite can exercise the whole refresh path without touching the network. The live
sources are the ones in `src/dailymail/parking_sources.py`; these are copies.

| File | Source |
|---|---|
| `glassboro-mymaps.kml` | Google My Maps `mid=1c2Qlz4nAV57oTio6HbOTgmYTwOoqimKW`, embedded by <https://www.rowan.edu/about/visiting/main.html>. Styles stripped and the Academic Departments / All Gender Restrooms / Shuttle Stop folders removed; every Parking placemark and every landmark the parser actually reads is intact. |
| `stratford-mymaps.kml` | `mid=1Sq4QEKv3l7nPp-chZUZpXq5lko4s3PEj`, embedded by <https://www.rowan.edu/about/visiting/stratford.html>. |
| `camden-mymaps.kml` | `mid=1YhmxFZP-QcEFuleJVKQZ-bgFN0qH2ryG`, embedded by <https://www.rowan.edu/about/visiting/camden.html>. |
| `sewell-mymaps.kml` | `mid=1AhzykQJLby6YoadivTofklMIfpMNfsA`, embedded by <https://www.rowan.edu/about/visiting/sewell.html>. |
| `*.stub` | Stand-ins for the PDF and HTML sources, which are fingerprinted rather than parsed. A stub keeps the fixtures small; `tests/test_parking_live.py` checks the real documents. |

Live sources are verified by the opt-in probe:

```sh
DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py -v
```
