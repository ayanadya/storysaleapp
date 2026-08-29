"""Guards on scripts/setup_server.ps1.

Windows PowerShell 5.1 assumes ANSI for .ps1 files that lack a UTF-8 BOM.
A single non-ASCII character (an em dash in a comment) then decodes to three
mojibake bytes, which unbalances the surrounding quotes and produces a wall
of parser errors far from the real cause. Both properties below have to hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup_server.ps1"


def test_script_exists():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_script_has_utf8_bom():
    """Without a BOM, PowerShell 5.1 reads the file as ANSI."""
    assert SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf", (
        "setup_server.ps1 must start with a UTF-8 BOM; rewrite it with "
        "encoding='utf-8-sig'"
    )


def test_script_is_ascii_only():
    """Belt and braces: pure ASCII parses identically under any codepage."""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    offenders = {
        (i, line.strip()[:60])
        for i, line in enumerate(text.splitlines(), 1)
        if any(ord(c) > 127 for c in line)
    }
    assert not offenders, f"non-ASCII characters in setup_server.ps1: {sorted(offenders)}"


def test_script_parses_as_powershell():
    """Catch syntax errors before they cost a round trip to the server."""
    import shutil
    import subprocess

    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("no PowerShell available on this platform")

    check = (
        "$e=$null;$t=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count -gt 0){$e|ForEach-Object{Write-Output $_.Message};exit 1}"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"PowerShell parse errors:\n{result.stdout}{result.stderr}"
