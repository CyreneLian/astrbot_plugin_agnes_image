"""Register temporary reference-image tokens without patching AstrBot."""

from inspect import Parameter, signature


async def register_reference_file(file_token_service, file_path: str) -> str:
    """Request a repeatable token when the installed API supports it.

    Older or non-introspectable APIs use their original registration behavior.
    Errors raised by register_file itself must propagate without a retry.
    """
    register_file = file_token_service.register_file
    try:
        parameters = signature(register_file).parameters
    except (TypeError, ValueError):
        supports_single_use = False
    else:
        single_use = parameters.get("single_use")
        supports_single_use = (
            single_use is not None
            and single_use.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
        ) or (
            single_use is None
            and any(param.kind == Parameter.VAR_KEYWORD for param in parameters.values())
        )

    if supports_single_use:
        return await register_file(file_path, single_use=False)
    return await register_file(file_path)
