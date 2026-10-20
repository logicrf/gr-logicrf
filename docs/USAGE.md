# Operation, profiles and integrity

## Connect and configure

1. Open a writable example copy in Companion and enter the H102 IP. Replace the documentation address `192.0.2.10` with the actual address.
2. Select IIO or a configured USB backend, a profile and RX/TX channels. Sample rate and bandwidth must agree with the profile.
3. Generate and run. After starting acquisition in the panel, wait for identity verification and readiness. Do not add a Throttle block to a hardware data path.
4. Pause, tune and resume within one profile. Change profiles while stopped. Applying a different sample-rate/bandwidth profile reinitializes the radio and may take more than 20 seconds.

Center-frequency control covers 50 MHz through 5.8 GHz, subject to profile and shared LO constraints. Channels sharing an LO cannot request conflicting frequencies. Frequency is in Hz and sample rate in samples/s. Changing a plot axis or DSP constant alone does not tune hardware.

## Transport budget

IQ16 uses 4 bytes per complex sample on the wire; cf32 uses 8 bytes per stored sample. Ethernet has a 64 MB/s budget per direction and for the combined bidirectional demand. USB has a combined 320 MB/s budget. The module rejects excess demand, dual USB RX combined with TX, and shared LO conflicts; it does not silently lower the rate.

Dual 40 MSPS USB reception writes approximately 640 MB/s as cf32. Storage must sustain the actual write rate.

## Recording and time

Recording checks sample counts throughout the receive path, device overflow status, metadata and file checksums. A completed recording manifest is saved only when all checks succeed. Interrupted or failed recordings remain marked as partial. Use the recording examples when you need these integrity checks; a standalone GNU Radio File Sink does not provide the same checks.

USB retains the native 128-byte header and integer time, epoch, index and quality fields. Valid device digital UTC does not imply calibrated absolute UTC at the antenna port or calibrated group delay. IIO output is labelled as relative/host output time; absolute ADC UTC is not fabricated. Controlled retuning gaps and stop boundaries are reported separately instead of joining discontinuous samples into an apparently continuous stream.

## FM and replay

The FM example defaults to a low-rate single RX IIO profile. GNU Qt panels provide tuning, candidate stations, search and volume. Finding a candidate does not identify its RDS station name. Replay checks the completed manifest, files and SHA before reading IQ, preserves integer tags and does not contact hardware. It can be used for offline inspection.

## Troubleshooting

- Connection errors: check IP, network and the installed API service. Do not enter a Linux password or enable an SSH fallback.
- Identity/profile mismatch: install matching firmware and services. Do not change SHA allowlists to bypass checks.
- Preparation delay: distinguish same-profile warm operations from cross-profile cold programming; wait for explicit readiness or an error.
- Recording failure: inspect source errors, counts and recovery receipts; check transport/storage budgets. Renaming a partial file does not make it successful.
- Device busy: stop the other flowgraph and disconnect before trying again. Do not interrupt another active application.
