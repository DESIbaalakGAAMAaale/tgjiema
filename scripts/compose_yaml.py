#!/usr/bin/env python3
"""Load Docker Compose YAML files that use Compose-specific tags.

Docker Compose extends YAML with special tags such as ``!override``
(replace list/mapping instead of merging) and ``!reset`` (reset to
default).  PyYAML's ``safe_load`` cannot parse these tags and raises
``yaml.constructor.ConstructorError``.  This module provides a drop-in
loader that registers constructors for the Compose-specific tags so
that test/contract code can inspect raw ``docker-compose*.yml`` files
without running ``docker compose config``.

Usage::

    from scripts.compose_yaml import load_compose_yaml

    doc = load_compose_yaml(Path("docker-compose.secretless.yml"))

The constructor for ``!override`` returns the wrapped value as-is
(sequence or mapping); callers see the same structure that Docker
Compose would resolve after applying the override.  ``!reset`` returns
``None`` so callers can detect a reset marker; if callers need the
resolved default they must run ``docker compose config``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ComposeSafeLoader(yaml.SafeLoader):
    """SafeLoader that understands Docker Compose extension tags."""

    pass


def _construct_override(
    loader: yaml.SafeLoader,
    node: yaml.Node,
) -> Any:
    """Return the wrapped value of an ``!override`` tag.

    ``!override`` may wrap either a sequence or a mapping.  We construct
    the underlying node using the same semantics as ``SafeLoader``
    so callers receive a plain ``list`` / ``dict``.
    """
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


def _construct_reset(
    loader: yaml.SafeLoader,
    node: yaml.Node,
) -> Any:
    """Return ``None`` for ``!reset`` (resolved default needs compose)."""
    return None


def _construct_merge(
    loader: yaml.SafeLoader,
    node: yaml.Node,
) -> Any:
    """Return the wrapped value of an ``!merge`` tag (explicit merge)."""
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


# Register Compose-specific tags on the module-level loader class.
# Multi-constructor ``tag:yaml.org,2002:...`` is not needed here because
# Docker Compose uses local tags (single exclamation prefix).
ComposeSafeLoader.add_constructor("!override", _construct_override)
ComposeSafeLoader.add_constructor("!reset", _construct_reset)
ComposeSafeLoader.add_constructor("!merge", _construct_merge)
# ``!include`` / ``!env`` are file/env directives that compose resolves at
# render time — when tests inspect raw YAML they expect the tag to be
# surfaced as a placeholder string, not raise a constructor error.
ComposeSafeLoader.add_constructor(
    "!include",
    lambda loader, node: loader.construct_scalar(node),
)
ComposeSafeLoader.add_constructor(
    "!env",
    lambda loader, node: loader.construct_scalar(node),
)


def load_compose_yaml(path: Path) -> Any:
    """Load a Docker Compose YAML file, returning the parsed document.

    Handles Compose-specific tags (``!override`` / ``!reset`` / ``!merge``)
    so callers can inspect raw compose files without running
    ``docker compose config``.

    Args:
        path: Path to a ``docker-compose*.yml`` file.

    Returns:
        The parsed YAML document (typically a ``dict`` with a ``services``
        key).  Returns ``None`` for empty files.
    """
    # ComposeSafeLoader subclasses yaml.SafeLoader; the custom constructors
    # only handle Compose extension tags (!override/!reset/!merge/!include/
    # !env) and return plain Python types (list/dict/scalar/None), so no
    # arbitrary object instantiation is possible.
    return yaml.load(  # nosec B506 — safe loader subclass, no python/object tags
        path.read_text(encoding="utf-8"),
        Loader=ComposeSafeLoader,
    )


def load_compose_yaml_text(text: str) -> Any:
    """Load Docker Compose YAML from a string, handling Compose tags."""
    # See load_compose_yaml() for the safety rationale: ComposeSafeLoader
    # subclasses yaml.SafeLoader and only registers constructors for Compose
    # extension tags that return plain Python types.
    return yaml.load(text, Loader=ComposeSafeLoader)  # nosec B506 — safe loader
