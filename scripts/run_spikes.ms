-- MaxRescue P0 spike runner.
--
-- Launched by scripts/run_spikes.ps1 through 3dsmaxbatch.exe. Keep this file
-- trivial: everything interesting lives in scripts/spikes.py, which is testable.
--
-- MAXScript has NO line continuation. A single trailing backslash anywhere in
-- this file kills it silently and the spike never runs. tests/test_maxscript_lint.py
-- enforces that, and that paths are embedded with forward slashes.
(
    local repo = systemTools.getEnvVariable "MAXRESCUE_REPO"
    if repo == undefined or repo == "" do
    (
        format "MaxRescue: MAXRESCUE_REPO is not set\n"
        throw "MAXRESCUE_REPO is not set"
    )

    local script = repo + "/scripts/spikes.py"
    if not (doesFileExist script) do
    (
        format "MaxRescue: script not found: %\n" script
        throw ("script not found: " + script)
    )

    format "MaxRescue: executing %\n" script
    python.ExecuteFile script
)
