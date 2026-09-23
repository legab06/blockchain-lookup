"""Messages de résultat liés à la complétude de la recherche."""

from blockchain_lookup.domain.models import SearchResult


def _coverage_gap_label(result: SearchResult) -> str:
    missing = []
    if result.get("coverage_missing_before"):
        missing.append("début")
    if result.get("coverage_missing_after"):
        missing.append("fin")
    return " et ".join(missing)


def partial_search_warning(result: SearchResult) -> str | None:
    if result["search_completeness"] == "complete":
        return None

    count = result["failed_blocks"]
    unit = "slot" if result["network"] == "Solana" else "bloc"
    reasons = []
    if count:
        label = f"{count} {unit}{'s' if count != 1 else ''}"
        reasons.append(
            f"{label} {'n’a' if count == 1 else 'n’ont'} pas pu être entièrement "
            f"{'analysé' if count == 1 else 'analysés'}"
        )
    gap = _coverage_gap_label(result)
    if gap:
        reasons.append(f"couverture temporelle incomplète au {gap} de la fenêtre")
    if not reasons:
        reasons.append("analyse incomplète")
    return "Recherche partielle : " + "; ".join(reasons) + ". Des correspondances peuvent manquer."


def no_matches_message(result: SearchResult, target_label: str | None = None) -> str:
    if result["search_completeness"] == "partial":
        subject = f" pour {target_label}" if target_label else ""
        count = result["failed_blocks"]
        unit = "slot" if result["network"] == "Solana" else "bloc"
        observed = "slots" if result["network"] == "Solana" else "blocs"
        missing = []
        if count:
            label = f"{count} {unit}{'s' if count != 1 else ''}"
            missing.append(
                f"{label} non entièrement {'analysé' if count == 1 else 'analysés'}"
            )
        if _coverage_gap_label(result):
            missing.append("la portion de fenêtre temporelle non couverte")
        if not missing:
            missing.append("une partie de la recherche")
        return (
            f"Aucune correspondance{subject} dans les {observed} analysés. "
            "L’absence de résultat n’est pas concluante pour "
            + " et ".join(missing) + "."
        )

    if target_label:
        return (
            f"Aucune correspondance pour {target_label}. "
            "Consultez Transactions et Opérations pour examiner toute la période."
        )
    return "Aucune correspondance trouvée."
