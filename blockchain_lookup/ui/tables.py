import pandas as pd
import streamlit as st

from blockchain_lookup.ui.operation_labels import operation_type_label


DISPLAY_COLUMN_LABELS = {
    "block": "Bloc",
    "block_time_utc": "Date / heure UTC",
    "transaction_index": "N° dans le bloc",
    "signature": "Transaction",
    "status": "Statut",
    "fee_sol": "Frais (SOL)",
    "fee_eth": "Frais (ETH)",
    "account_count": "Nb. de comptes",
    "accounts": "Comptes concernés",
    "operation_count": "Nb. d'opérations",
    "operation_summary": "Opération(s) détectée(s)",
    "operation_type": "Type",
    "sent": "Envoyé",
    "received": "Reçu",
    "source": "Expéditeur",
    "destination": "Destinataire",
    "account": "Compte concerné",
    "evidence": "Détection",
    "detail": "Détail",
    "match_role": "Correspondance",
    "match_quality": "Précision",
    "matched_amount": "Montant",
    "matched_asset": "Actif",
    "asset": "Actif",
    "balance_before": "Solde avant",
    "balance_after": "Solde après",
    "delta_amount": "Variation",
    "absolute_delta_amount": "Variation absolue",
    "match_target": "Montant recherché ?",
    "error": "Erreur",
    "explorer": "Explorateur",
    "secondary_explorer": "Explorateur secondaire",
}

TABLE_COLUMNS = {
    "matches": [
        "block_time_utc",
        "matched_amount",
        "matched_asset",
        "match_quality",
        "match_role",
        "operation_type",
        "sent",
        "received",
        "source",
        "destination",
        "account",
        "signature",
        "transaction_index",
        "detail",
        "secondary_explorer",
        "explorer",
        "block",
    ],
    "transactions": [
        "block_time_utc",
        "operation_summary",
        "operation_count",
        "status",
        "signature",
        "fee_sol",
        "fee_eth",
        "account_count",
        "accounts",
        "secondary_explorer",
        "explorer",
        "block",
        "transaction_index",
        "error",
    ],
    "operations": [
        "block_time_utc",
        "operation_type",
        "sent",
        "received",
        "source",
        "destination",
        "account",
        "evidence",
        "signature",
        "transaction_index",
        "detail",
        "secondary_explorer",
        "explorer",
        "block",
    ],
    "movements": [
        "block_time_utc",
        "asset",
        "absolute_delta_amount",
        "delta_amount",
        "account",
        "signature",
        "transaction_index",
        "match_target",
        "secondary_explorer",
        "explorer",
        "block",
    ],
}

def localize_rows(rows: list[dict], network: str) -> list[dict]:
    localized: list[dict] = []
    for row in rows:
        item = dict(row)
        if item.get("solscan") and not item.get("secondary_explorer"):
            item["secondary_explorer"] = item["solscan"]
        if "operation_type" in item:
            item["operation_type"] = operation_type_label(item["operation_type"], network)
        if "match_type" in item:
            item["operation_type"] = operation_type_label(item["match_type"], network)
        if "match_quality" in item:
            item["match_quality"] = {
                "exact": "Exacte",
                "approximate": "Approchée",
            }.get(item["match_quality"], item["match_quality"])
        if item.get("match_target") == "EXACT":
            item["match_target"] = "Exact"
        elif item.get("match_target") == "APPROX":
            item["match_target"] = "Approché"
        localized.append(item)
    return localized


def _display_labels(network: str) -> dict[str, str]:
    labels = dict(DISPLAY_COLUMN_LABELS)
    if network == "Solana":
        labels["explorer"] = "Solana Explorer"
        labels["secondary_explorer"] = "Solscan"
    elif network == "Ethereum":
        labels["explorer"] = "Etherscan"
        labels["secondary_explorer"] = "Blockscout"
    elif network == "Bitcoin":
        labels["explorer"] = "mempool.space"
        labels["secondary_explorer"] = "Blockstream"
    return labels


def prepare_table_dataframe(
    rows: list[dict],
    *,
    table_kind: str,
    network: str,
) -> pd.DataFrame:
    columns = TABLE_COLUMNS[table_kind]
    labels = _display_labels(network)

    table = pd.DataFrame(localize_rows(rows, network)).reindex(columns=columns)
    table = table.dropna(axis=1, how="all").rename(columns=labels)

    if "Statut" in table:
        table["Statut"] = table["Statut"].replace(
            {"SUCCESS": "Réussie", "FAILED": "Échouée", "UNKNOWN": "Statut inconnu"}
        )

    for column in (
        "Opération(s) détectée(s)",
        "Envoyé",
        "Reçu",
        "Expéditeur",
        "Destinataire",
        "Compte concerné",
    ):
        if column in table:
            table[column] = table[column].replace("", "—").fillna("—")

    return table


def show_table(
    rows: list[dict],
    *,
    table_kind: str,
    empty_message: str,
    network: str,
) -> None:
    if not rows:
        st.info(empty_message)
        return

    table = prepare_table_dataframe(rows, table_kind=table_kind, network=network)
    labels = _display_labels(network)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        lazy=True,
        column_config={
            labels["explorer"]: st.column_config.LinkColumn(
                labels["explorer"],
                display_text="Ouvrir",
            ),
            labels["secondary_explorer"]: st.column_config.LinkColumn(
                labels["secondary_explorer"],
                display_text="Ouvrir",
            ),
        },
    )


def filter_rows(
    rows: list[dict],
    query: str,
    *,
    only_success: bool = False,
) -> list[dict]:
    query = query.strip().casefold()
    filtered: list[dict] = []

    for row in rows:
        if only_success and row.get("status") != "SUCCESS":
            continue

        if query:
            searchable = " ".join(str(value) for value in row.values()).casefold()
            if query not in searchable:
                continue

        filtered.append(row)

    return filtered
