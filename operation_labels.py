OPERATION_TYPE_LABELS = {
    "transfer": "Transfert natif",
    "token_transfer": "Transfert de token",
    "swap_probable": "Swap probable",
    "balance_delta": "Variation de solde",
}


def operation_type_label(operation_type: str, network: str) -> str:
    if network == "Bitcoin" and operation_type == "transfer":
        return "Sortie BTC"
    return OPERATION_TYPE_LABELS.get(operation_type, operation_type)
