"""Decide whether a configured state root lies inside the package source.

The installer calls this before it creates a directory or contacts Docker.
Paths are canonicalized with the platform realpath, so existing symlink
ancestors and parent components count. A prefix sibling of the source, such
as a directory whose name only begins with the source name, is outside.
"""
import os
import sys


def resolve(path, cwd):
    if path == "":
        raise ValueError("empty path")
    if not os.path.isabs(path):
        if cwd == "":
            raise ValueError("empty cwd")
        path = os.path.join(cwd, path)
    resolved = os.path.realpath(path)
    if resolved == "" or not os.path.isabs(resolved):
        raise ValueError("unresolved path")
    return os.path.normpath(resolved)


def contained(candidate, source):
    if candidate == source:
        return True
    prefix = os.sep if source == os.sep else source + os.sep
    return candidate.startswith(prefix)


def main(argv):
    if len(argv) != 4:
        return 2
    try:
        source = resolve(argv[1], argv[3])
        candidate = resolve(argv[2], argv[3])
    except (OSError, ValueError):
        return 2
    if contained(candidate, source):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
