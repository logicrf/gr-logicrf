# 1.2.2rc2 source packaging candidate

## Changes

- LogicRF-owned module source is provided under GPL-3.0-or-later; installation includes license and third-party notices.
- Fourteen minimal control source files, without the complete Qt GUI, are extracted from a pinned GUI commit and bundled with the module. Customers do not need access to a private GUI repository.
- The backend's host-source provenance set excludes GUI-only files. Source provenance and any delivery modifications are listed in `control-runtime/SOURCE-MAP.json`.
- Standalone source installation, launcher registration and customer documentation are included. The native IQ data path is based on 1.2.1.

Source baselines: module `002451b3c0a2ba470f6cc13d5b93cd6ca7b59cd9`, control runtime `bf274c9391577c7326ada7ad3d3578f1468ef86a`. Exact mappings are in `control-runtime/SOURCE-MAP.json`. Internal development history, task records, failed reports, vendor binaries, system images and captured IQ are excluded.

## Qualification limits

Historical 1.2.1 hardware evidence applies to the first board with its exact matching services: an IIO 8 MSPS single RX/single TX 300-second window, a dual 40 MSPS USB 300-second window and corresponding recovery checks. It does not qualify arbitrary hosts, every profile, all temperatures, arbitrary underflow-free waveforms or absolute UTC. The second board's latest add-on combination has not completed this qualification round.

The 1.2.2rc2 packaging checks cover independent extraction, build, offline protocol/model tests, installation and GRC generation. These offline results do not add hardware qualification or complete third-party USB licensing reviews. This is a source pre-release; it does not replace the existing Kuiper/RTL 1.2.0 firmware release.

## Delivery branches

Gitea `main` is the Chinese development branch. Gitea `github-en` contains the reviewed English delivery snapshot; GitHub `main` mirrors the same commits. Delivery commits, Markdown, source explanations and user-facing text use English. Upstream third-party names, notices and API identifiers retain their original meaning. GitHub stays private until the user approves publication. A delivery manifest records the corresponding development source revisions; development history is not merged into the delivery branch.
