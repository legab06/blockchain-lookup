"""Messages de résultat liés à la complétude de la recherche."""

from blockchain_lookup.domain.models import SearchResult


def partial_search_warning(result: SearchResult) -> str | None:
    if result["search_completeness"] == "complete":
        return None

    count = result["failed_blocks"]
    unit = "slot" if result["network"] == "Solana" else "bloc"
    label = f"{count} {unit}{'s' if count != 1 else ''}"
    return (
        f"Recherche partielle : {label} "
        f"{'n’a' if count == 1 else 'n’ont'} pas pu être entièrement "
        f"{'analysé' if count == 1 else 'analysés'}. "
        "Des correspondances peuvent manquer."
    )


def no_matches_message(result: SearchResult, target_label: str | None = None) -> str:
    if result["search_completeness"] == "partial":
        subject = f" pour {target_label}" if target_label else ""
        count = result["failed_blocks"]
        unit = "slot" if result["network"] == "Solana" else "bloc"
        label = f"{count} {unit}{'s' if count != 1 else ''}"
        observed = "slots" if result["network"] == "Solana" else "blocs"
        return (
            f"Aucune correspondance{subject} dans les {observed} analysés. "
            f"L’absence de résultat n’est pas concluante pour {label} "
            f"non entièrement {'analysé' if count == 1 else 'analysés'}."
        )

    if target_label:
        return (
            f"Aucune correspondance pour {target_label}. "
            "Consultez Transactions et Opérations pour examiner toute la période."
        )
    return "Aucune correspondance trouvée."
