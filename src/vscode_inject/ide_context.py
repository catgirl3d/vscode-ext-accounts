from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Sequence

try:
    import winreg
except ImportError:
    winreg = None


@dataclass(frozen=True)
class IDEContext:
    name: str
    label: str
    db_path: str = ""
    local_state_path: str = ""
    process_name: str = ""
    launch_env: str | None = None
    launch_commands: tuple[str, ...] = ()
    launch_paths: tuple[str, ...] = ()


DEFAULT_IDE_PATHS = {
    "vscode": {
        "label": "VSCode",
        "db": os.path.expandvars(r"%APPDATA%\Code\User\globalStorage\state.vscdb"),
        "local_state": os.path.expandvars(r"%APPDATA%\Code\Local State"),
        "process": "Code.exe",
        "launch_env": "VSCODE_INJECT_VSCODE_EXE",
        "launch_commands": ("code",),
        "launch_paths": (
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        ),
    },
    "antigravity": {
        "label": "Antigravity",
        "db": os.path.expandvars(r"%APPDATA%\Antigravity\User\globalStorage\state.vscdb"),
        "local_state": os.path.expandvars(r"%APPDATA%\Antigravity\Local State"),
        "process": "Antigravity.exe",
        "launch_env": "VSCODE_INJECT_ANTIGRAVITY_EXE",
        "launch_commands": ("antigravity",),
        "launch_paths": (
            r"%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe",
            r"%LOCALAPPDATA%\Antigravity\Antigravity.exe",
            r"%ProgramFiles%\Antigravity\Antigravity.exe",
            r"%ProgramFiles(x86)%\Antigravity\Antigravity.exe",
        ),
    },
}


def default_ide_paths() -> dict[str, dict[str, object]]:
    return {name: dict(cfg) for name, cfg in DEFAULT_IDE_PATHS.items()}


def _tupled(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if value is None:
        return ()
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return ()


def resolve_context(name: str, ide_paths: Mapping[str, Mapping[str, object]]) -> IDEContext:
    if name not in ide_paths:
        valid = ", ".join(ide_paths)
        raise ValueError(f"Unknown IDE '{name}'. Expected one of: {valid}")

    cfg = ide_paths[name]
    raw_launch_env = cfg.get("launch_env")
    launch_env = str(raw_launch_env) if raw_launch_env else None
    return IDEContext(
        name=name,
        label=str(cfg.get("label", name)),
        db_path=str(cfg.get("db", "")),
        local_state_path=str(cfg.get("local_state", "")),
        process_name=str(cfg.get("process", "")),
        launch_env=launch_env,
        launch_commands=_tupled(cfg.get("launch_commands", ())),
        launch_paths=_tupled(cfg.get("launch_paths", ())),
    )


def override_context(
    context: IDEContext,
    *,
    db_path: str | None = None,
    local_state_path: str | None = None,
) -> IDEContext:
    return replace(
        context,
        db_path=context.db_path if db_path is None else db_path,
        local_state_path=context.local_state_path if local_state_path is None else local_state_path,
    )


def dedupe_candidate_paths(paths: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        expanded = os.path.expandvars(path)
        normalized = os.path.normcase(os.path.normpath(expanded))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(os.path.normpath(expanded))
    return unique


def windows_app_path_candidates(exe_name: str) -> list[str]:
    if winreg is None:
        return []

    subkeys = [
        rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        rf"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
    ]
    candidates: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in subkeys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _value_type = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            candidates.append(str(value))
    return dedupe_candidate_paths(candidates)


def path_command_candidates(command_names: Sequence[str]) -> list[str]:
    candidates: list[str] = []
    for command_name in command_names:
        if not command_name:
            continue
        resolved = shutil.which(command_name)
        if resolved:
            candidates.append(resolved)
    return dedupe_candidate_paths(candidates)


def ide_executable_candidates(
    context: IDEContext,
    *,
    environ: Mapping[str, str] | None = None,
    windows_app_path_candidates_fn: Callable[[str], list[str]] = windows_app_path_candidates,
    path_command_candidates_fn: Callable[[Sequence[str]], list[str]] = path_command_candidates,
) -> list[str]:
    env = environ or os.environ
    candidates: list[str] = []

    if context.launch_env:
        candidates.append(env.get(context.launch_env, ""))

    candidates.extend(list(context.launch_paths))

    if context.process_name:
        candidates.extend(windows_app_path_candidates_fn(context.process_name))

    launch_commands = list(context.launch_commands)
    if context.process_name:
        launch_commands.append(context.process_name)
    candidates.extend(path_command_candidates_fn(launch_commands))
    return dedupe_candidate_paths(candidates)


def resolve_ide_executable_path(
    context: IDEContext,
    *,
    executable_candidates: Callable[[IDEContext], list[str]] | None = None,
    isfile: Callable[[str], bool] | None = None,
) -> str | None:
    candidates = executable_candidates(context) if executable_candidates else ide_executable_candidates(context)
    for candidate in candidates:
        if (isfile or os.path.isfile)(candidate):
            return candidate
    return None


def launch_ide(
    context: IDEContext,
    *,
    resolve_executable_path: Callable[[IDEContext], str | None] | None = None,
    executable_candidates: Callable[[IDEContext], list[str]] | None = None,
    popen: Callable[..., object] | None = None,
) -> str:
    resolver = resolve_executable_path or resolve_ide_executable_path
    exe_path = resolver(context)
    if not exe_path:
        candidates = executable_candidates(context) if executable_candidates else ide_executable_candidates(context)
        checked = "\n".join(f"  - {path}" for path in candidates)
        hint = f"\nSet {context.launch_env} to the full path if needed." if context.launch_env else ""
        raise RuntimeError(
            f"{context.label} executable not found.\nChecked:\n{checked or '  - no candidate paths configured'}{hint}"
        )

    (popen or subprocess.Popen)([exe_path], close_fds=True)
    return f"Started {context.label}"


def is_ide_running(
    context: IDEContext,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    if not context.process_name:
        return False
    result = (run or subprocess.run)(
        ["tasklist", "/FI", f"IMAGENAME eq {context.process_name}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
    )
    return context.process_name in result.stdout
