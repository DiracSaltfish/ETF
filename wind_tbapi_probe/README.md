# Wind TBAPI2 read-only probe

This proof of concept reuses the TBAPI2 singleton already present in a running,
logged-in Wind client. It submits exactly one allow-listed `SELECT` for the six
ETF subscription/redemption fields of `159518.SZ` and writes the callback's raw
frame to the Wind sandbox's `Data/tmp/wind_tbapi_probe_result.json`.

It does not initialize a second session, read credentials, modify Wind files, or
send trading instructions. The interface is private and version-specific.
