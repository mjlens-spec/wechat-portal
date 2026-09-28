#!/usr/bin/env python3
"""Install WeChat Portal entry points from this repository. Never reads, copies or changes keys.

Installs (all per-user):
  ~/.local/share/wechat-portal/config.json   account binding (0600)
  ~/.local/bin/wechat-portal                 launcher that runs this repository
  ~/.claude/skills/wechat-portal, ~/.codex/skills/wechat-portal  -> <repo>/skill
    (Claude Code: /wechat-portal; Codex CLI and the ChatGPT app: $wechat-portal)

Claude slash commands that earlier versions installed (wechat-portal.md, wx-image.md, wechat-work.md)
duplicate the skill and are backed up and removed on every install. With --migrate-wechat-work, the
former wechat-work skill links, launcher and runtime are backed up and removed as well.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parent
LEGACY_RUNTIME = ".local/share/wechat-work"
COMMANDS = ("wechat-portal.md", "wx-image.md", "wechat-work.md")
MARKERS = re.compile(r"skills/wechat-(portal|work)/SKILL\.md|\.local/bin/wechat-(portal|work)")


def regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError("Refused unexpected file ownership/type: " + str(path))


def directory(path, home):
    """Check each managed parent before creating or writing below it."""
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Refused symlink directory: " + str(current))
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir() or current.stat().st_uid != os.getuid():
            raise ValueError("Refused unexpected directory ownership/type: " + str(current))


def legacy_account(home):
    config = home / LEGACY_RUNTIME / "config.json"
    if config.is_file() and not config.is_symlink():
        return json.loads(config.read_text()).get("account_dir")
    return None


def install(home, repo=REPO, binary=None, account_dir=None, agent="both", migrate=False, python=None):
    os.umask(0o077)
    home = home.resolve()
    runtime = home / ".local/share/wechat-portal"
    config_path = runtime / "config.json"
    config = {}
    if config_path.exists() or config_path.is_symlink():
        regular(config_path)
        config = json.loads(config_path.read_text())

    binary = Path(binary or config.get("binary") or repo / "vendor/wx-cli/wx-cli").expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("wx-cli must be an executable file")
    account = account_dir or config.get("account_dir") or (legacy_account(home) if migrate else None)
    if not account:
        raise ValueError("First installation requires --account-dir (the directory containing db_storage)")
    account = Path(account).expanduser()
    if account.is_symlink():
        raise ValueError("Account directory must not be a symlink")
    account = account.resolve(strict=True)
    if not (account / "db_storage").is_dir():
        raise ValueError("Account directory must contain db_storage")
    config = {"schema_version": 2, "binary": str(binary), "account_dir": str(account)}

    hosts = (".codex", ".claude") if agent == "both" else ("." + agent,)
    skill_source = repo / "skill"
    files = [config_path, home / ".local/bin/wechat-portal"]
    targets = [home / host / "skills/wechat-portal" for host in hosts]

    legacy_links = [home / host / "skills/wechat-work" for host in (".codex", ".claude")]
    legacy_files = [home / ".local/bin/wechat-work"]
    # Commands we installed before; they only point at the skill. Unrelated commands are left alone.
    managed_commands = [p for p in (home / ".claude/commands" / n for n in COMMANDS)
                        if p.is_file() and not p.is_symlink() and MARKERS.search(p.read_text(errors="ignore"))]
    legacy_runtime = home / LEGACY_RUNTIME

    # ---- preflight: nothing changes until every destination checks out ----
    for path in files + targets:
        directory(path.parent, home)
    for path in files:
        if path.exists() or path.is_symlink():
            regular(path)
    for target in targets:
        if (target.exists() or target.is_symlink()) and (
                not target.is_symlink() or target.resolve() != skill_source.resolve()):
            raise ValueError("Existing unrelated skill left unchanged: " + str(target))
    if migrate:
        for link in legacy_links:
            if (link.exists() or link.is_symlink()) and not link.is_symlink():
                raise ValueError("Legacy skill is not a symlink; left unchanged: " + str(link))
        for path in legacy_files:
            if path.exists() or path.is_symlink():
                regular(path)
        if legacy_runtime.is_symlink():
            raise ValueError("Legacy runtime is a symlink; left unchanged")

    runtime.chmod(0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = runtime / "backups" / stamp

    def backup(path):
        # ".claude/commands/x.md" -> "claude__commands__x.md" (no leading dot, so backups stay visible)
        target = backup_root / str(path.relative_to(home)).lstrip(".").replace("/", "__")
        directory(target.parent, home)
        if path.is_symlink():
            target.with_suffix(target.suffix + ".symlink").write_text(os.readlink(path))
        else:
            shutil.copy2(path, target)
            target.chmod(0o600)

    def put(path, contents, mode=0o600):
        if path.exists():
            if path.read_bytes() == contents:
                path.chmod(mode)
                return
            backup(path)
        fd, temporary = tempfile.mkstemp(prefix=".wechat-portal-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(contents)
                out.flush()
                os.fchmod(out.fileno(), mode)
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    put(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    interpreter = python or sys.executable
    launcher = "#!/bin/sh\nexec " + shlex.quote(interpreter) + " -I " + shlex.quote(str(repo / "run.py")) + ' "$@"\n'
    put(home / ".local/bin/wechat-portal", launcher.encode(), 0o700)
    for target in targets:
        if not target.is_symlink():
            target.symlink_to(skill_source, target_is_directory=True)
    removed = []
    for path in managed_commands:
        backup(path)
        path.unlink()
        removed.append(str(path))
    if migrate:
        for link in legacy_links:
            if link.is_symlink():
                backup(link)
                link.unlink()
                removed.append(str(link))
        for path in legacy_files:
            if path.exists():
                backup(path)
                path.unlink()
                removed.append(str(path))
        if legacy_runtime.is_dir():
            directory(backup_root, home)
            shutil.move(str(legacy_runtime), str(backup_root / "wechat-work-runtime"))
            removed.append(str(legacy_runtime))

    return {"installed": True, "command": str(home / ".local/bin/wechat-portal"), "repository": str(repo),
            "backend": str(binary), "agents": list(hosts), "removed": removed,
            "backup_dir": str(backup_root) if backup_root.exists() else None, "keys_modified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", help="wx-cli executable (default: vendor/wx-cli/wx-cli in this repository)")
    parser.add_argument("--account-dir", help="WeChat account directory containing db_storage; required on first install")
    parser.add_argument("--agent", choices=("codex", "claude", "both"), default="both")
    parser.add_argument("--migrate-wechat-work", action="store_true",
                        help="Back up and remove the former wechat-work skill links, launcher and runtime")
    args = parser.parse_args(argv)
    try:
        result = install(Path.home(), REPO, args.binary, args.account_dir, args.agent, args.migrate_wechat_work)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        # Exceptions can include config values; show the class and the checked path only.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print("Installation failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
