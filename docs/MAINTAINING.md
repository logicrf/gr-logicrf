# Maintaining the English distribution

## Branches

| Repository branch | Purpose |
|---|---|
| Gitea `main` | Chinese development branch and engineering history |
| Gitea `github-en` | Reviewed English source distribution |
| GitHub `main` | The same commits as Gitea `github-en` |

GitHub repositories are private by default. Completing a translation or a build does not authorize making a repository public. The same policy applies to H102 Kuiper/Linux, RTL, IIO GUI and GNU Radio delivery repositories. Each component retains its own distribution and licensing scope.

Implement functional changes on the development branch first, then prepare their English distribution. Do not merge private development history into this branch or use `git push --mirror`/`--all` for GitHub. Push only the reviewed delivery branch and named release tags. Keep the development revision and per-file provenance in delivery manifests.

Before synchronizing a delivery commit, run `python3 scripts/check_delivery.py`. It checks tracked text, all Markdown links and the selected commit's entire ancestor history. Push `github-en` to Gitea first, then the same commit to GitHub `main`; verify both remote commit IDs. Repository visibility is managed separately and remains private by default.

## Writing for users

Write around installation, connection, configuration, receiving, transmitting, recording and troubleshooting. Use natural English rather than word-for-word translations. Explain compatibility requirements and practical limits where they affect a task. Keep internal build paths, incident timelines and superseded experiments out of user guides.

Use **device**, **receive session**, **exclusive access**, **requirements**, **supported profiles** and **known limitations** in user-facing prose. Reserve implementation terms such as owner, guard, post and source pins for developer documentation. Keep units explicit: Hz, MHz, samples/s, MSPS, dB and dBFS. Configuration IDs and API names remain unchanged.

## Source identity and compatibility

English comments change file hashes. `docs/h102-runtime.lock.json` pins the actual translated host files. `control-runtime/SOURCE-MAP.json` retains original provenance and records modifications.

Ordinary API calls do not upload these helper copies: they request helpers already installed on the device. `python/h102_control/installed-helpers.json` separates reviewed local hashes from the original installed helper requirements. Only exact reviewed local hashes map to those requirements. Edited or unknown hashes are never mapped, and the existing device service still independently verifies its complete installed source set. Device firmware, APIs and allowlists are unchanged.

`docs/usb-protocol.lock.json` records both original and translated hashes. Protocol/header translations preserve every executable and preprocessor token. Translation checks do not add hardware, RF, timing or vendor SDK qualification.
