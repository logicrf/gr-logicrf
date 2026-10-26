# 1.2.2 source release

## Changes

Version and installation-registration metadata now identify 1.2.2. The native IQ sources, installed-helper requirements and device firmware remain unchanged from the preceding reviewed English delivery. Optional USB host tools retain their independent 1.0.0-r2 version.


- LogicRF-owned module source is provided under GPL-3.0-or-later; installation includes license and third-party notices.
- Fourteen minimal control source files, without the complete Qt GUI, are extracted from a pinned GUI commit and bundled with the module. Customers do not need access to a private GUI repository.
- The backend's host-source provenance set excludes GUI-only files. Source provenance and any delivery modifications are listed in `control-runtime/SOURCE-MAP.json`.
- Standalone source installation, launcher registration and customer documentation are included. The native IQ data path is based on 1.2.1.

Source baselines: module `002451b3c0a2ba470f6cc13d5b93cd6ca7b59cd9`, control runtime `bf274c9391577c7326ada7ad3d3578f1468ef86a`. Exact mappings are in `control-runtime/SOURCE-MAP.json`. Internal development history, task records, failed reports, vendor binaries, system images and captured IQ are excluded.

## Qualification limits

Historical 1.2.1 hardware evidence applies to the first board with its exact matching services: an IIO 8 MSPS single RX/single TX 300-second window, a dual 40 MSPS USB 300-second window and corresponding recovery checks. It does not qualify arbitrary hosts, every profile, all temperatures, arbitrary underflow-free waveforms or absolute UTC. The second board's latest add-on combination has not completed this qualification round.

The source-package checks cover independent extraction, build, offline protocol/model tests, installation and GRC generation. These offline results do not add hardware qualification or complete third-party USB licensing reviews. This is a source release; it does not replace the existing Kuiper/RTL 1.2.0 firmware release.

## Source-release verification

The stable package was built with 8 jobs against GNU Radio 3.10.12.0 with QtGUI, Python 3.10 and libiio 0.23. All 6 CTest groups, source-install registration and a 12,345-sample native mock passed. On the first H102, a one-second dual-RX IIO recording at 4 MSPS produced exactly 4,000,000 samples per channel with passing ADC-window checks and full-file SHA-256. Both files were read to EOF through native GNU Radio blocks without network access.

The original profile, RF settings, identity and idle state were independently restored. Selecting a profile can reset carrier settings; maintenance software must restore the complete saved RF state rather than assuming that reselecting the previous profile restores every carrier. No RF transmission was enabled. These checks do not add absolute UTC, a second-board qualification or a repeated USB endurance result.

## Delivery branches

Gitea `main` is the Chinese development branch. Gitea `github-en` contains the reviewed English delivery snapshot; GitHub `main` mirrors the same commits. Delivery commits, Markdown, source explanations and user-facing text use English. Upstream third-party names, notices and API identifiers retain their original meaning. GitHub stays private until the user approves publication. A delivery manifest records the corresponding development source revisions; development history is not merged into the delivery branch.
