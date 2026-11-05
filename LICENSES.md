# Licences

| Component | Licence | What this archive contains |
| --- | --- | --- |
| dify-table-csv sample (checker, direct client, DSL builder, install scripts, tests) | MIT. Copyright (c) 2026 Pilot (epistemedeus) / Neomorphic dify-table-csv sample. See LICENSE. | The source of this sample. |
| Pinned free reader | MIT. Copyright (c) 2026 SameDayDesk. See reader/LICENSE. | reader/ source and package-lock.json. Not node_modules. |
| Reader dependencies (@exodus/bytes 1.16.0, htmlparser2 12.0.0, zod 3.25.76, and their locked transitive modules) | See reader/notices/npm/*/LICENSE and reader/notices/npm/SOURCES.json. | Notices only. `npm ci --ignore-scripts --prefix reader` installs the locked modules. |
| Dify 1.19.0 | Modified Apache License 2.0, extra commercial and logo conditions. Copyright 2025 LangGenius, Inc. SPDX NOASSERTION. Text sha256 232cf91474932d5110ed304e53b6b742a58463857c571fae803fdf2ac36d7bb3. See licenses/dify-1.19.0-LICENSE. The 1.19.0 text matches the 1.18.0 text. | The licence text and the image pins in install/compose.pin.yml. Not the Dify source tree and not the images. |
| Workflow DSL | Same MIT sample licence. DSL version 0.7.0, kind app, mode workflow, dependencies empty. | workflow/reader-table-csv.dsl.yml. No secrets and no model node. |

The checker id `h64314.table_csv.v1` and provenance schema `h64314.reader-csv.provenance.v1` are the installed compatibility pins. They are not an account, a customer, or a paid route.

A stopped reader is HTTP 503. The checker classifies that as malformed, retryable false, and an empty CSV. It does not return a fixture CSV. Recovery is a new run after the reader is listening again. Automatic HTTP retry stays off.
