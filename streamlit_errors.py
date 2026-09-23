import logging


GENERIC_SEARCH_ERROR = (
    "Une erreur inattendue est survenue pendant la recherche. "
    "Réessayez plus tard."
)


def log_unexpected_search_error(logger: logging.Logger, error: Exception) -> str:
    """Loggue le traceback côté serveur et renvoie un message sûr pour l’UI."""
    logger.error(
        "Unexpected error during blockchain search",
        exc_info=(type(error), error, error.__traceback__),
    )
    return GENERIC_SEARCH_ERROR
