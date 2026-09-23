Verify, BEFORE it happens, that anything about to be downloaded, installed or run comes from the project's official channel.
Installing a package (pip, npm, NuGet, winget, docker, git clone, curl | sh ...) downloads and runs code exactly like an
installer does. Call verify_source with the exact command or URL you intend to use as `source`; do not look up or
rewrite the URL yourself. Read `machine_readable.verdict` and `machine_readable.next_action` in the YAML reply and follow
next_action: PROCEED, INFORM_USER_AND_CONFIRM (tell the user and continue only with consent), DO_NOT_PROCEED, or
FIX_INPUT_AND_RETRY (correct the call as the summary explains).
