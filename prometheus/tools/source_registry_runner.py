#!/usr/bin/env python3
"""Reproduce generated Prometheus source registries in a hermetic sandbox."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


RUNNER_ID = "netdata-prometheus-source-registry-v1"
MANIFEST_NAME = "SOURCE-REGISTRY.generator.yaml"
REGISTRY_NAME = "SOURCE-REGISTRY.yaml"
GENERATOR_DIRECTORY = "generator"
GENERATOR_ENTRYPOINT = "generate.py"
SHARED_GENERATOR_RUNTIME = "source_registry_client_python.py"
ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
GENERATOR_FILE_PATTERN = re.compile(r"[a-z][a-z0-9_]*\.py\Z")


@dataclass(frozen=True)
class Upstream:
    repository: str
    commit: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    profile: str
    upstreams: dict[str, Upstream]


def parse_manifest(path: Path) -> Manifest:
    top: dict[str, str] = {}
    upstreams: dict[str, dict[str, object]] = {}
    current_upstream: str | None = None
    in_paths = False

    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line:
            raise ValueError(f"{path}:{number}: tabs are not allowed")
        if raw_line == "upstreams:":
            if "upstreams" in top:
                raise ValueError(f"{path}:{number}: duplicate upstreams")
            top["upstreams"] = ""
            current_upstream = None
            in_paths = False
            continue
        match = re.fullmatch(r"([a-z_]+): ([^#\s][^#]*)", raw_line)
        if match:
            key, value = match.groups()
            if key in top:
                raise ValueError(f"{path}:{number}: duplicate {key}")
            top[key] = value.rstrip()
            current_upstream = None
            in_paths = False
            continue
        match = re.fullmatch(r"  ([a-z][a-z0-9_]*):", raw_line)
        if match and "upstreams" in top:
            current_upstream = match.group(1)
            if current_upstream in upstreams:
                raise ValueError(f"{path}:{number}: duplicate upstream {current_upstream}")
            upstreams[current_upstream] = {"paths": []}
            in_paths = False
            continue
        match = re.fullmatch(r"    (repository|commit): ([^#\s][^#]*)", raw_line)
        if match and current_upstream is not None:
            key, value = match.groups()
            if key in upstreams[current_upstream]:
                raise ValueError(f"{path}:{number}: duplicate upstream {key}")
            upstreams[current_upstream][key] = value.rstrip()
            in_paths = False
            continue
        if raw_line == "    paths:" and current_upstream is not None:
            if in_paths:
                raise ValueError(f"{path}:{number}: duplicate paths")
            in_paths = True
            continue
        match = re.fullmatch(r"      - ([^#\s][^#]*)", raw_line)
        if match and current_upstream is not None and in_paths:
            upstreams[current_upstream]["paths"].append(match.group(1).rstrip())
            continue
        raise ValueError(f"{path}:{number}: unsupported manifest syntax")

    if set(top) != {"version", "profile", "runner", "upstreams"}:
        raise ValueError(f"{path}: expected version, profile, runner, and upstreams")
    if top["version"] != "v1" or top["runner"] != RUNNER_ID:
        raise ValueError(f"{path}: unsupported version or runner")
    if not ID_PATTERN.fullmatch(top["profile"]):
        raise ValueError(f"{path}: invalid profile {top['profile']!r}")
    if not upstreams:
        raise ValueError(f"{path}: upstreams must not be empty")

    parsed: dict[str, Upstream] = {}
    for upstream_id, fields in sorted(upstreams.items()):
        if set(fields) != {"repository", "commit", "paths"}:
            raise ValueError(f"{path}: upstream {upstream_id} is incomplete")
        repository = str(fields["repository"])
        commit = str(fields["commit"])
        paths = tuple(str(value) for value in fields["paths"])
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError(f"{path}: invalid repository {repository!r}")
        if not COMMIT_PATTERN.fullmatch(commit):
            raise ValueError(f"{path}: commit for {upstream_id} must be a full lowercase SHA-1")
        if not paths or len(paths) != len(set(paths)):
            raise ValueError(f"{path}: paths for {upstream_id} must be nonempty and unique")
        for source_path in paths:
            _validate_relative_path(path, upstream_id, source_path)
        parsed[upstream_id] = Upstream(repository, commit, paths)
    return Manifest(top["profile"], parsed)


def verify_profile(profile_directory: Path) -> None:
    manifest_path = profile_directory / MANIFEST_NAME
    registry_path = profile_directory / REGISTRY_NAME
    generator_directory = profile_directory / GENERATOR_DIRECTORY
    manifest = parse_manifest(manifest_path)
    if manifest.profile != profile_directory.name:
        raise ValueError(
            f"{manifest_path}: profile {manifest.profile!r} does not match directory {profile_directory.name!r}"
        )
    if not registry_path.is_file() or registry_path.is_symlink():
        raise ValueError(f"{registry_path}: committed registry must be a regular file")
    generator_files = _generator_files(generator_directory)

    with (
        tempfile.TemporaryDirectory(prefix=f"netdata-{manifest.profile}-registry-source-") as private_temporary,
        tempfile.TemporaryDirectory(
            prefix=f"netdata-{manifest.profile}-registry-sandbox-", dir="/tmp"
        ) as sandbox_temporary,
    ):
        private_root = Path(private_temporary)
        work = Path(sandbox_temporary)
        upstream_root = work / "upstreams"
        upstream_root.mkdir()
        for upstream_id, upstream in manifest.upstreams.items():
            _materialize_upstream(private_root, upstream_root, upstream_id, upstream)
        target_generator = work / GENERATOR_DIRECTORY
        target_generator.mkdir()
        for source in generator_files:
            shutil.copyfile(source, target_generator / source.name)
        shutil.copyfile(
            Path(__file__).with_name(SHARED_GENERATOR_RUNTIME),
            target_generator / SHARED_GENERATOR_RUNTIME,
        )
        _make_read_only(work)

        _run_isolated(
            work,
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                GENERATOR_DIRECTORY,
                "-p",
                "test_*.py",
            ],
            capture=False,
        )
        generator_entrypoint = f"{GENERATOR_DIRECTORY}/{GENERATOR_ENTRYPOINT}"
        generated = _run_isolated(
            work,
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import runpy,sys;"
                f"sys.path.insert(0,{GENERATOR_DIRECTORY!r});"
                f"runpy.run_path({generator_entrypoint!r},run_name='__main__')",
            ],
            capture=True,
        )
        committed = registry_path.read_bytes()
        if generated != committed:
            raise ValueError(
                f"{registry_path}: generated output differs; run the fixed registry runner and commit the result"
            )


def _generator_files(directory: Path) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"{directory}: generator must be a real directory")
    files: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file() or not GENERATOR_FILE_PATTERN.fullmatch(entry.name):
            raise ValueError(f"{directory}: unsupported generator entry {entry.name!r}")
        files.append(entry)
    names = {entry.name for entry in files}
    if GENERATOR_ENTRYPOINT not in names:
        raise ValueError(f"{directory}: missing {GENERATOR_ENTRYPOINT}")
    if not any(name.startswith("test_negative_") for name in names):
        raise ValueError(f"{directory}: missing a test_negative_*.py fail-closed test")
    return files


def _materialize_upstream(
    temporary_root: Path,
    upstream_root: Path,
    upstream_id: str,
    upstream: Upstream,
) -> None:
    repository = temporary_root / f"git-{upstream_id}"
    _run(["git", "init", "--quiet", str(repository)])
    remote = f"https://github.com/{upstream.repository}.git"
    _run(["git", "-C", str(repository), "remote", "add", "origin", remote])
    _run(
        ["git", "-C", str(repository), "fetch", "--quiet", "--depth=1", "origin", upstream.commit],
        timeout=600,
    )
    resolved = _run(
        ["git", "-C", str(repository), "rev-parse", "FETCH_HEAD"],
        capture=True,
    ).decode().strip()
    if resolved != upstream.commit:
        raise ValueError(f"{upstream.repository}: fetched {resolved}, expected {upstream.commit}")

    for source_path in upstream.paths:
        tree_line = _run(
            ["git", "-C", str(repository), "ls-tree", upstream.commit, "--", source_path],
            capture=True,
        ).decode()
        match = re.fullmatch(r"(100644|100755) blob [0-9a-f]{40}\t(.+)\n", tree_line)
        if match is None or match.group(2) != source_path:
            raise ValueError(f"{upstream.repository}@{upstream.commit}:{source_path} is not one regular blob")
        content = _run(
            ["git", "-C", str(repository), "show", f"{upstream.commit}:{source_path}"],
            capture=True,
        )
        destination = upstream_root / upstream_id / PurePosixPath(source_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _run_isolated(work: Path, command: list[str], capture: bool) -> bytes:
    system = platform.system()
    if system == "Linux":
        isolated = [
            "sudo",
            "-n",
            "unshare",
            "--net",
            "--",
            "setpriv",
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            "--",
            *command,
        ]
    elif system == "Darwin":
        policy = "(version 1) (allow default) (deny network*) (deny file-write*)"
        isolated = ["/usr/bin/sandbox-exec", "-p", policy, *command]
    else:
        raise ValueError(f"unsupported hermetic-runner platform {system!r}")
    return _run(isolated, cwd=work, capture=capture, timeout=300)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: int = 120,
) -> bytes:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace") if result.stderr else ""
        raise ValueError(f"command failed ({result.returncode}): {command!r}\n{stderr}")
    if capture and result.stderr:
        raise ValueError(f"command wrote to stderr: {command!r}\n{result.stderr.decode(errors='replace')}")
    return result.stdout or b""


def _make_read_only(root: Path) -> None:
    for directory, directories, files in os.walk(root, topdown=False):
        for name in files:
            (Path(directory) / name).chmod(0o444)
        for name in directories:
            (Path(directory) / name).chmod(0o555)
    root.chmod(0o555)


def _validate_relative_path(manifest: Path, upstream_id: str, value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"{manifest}: invalid path {value!r} for upstream {upstream_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profiles", nargs="*", help="profile IDs; default: every generated registry")
    arguments = parser.parse_args()

    testdata_root = Path(__file__).resolve().parents[2]
    profiles_root = testdata_root / "prometheus" / "profiles"
    if arguments.profiles:
        invalid = [profile for profile in arguments.profiles if not ID_PATTERN.fullmatch(profile)]
        if invalid:
            raise ValueError(f"invalid profile IDs: {invalid}")
        profile_directories = [profiles_root / profile for profile in sorted(set(arguments.profiles))]
    else:
        profile_directories = sorted(path.parent for path in profiles_root.glob(f"*/{MANIFEST_NAME}"))
    if not profile_directories:
        raise ValueError("no generated source registries found")
    for profile_directory in profile_directories:
        print(f"verify {profile_directory.name}", flush=True)
        verify_profile(profile_directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
