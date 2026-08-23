-- MaxRescue batch entry point.
--
-- Launched by scripts/run_rescue.ps1 through 3dsmaxbatch.exe. Keep this trivial;
-- everything interesting lives in scripts/rescue.py, which is statically checked.
--
-- MAXScript has NO line continuation. One trailing backslash kills this file
-- silently and the rescue never runs. tests/test_maxscript_lint.py enforces it.
(
    local repo = systemTools.getEnvVariable "MAXRESCUE_REPO"
    if repo == undefined or repo == "" do
    (
        format "MaxRescue: MAXRESCUE_REPO is not set\n"
        throw "MAXRESCUE_REPO is not set"
    )

    local script = repo + "/scripts/rescue.py"
    if not (doesFileExist script) do
    (
        format "MaxRescue: script not found: %\n" script
        throw ("script not found: " + script)
    )

    format "MaxRescue: executing %\n" script
    python.ExecuteFile script
)
