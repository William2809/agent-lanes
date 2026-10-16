#!/usr/bin/env python3
"""Resolve exact-command runner needs from a fail-closed JSON policy."""
import json
from pathlib import Path
import re
import sys


INVALID = 74
ABSENT = 10
CAPABILITY = re.compile(r"[A-Za-z0-9_.-]+")


class InvalidPolicy(Exception):
    pass


def object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPolicy("duplicate field")
        result[key] = value
    return result


def capabilities(value, *, required):
    if not isinstance(value, list) or (required and not value):
        raise InvalidPolicy("invalid capabilities")
    if any(type(capability) is not str or not CAPABILITY.fullmatch(capability)
           for capability in value):
        raise InvalidPolicy("invalid capabilities")
    return value


def policy(path):
    try:
        data = json.loads(Path(path).read_text(), object_pairs_hook=object_without_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, InvalidPolicy) as error:
        raise InvalidPolicy("invalid route policy") from error
    if type(data) is not dict or set(data) != {"version", "projects"}:
        raise InvalidPolicy("invalid route policy")
    if type(data["version"]) is not int or data["version"] != 1:
        raise InvalidPolicy("invalid route policy")
    projects = data["projects"]
    if type(projects) is not dict:
        raise InvalidPolicy("invalid projects")
    for project, rule in projects.items():
        if type(project) is not str or not project or type(rule) is not dict:
            raise InvalidPolicy("invalid project")
        if set(rule) != {"default_needs", "commands"}:
            raise InvalidPolicy("invalid project")
        capabilities(rule["default_needs"], required=True)
        commands = rule["commands"]
        if type(commands) is not dict:
            raise InvalidPolicy("invalid commands")
        for command, needs in commands.items():
            if type(command) is not str or not command:
                raise InvalidPolicy("invalid command")
            capabilities(needs, required=False)
    return projects


def needs(path, project, command=None):
    projects = policy(path)
    if project not in projects:
        return None
    rule = projects[project]
    return rule["commands"].get(command, rule["default_needs"])


def main(argv):
    if len(argv) not in (3, 4) or not argv[2]:
        return INVALID
    try:
        result = needs(argv[1], argv[2], argv[3] if len(argv) == 4 else None)
    except InvalidPolicy:
        return INVALID
    if result is None:
        return ABSENT
    print(" ".join(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
