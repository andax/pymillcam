"""Post-processors: IR → controller-specific G-code.

``POST_REGISTRY`` maps the lower-cased ``MachineDefinition.controller``
string to the concrete post-processor class. The UI uses
``get_post(controller)`` to pick the right post from the project's
machine block so shops running different firmware don't have to fork
PyMillCAM — they just set ``controller`` on their machine definition.

Adding a new post means: subclass ``BasicGcodePost`` (or implement
``PostProcessor`` directly if the dialect diverges), then register the
class here.
"""
from pymillcam.post._basic import BasicGcodePost
from pymillcam.post.base import PostProcessor
from pymillcam.post.grbl import GrblPostProcessor
from pymillcam.post.uccnc import UccncPostProcessor

POST_REGISTRY: dict[str, type[PostProcessor]] = {
    "uccnc": UccncPostProcessor,
    "grbl": GrblPostProcessor,
}


def registered_controller_names() -> list[str]:
    """Lower-cased controller keys in registration order — used by the UI
    to populate the Machine dialog's controller picker."""
    return list(POST_REGISTRY.keys())


def get_post(controller: str) -> PostProcessor:
    """Resolve a controller string to a concrete post-processor instance.

    Lookup is case-insensitive. Unknown controllers fall back to UCCNC —
    matches the model's default so projects created before a specific
    controller registers still generate *something* rather than erroring
    out at generate time.
    """
    cls = POST_REGISTRY.get(controller.lower(), UccncPostProcessor)
    return cls()


__all__ = [
    "POST_REGISTRY",
    "BasicGcodePost",
    "GrblPostProcessor",
    "PostProcessor",
    "UccncPostProcessor",
    "get_post",
    "registered_controller_names",
]
