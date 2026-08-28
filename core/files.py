"""Recoverable deletes — send2trash.

There is no approval gate in this client: whatever the model decides to run,
runs. `Remove-Item` through the powershell tool is therefore one bad wildcard
away from being unrecoverable, while the Recycle Bin is a cheap undo. This tool
exists so the model has a delete that is not final.

Worth knowing on Windows: `Remove-Item` does *not* use the Recycle Bin, even
interactively — PowerShell has no switch for it, so the shell's own delete is
always permanent. That makes this tool the only recoverable delete here rather
than merely the more convenient one.

Requires:  pip install send2trash
"""

import asyncio
import json
from pathlib import Path

TOOLS = [
    {
        "name": "trash",
        "description": (
            "Delete files or directories to the Windows Recycle Bin, where they "
            "can be restored. Use this instead of 'Remove-Item' in the powershell "
            "tool for ANY deletion the user did not explicitly ask to be "
            "permanent — Remove-Item bypasses the Recycle Bin entirely, so this "
            "is the only recoverable delete available here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files or directories to move to the Recycle Bin."
                    ),
                }
            },
            "required": ["paths"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "trash":
        return json.dumps({"error": f"unknown file tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _run(tool_input: dict) -> str:
    raw = tool_input.get("paths") or []
    if isinstance(raw, str):  # tolerate a single path sent as a bare string
        raw = [raw]
    if not raw:
        return json.dumps({"error": "no paths provided"})

    try:
        from send2trash import send2trash
        from send2trash.exceptions import TrashPermissionError
    except ImportError:
        return json.dumps(
            {
                "error": "send2trash is not installed — `pip install send2trash` "
                "to enable the trash tool"
            }
        )

    results = []
    for item in raw:
        # send2trash needs an absolute path; a relative one fails with a bare
        # "Permission denied: ''" that says nothing about the real cause.
        path = Path(str(item)).expanduser().absolute()
        if not path.exists():
            results.append({"path": str(path), "trashed": False, "error": "no such path"})
            continue
        try:
            send2trash(str(path))
            results.append({"path": str(path), "trashed": True})
        except TrashPermissionError:
            # On Windows this is the Recycle Bin declining the volume rather
            # than a permissions problem: network shares (UNC paths and mapped
            # drives) have no Recycle Bin at all, most removable media skip it,
            # and it can be turned off per-volume in its own properties. The
            # exception carries an empty message in each case, so say what it
            # actually means.
            results.append(
                {
                    "path": str(path),
                    "trashed": False,
                    "error": (
                        "this volume has no Recycle Bin, so the file cannot be "
                        "recovered after deletion. Network shares and most "
                        "removable drives never have one, and it can also be "
                        "disabled per-volume. Deleting it would have to be "
                        "permanent — ask the user first."
                    ),
                }
            )
        except Exception as e:
            results.append({"path": str(path), "trashed": False, "error": str(e)})

    return json.dumps(
        {
            "results": results,
            "trashed": sum(1 for r in results if r["trashed"]),
            "failed": sum(1 for r in results if not r["trashed"]),
        }
    )
