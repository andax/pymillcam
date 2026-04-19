"""Post-processor registry: controller-string → concrete post."""
from __future__ import annotations

from pymillcam.post import (
    POST_REGISTRY,
    GrblPostProcessor,
    UccncPostProcessor,
    get_post,
    registered_controller_names,
)


def test_uccnc_and_grbl_are_registered() -> None:
    assert "uccnc" in POST_REGISTRY
    assert "grbl" in POST_REGISTRY


def test_get_post_is_case_insensitive() -> None:
    assert isinstance(get_post("UCCNC"), UccncPostProcessor)
    assert isinstance(get_post("Grbl"), GrblPostProcessor)


def test_get_post_unknown_falls_back_to_uccnc() -> None:
    """Unknown controller strings shouldn't raise at generate time;
    default to UCCNC so projects created before a specific post was
    registered still produce output."""
    assert isinstance(get_post("made-up-controller"), UccncPostProcessor)


def test_registered_names_includes_all_registered_posts() -> None:
    names = registered_controller_names()
    assert set(names) >= {"uccnc", "grbl"}
