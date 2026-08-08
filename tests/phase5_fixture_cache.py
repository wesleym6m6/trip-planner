"""Shared cache for immutable Phase 5 default test checkpoints."""

from __future__ import annotations

from functools import cache, wraps
from importlib import import_module
from typing import Any, Callable, ParamSpec, TypeVar, cast


P = ParamSpec("P")
R = TypeVar("R")

_GUIDED_MODULE_NAMES = (
    "trip_planner.guided_draft",
    "trip_planner.guided_evidence_plan",
    "trip_planner.guided_itinerary",
    "trip_planner.guided_proposal",
    "trip_planner.guided_provider_execution_authorization_response",
    "trip_planner.guided_provider_execution_authorization_review",
    "trip_planner.guided_provider_execution_target_bindings",
    "trip_planner.guided_provider_execution_targets",
    "trip_planner.guided_provider_execution_time_recheck",
    "trip_planner.guided_provider_preflight",
    "trip_planner.guided_provider_preflight_response",
    "trip_planner.guided_provider_request_contract_materialization",
    "trip_planner.guided_provider_request_credential_binding_response",
    "trip_planner.guided_provider_request_credential_binding_review",
    "trip_planner.guided_provider_request_live_credential_binding_response",
    "trip_planner.guided_provider_request_live_credential_binding_review",
    "trip_planner.guided_provider_request_materialization_response",
    "trip_planner.guided_provider_request_materialization_review",
    "trip_planner.guided_provider_request_send_authorization_response",
    "trip_planner.guided_provider_request_send_authorization_review",
    "trip_planner.guided_provider_request_send_preparation",
    "trip_planner.guided_provider_scope",
    "trip_planner.guided_provider_scope_response",
    "trip_planner.guided_refinement",
)
_assessment_cache_installed = False


class _IdentityKey:
    """Hash an otherwise unhashable fixture by retained object identity."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __hash__(self) -> int:
        return id(self.value)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _IdentityKey) and self.value is other.value
        )


def _fixture_key(value: object) -> object:
    try:
        hash(value)
    except TypeError:
        return _IdentityKey(value)
    return value


def _memoize_immutable_assessment(
    assessment: Callable[..., Any],
) -> Callable[..., Any]:
    if getattr(assessment, "_phase5_checkpoint_cached", False):
        return assessment

    @cache
    def cached(
        positional: tuple[object, ...],
        keyword: tuple[tuple[str, object], ...],
    ) -> Any:
        args = tuple(
            item.value if isinstance(item, _IdentityKey) else item
            for item in positional
        )
        kwargs = {
            name: item.value if isinstance(item, _IdentityKey) else item
            for name, item in keyword
        }
        return assessment(*args, **kwargs)

    @wraps(assessment)
    def wrapped(*args: object, **kwargs: object) -> Any:
        positional = tuple(_fixture_key(item) for item in args)
        keyword = tuple(
            (name, _fixture_key(value))
            for name, value in sorted(kwargs.items())
        )
        return cached(positional, keyword)

    wrapped._phase5_checkpoint_cached = True  # type: ignore[attr-defined]
    return wrapped


def install_guided_assessment_checkpoint_cache() -> None:
    """Memoize only imported pure assessors inside the guided chain.

    A module's own public assessor remains untouched, so each contract test
    still executes its subject. Only identical calls to immutable upstream
    assessors are reused; explicit context, time, preimage, and branch changes
    produce a distinct key and execute normally.
    """

    global _assessment_cache_installed
    if _assessment_cache_installed:
        return

    for module_name in _GUIDED_MODULE_NAMES:
        module = import_module(module_name)
        for name, value in tuple(vars(module).items()):
            if not name.startswith("assess_guided_") or not callable(value):
                continue
            if getattr(value, "__module__", None) == module.__name__:
                continue
            setattr(module, name, _memoize_immutable_assessment(value))

    _assessment_cache_installed = True


def reuse_immutable_default_fixture(
    builder: Callable[P, R],
) -> Callable[P, R]:
    """Reuse only a builder's no-argument immutable default result.

    Explicit arguments always bypass the cache so drift, tamper, expiry, and
    alternate-branch cases continue to construct and validate their own graph.
    Phase 5 fixture objects are frozen dataclasses held in tuples; callers must
    not apply this decorator to mutable fixtures.
    """

    cached_default = cache(lambda: builder())

    @wraps(builder)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if args or kwargs:
            return builder(*args, **kwargs)
        return cached_default()

    return cast(Callable[P, R], wrapped)
