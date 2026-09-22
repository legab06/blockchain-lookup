from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Garde-fou commun : même si l'utilisateur saisit très peu de décimales,
# une correspondance approchée ne pourra jamais s'écarter de plus d'un
# millionième de l'unité de l'actif. Cela évite les faux positifs grossiers.
DEFAULT_MAX_APPROX_TOLERANCE = Decimal("0.000001")

# Référence commune pour les actifs natifs. Le futur moteur Bitcoin pourra
# directement réutiliser ce module avec BTC = 8 décimales (satoshi).
NATIVE_ASSET_DECIMALS = {
    "SOL": 9,
    "ETH": 18,
    "BTC": 8,
}


@dataclass(frozen=True)
class AmountCriterion:
    raw_text: str
    amount: Decimal
    decimal_places: int
    max_decimals: int
    tolerance: Decimal

    def classify(self, candidate: Decimal) -> str | None:
        """
        Retourne:
        - "exact" si la valeur est identique ;
        - "approximate" si elle est compatible avec la précision saisie ;
        - None sinon.

        La comparaison se fait sur la valeur absolue car les mouvements de
        solde peuvent être positifs ou négatifs.
        """
        candidate_abs = abs(Decimal(candidate))
        difference = abs(candidate_abs - self.amount)

        if difference == 0:
            return "exact"

        # Une recherche explicite de zéro reste stricte : accepter de petites
        # valeurs non nulles comme "environ zéro" créerait trop de bruit.
        if self.amount == 0:
            return None

        if self.tolerance > 0 and difference < self.tolerance:
            return "approximate"

        return None

    def difference(self, candidate: Decimal) -> Decimal:
        return abs(abs(Decimal(candidate)) - self.amount)


def parse_amount_criterion(
    value: str | Decimal | None,
    *,
    max_decimals: int,
    max_approx_tolerance: Decimal = DEFAULT_MAX_APPROX_TOLERANCE,
) -> AmountCriterion | None:
    """
    Parse un montant utilisateur en conservant sa précision déclarée.

    Exemple:
        "0.5472056" -> 7 décimales -> quantum 1e-7.
        La tolérance approchée sera donc < 1e-7 unité.

    Le plafond max_approx_tolerance empêche une saisie très peu précise
    (ex. "0.5") de créer une plage de recherche démesurée.
    """
    if value is None:
        return None

    raw = str(value).strip().replace(",", ".")
    if not raw:
        return None

    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Montant invalide.") from exc

    if not amount.is_finite():
        raise ValueError("Montant invalide.")

    if amount < 0:
        raise ValueError("Le montant doit être positif ou nul.")

    decimal_places = max(0, -amount.as_tuple().exponent)
    if decimal_places > max_decimals:
        raise ValueError(
            f"Ce montant ne peut pas dépasser {max_decimals} décimales."
        )

    quantum = Decimal(1).scaleb(-decimal_places)
    tolerance = min(quantum, max_approx_tolerance)

    return AmountCriterion(
        raw_text=raw,
        amount=amount,
        decimal_places=decimal_places,
        max_decimals=max_decimals,
        tolerance=tolerance,
    )


def match_label(match_quality: str | None) -> str:
    return {
        "exact": "Exacte",
        "approximate": "Approchée",
    }.get(match_quality, "")
