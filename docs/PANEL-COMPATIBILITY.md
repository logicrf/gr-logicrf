# Panel firmware compatibility

This gr-logicrf 1.2.3rc2 candidate is paired with the complete Kuiper 1.2.1-rc1 image. Install the matching SD image and host module. The module checks the firmware, SDK and device-control helper identities before starting a session; changing a bitstream file alone is insufficient.

Open a supplied flowgraph in GNU Radio Companion, enter the device IP, select a supported configuration and channel set, and run it. Normal use requires no device Linux password. USB operation also requires the documented one-time USB host setup.

S0 is the FPGA heartbeat. S1 indicates host IQ activity; it does not prove RF output, GNSS lock or lossless recording. Check the session and recording results independently.

The new full image has passed offline file, filesystem and first-boot preparation checks. Physical SD cold-boot validation is pending. Earlier panel incremental tests and the previous release's RF/throughput evidence retain their original version and scope. Do not treat this candidate as a new qualified stable image.
