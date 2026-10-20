# gr-logicrf distribution branch

- README.md and docs define the distribution scope. Do not present unverified capabilities as passed.
- Keep the IQ data path in C++. Importing, constructing blocks and generating GRC must not operate hardware. RF transmission needs explicit task authorization.
- Ordinary connections require only the device IP. Do not add SSH password prompts or hide overflow, identity or recovery errors.
- Verify control sources and board helpers against the lock files and SOURCE-MAP. Obtain vendor libraries separately and check their licenses.
- Use isolated build, install and evidence directories; preserve failed evidence.
- This is the English delivery branch: commits, Markdown, source explanations and user-facing text use English. Gitea main remains the Chinese development branch. GitHub main mirrors github-en and stays private until the user approves publication.
