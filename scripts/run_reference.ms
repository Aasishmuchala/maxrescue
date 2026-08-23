-- MaxRescue reference-render entry point.
--
-- MAXScript has NO line continuation; one trailing backslash kills this file
-- silently. tests/test_maxscript_lint.py enforces that.
(
    local repo = systemTools.getEnvVariable "MAXRESCUE_REPO"
    if repo == undefined or repo == "" do
    (
        format "MaxRescue: MAXRESCUE_REPO is not set\n"
        throw "MAXRESCUE_REPO is not set"
    )

    local script = repo + "/scripts/reference.py"
    if not (doesFileExist script) do
    (
        format "MaxRescue: script not found: %\n" script
        throw ("script not found: " + script)
    )

    format "MaxRescue: executing %\n" script
    python.ExecuteFile script
)
