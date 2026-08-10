"""Dependency-free controller side of the fixed negotiation contract."""

from __future__ import annotations

import re
from collections.abc import Sequence


NEGOTIATION_VERSION = "1.0"
REVIEWED_PROTOCOL_VERSIONS = ("1.0",)
MAX_PROTOCOL_VERSIONS = 8
_VERSION = re.compile(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")


class ProtocolNegotiationError(ValueError):
    pass


class NoCommonProtocolVersion(ProtocolNegotiationError):
    pass


def _key(value: str) -> tuple[int, int]:
    matched = _VERSION.fullmatch(value)
    if matched is None:
        raise ProtocolNegotiationError("invalid protocol version")
    return int(matched.group(1)), int(matched.group(2))


def _versions(value: object, *, json_array: bool) -> tuple[str, ...]:
    valid = type(value) is list if json_array else (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    )
    if not valid:
        raise ProtocolNegotiationError("protocol versions must be an array")
    versions = tuple(value)  # type: ignore[arg-type]
    if not 1 <= len(versions) <= MAX_PROTOCOL_VERSIONS:
        raise ProtocolNegotiationError("protocol version count is invalid")
    if any(type(item) is not str for item in versions):
        raise ProtocolNegotiationError("protocol version is invalid")
    keys = tuple(_key(item) for item in versions)
    if len(set(versions)) != len(versions) or keys != tuple(sorted(keys, reverse=True)):
        raise ProtocolNegotiationError("protocol versions are not a unique highest-first set")
    return versions  # type: ignore[return-value]


def select_protocol_version(
    agent_versions: object,
    controller_versions: object = REVIEWED_PROTOCOL_VERSIONS,
) -> str:
    offered = _versions(agent_versions, json_array=True)
    reviewed = _versions(controller_versions, json_array=False)
    common = set(offered).intersection(reviewed)
    if not common:
        raise NoCommonProtocolVersion("no common reviewed protocol version")
    return max(common, key=_key)
