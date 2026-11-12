"""Parameter discovery and expansion for the interactive favorite browser."""

from __future__ import annotations

import re
from uuid import uuid4

from mycli.packages.special.favoritequeries import analyze_favorite_query_template
from mycli.packages.special.iocommands import parse_favorite_query_args, render_favorite_query, restore_favorite_query_args

POSITIONAL = re.compile(r'\$\d+')


def _has_template(query: str) -> bool:
    return any(marker in query for marker in ('{{', '{%', '{#'))


def parameter_names(query: str) -> tuple[list[str], bool]:
    """Return numbered inputs, named inputs and whether dynamic keys are used."""
    keys, dynamic = analyze_favorite_query_template(query) if _has_template(query) else (set(), False)
    positions = set(POSITIONAL.findall(query))
    if positions & keys:
        raise ValueError('A template key conflicts with a numbered parameter.')
    return sorted(positions, key=lambda name: int(name[1:])) + sorted(keys), dynamic


def render_parameters(query: str, values: dict[str, str], extra: str = '') -> str:
    """Use existing template semantics without reinterpreting input as a template."""
    arguments, extra_named = parse_favorite_query_args(extra)
    if arguments:
        raise ValueError('Additional arguments must use --key=value.')
    named = {key: value for key, value in values.items() if not POSITIONAL.fullmatch(key) and value != ''}
    if named.keys() & extra_named.keys():
        raise ValueError('A named parameter was supplied twice.')
    named.update(extra_named)
    markers: dict[str, str] = {}
    prefix = f'__mycli_form_{uuid4().hex}_'

    def protect(match: re.Match[str]) -> str:
        key = match.group()
        if key not in values:
            raise ValueError(f'Missing parameter {key}.')
        marker = f'{prefix}{key[1:]}__'
        markers[marker] = values[key]
        return marker

    prepared = POSITIONAL.sub(protect, query)
    rendered = render_favorite_query(prepared, named) if named or _has_template(prepared) else prepared
    return restore_favorite_query_args(rendered, markers)
