from __future__ import annotations

from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

from solana_engine import SolanaSearchError, search_solana_window


st.set_page_config(
    page_title="Blockchain Lookup",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        .block-container { padding-top: 2rem; padding-bottom: 4rem; }
        div[data-testid="stMetric"] {
            border: 1px solid rgba(128, 128, 128, 0.18);
            border-radius: 14px;
            padding: 12px 16px;
        }
        .lookup-subtitle {
            color: #8a8f98;
            margin-top: -0.6rem;
            margin-bottom: 1.8rem;
        }
        .utc-badge {
            display: inline-block;
            border: 1px solid rgba(128, 128, 128, 0.25);
            border-radius: 999px;
            padding: 0.2rem 0.65rem;
            font-size: 0.82rem;
            color: #8a8f98;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def to_csv_bytes(rows: list[dict]) -> bytes:
    return (
        pd.DataFrame(rows)
        .to_csv(index=False, sep=";", encoding="utf-8-sig")
        .encode("utf-8-sig")
    )


DISPLAY_COLUMN_LABELS = {
    "block": "Bloc",
    "block_time_utc": "Date / heure UTC",
    "transaction_index": "N° dans le bloc",
    "signature": "Transaction",
    "status": "Statut",
    "fee_sol": "Frais (SOL)",
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
    "solscan": "Solscan",
}

TABLE_COLUMNS = {
    "matches": [
        "block_time_utc",
        "matched_amount",
        "matched_asset",
        "match_role",
        "operation_type",
        "sent",
        "received",
        "source",
        "destination",
        "account",
        "signature",
        "detail",
        "solscan",
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
        "account_count",
        "accounts",
        "solscan",
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
        "detail",
        "solscan",
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
        "match_target",
        "solscan",
        "explorer",
        "block",
    ],
}

OPERATION_TYPE_LABELS = {
    "transfer": "Transfert SOL",
    "token_transfer": "Transfert de token",
    "swap_probable": "Swap probable",
    "balance_delta": "Variation de solde",
}


def localize_rows(rows: list[dict]) -> list[dict]:
    localized: list[dict] = []
    for row in rows:
        item = dict(row)
        if "operation_type" in item:
            item["operation_type"] = OPERATION_TYPE_LABELS.get(
                item["operation_type"],
                item["operation_type"],
            )
        if "match_type" in item:
            item["operation_type"] = OPERATION_TYPE_LABELS.get(
                item["match_type"],
                item["match_type"],
            )
        localized.append(item)
    return localized


def show_table(rows: list[dict], *, table_kind: str, empty_message: str) -> None:
    if not rows:
        st.info(empty_message)
        return

    columns = TABLE_COLUMNS[table_kind]
    table = (
        pd.DataFrame(localize_rows(rows))
        .reindex(columns=columns)
        .rename(columns=DISPLAY_COLUMN_LABELS)
    )

    if "Statut" in table:
        table["Statut"] = table["Statut"].replace(
            {"SUCCESS": "Réussie", "FAILED": "Échouée"}
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

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Explorateur": st.column_config.LinkColumn(
                "Explorateur",
                display_text="Ouvrir",
            ),
            "Solscan": st.column_config.LinkColumn(
                "Solscan",
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


st.title("🔎 Blockchain Lookup")
st.markdown(
    '<div class="lookup-subtitle">'
    "Retrouver une opération blockchain à partir d’une date, "
    "d’une heure approximative et, si disponible, d’un montant."
    "</div>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Réseau")
    network = st.radio(
        "Blockchain",
        ["Solana", "Ethereum", "Bitcoin"],
        index=0,
    )

    if network == "Solana":
        st.success("SOL · disponible")
    else:
        st.info(f"{network} · module à ajouter")

    st.divider()
    st.caption(
        "Toutes les dates et heures de recherche sont interprétées en UTC."
    )

now_utc = datetime.now(timezone.utc)

if "lookup_date" not in st.session_state:
    st.session_state["lookup_date"] = now_utc.date()

if "lookup_time" not in st.session_state:
    st.session_state["lookup_time"] = time(
        now_utc.hour,
        now_utc.minute,
        now_utc.second,
    )

with st.form("lookup_form", border=True):
    st.subheader("Critères de recherche")
    st.caption(
        "Saisissez uniquement les éléments communiqués par le prestataire. "
        "Le montant reste facultatif."
    )

    col_date, col_time, col_tolerance = st.columns([1.15, 1.15, 1])

    with col_date:
        search_date = st.date_input(
            "Date UTC",
            format="DD/MM/YYYY",
            key="lookup_date",
        )

    with col_time:
        search_time = st.time_input(
            "Heure approximative UTC",
            step=1,
            key="lookup_time",
        )

    with col_tolerance:
        tolerance = st.number_input(
            "Tolérance ± (secondes)",
            min_value=0,
            max_value=3600,
            value=30,
            step=5,
        )

    amount_col, asset_col = st.columns([3, 1])

    with amount_col:
        amount = st.text_input(
            "Montant communiqué — facultatif",
            placeholder="Ex. 2,5",
            help=(
                "Montant communiqué par le prestataire. "
                "Il sera recherché dans les transferts, swaps probables "
                "et variations de solde de l'actif choisi."
            ),
        )

    with asset_col:
        asset = st.selectbox(
            "Actif",
            ["SOL", "USDC", "USDT"],
            index=0,
            help="Actif auquel correspond le montant communiqué.",
        )

    submitted = st.form_submit_button(
        "Rechercher",
        type="primary",
        use_container_width=True,
    )

if submitted:
    if network != "Solana":
        st.warning(
            f"Le moteur {network} n'est pas encore branché. "
            "Pour l'instant, sélectionnez Solana."
        )
    else:
        progress_bar = st.progress(0, text="Initialisation…")

        with st.status(
            "Recherche Solana en cours…",
            expanded=True,
        ) as status_box:

            def on_status(message: str) -> None:
                status_box.write(message)

            def on_progress(current: int, total: int, slot: int) -> None:
                if total <= 0:
                    progress_bar.progress(0, text="Aucun bloc candidat")
                    return

                ratio = min(max(current / total, 0.0), 1.0)
                progress_bar.progress(
                    ratio,
                    text=(
                        f"Analyse des blocs : {current}/{total} "
                        f"· slot {slot}"
                    ),
                )

            try:
                result = search_solana_window(
                    search_date=search_date,
                    search_time=search_time,
                    tolerance_seconds=int(tolerance),
                    amount_sol=amount,
                    asset_symbol=asset,
                    progress_callback=on_progress,
                    status_callback=on_status,
                )
            except ValueError as exc:
                status_box.update(
                    label="Paramètres invalides",
                    state="error",
                    expanded=True,
                )
                st.error(str(exc))
            except SolanaSearchError as exc:
                status_box.update(
                    label="Erreur pendant la recherche",
                    state="error",
                    expanded=True,
                )
                st.error(str(exc))
            except Exception as exc:
                status_box.update(
                    label="Erreur inattendue",
                    state="error",
                    expanded=True,
                )
                st.exception(exc)
            else:
                progress_bar.progress(1.0, text="Recherche terminée")
                status_box.update(
                    label="Recherche terminée",
                    state="complete",
                    expanded=False,
                )
                st.session_state["solana_result"] = result

result = st.session_state.get("solana_result")

if result:
    st.divider()
    st.subheader("Résultats de la recherche")

    window_label = (
        f"{result['start_dt'].strftime('%d/%m/%Y %H:%M:%S')} → "
        f"{result['end_dt'].strftime('%d/%m/%Y %H:%M:%S')} UTC"
    )
    st.markdown(
        f'<span class="utc-badge">{window_label}</span>',
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Blocs analysés", result["analyzed_blocks"])
    m2.metric("Transactions", len(result["transactions"]))
    m3.metric("Opérations détectées", len(result["operations"]))
    m4.metric("Résultats exacts", len(result["matches"]))

    if result["target_amount"] is not None:
        target_label = (
            f"{result['target_amount']} {result['target_asset']}"
        )

        if result["matches"]:
            st.success(
                f"{len(result['matches'])} correspondance(s) exacte(s) "
                f"pour {target_label}."
            )
        else:
            st.warning(
                f"Aucune correspondance exacte pour {target_label}. "
                "Consultez Transactions et Opérations pour examiner "
                "toute la période."
            )

    st.markdown("#### Filtrer les données")
    search_col, status_col = st.columns([4.5, 1.2])

    with search_col:
        global_query = st.text_input(
            "Recherche",
            placeholder="Hash, adresse, montant, actif, bloc, heure…",
            key="global_result_search",
            help=(
                "Cette recherche s'applique aux onglets Résultats, "
                "Transactions, Opérations et Variations de solde."
            ),
        )

    with status_col:
        st.markdown(
            "<div style='height: 1.72rem;'></div>",
            unsafe_allow_html=True,
        )
        only_success = st.checkbox(
            "Réussies uniquement",
            value=False,
            key="tx_success_only",
            help="Ce filtre s'applique à l'onglet Transactions.",
        )

    filtered_matches = filter_rows(result["matches"], global_query)
    filtered_transactions = filter_rows(
        result["transactions"],
        global_query,
        only_success=only_success,
    )
    filtered_operations = filter_rows(result["operations"], global_query)
    filtered_movements = filter_rows(result["movements"], global_query)

    (
        tab_matches,
        tab_tx,
        tab_operations,
        tab_movements,
        tab_details,
    ) = st.tabs(
        [
            "🎯 Résultats",
            "🧾 Transactions",
            "💸 Opérations",
            "📊 Variations de solde",
            "ℹ️ Résumé de la recherche",
        ]
    )

    with tab_matches:
        st.caption(
            "Correspondances exactes avec le montant et l'actif renseignés."
        )

        if result["target_amount"] is None:
            st.info(
                "Aucun montant n'a été renseigné. "
                "Consultez Transactions ou Opérations pour parcourir "
                "toute la période."
            )
        else:
            show_table(
                filtered_matches,
                table_kind="matches",
                empty_message="Aucun résultat exact trouvé.",
            )

            if filtered_matches:
                st.download_button(
                    "Télécharger les résultats affichés CSV",
                    data=to_csv_bytes(filtered_matches),
                    file_name="solana_matches.csv",
                    mime="text/csv",
                )

    with tab_tx:
        st.caption(
            "Toutes les transactions de la fenêtre, même lorsqu'aucune "
            "opération simple ou aucun montant précis n'a pu être identifié. "
            "« — » signifie qu'aucune opération utilisateur claire n'a été "
            "détectée automatiquement."
        )
        st.caption(
            f"{len(filtered_transactions)} transaction(s) affichée(s) "
            f"sur {len(result['transactions'])}."
        )

        show_table(
            filtered_transactions,
            table_kind="transactions",
            empty_message="Aucune transaction ne correspond à la recherche.",
        )

        if filtered_transactions:
            st.download_button(
                "Télécharger les transactions affichées CSV",
                data=to_csv_bytes(filtered_transactions),
                file_name="solana_transactions.csv",
                mime="text/csv",
            )

    with tab_operations:
        st.caption(
            "Opérations lisibles détectées automatiquement : transferts SOL, "
            "transferts de tokens et swaps probables. "
            "Un « swap probable » est déduit des variations nettes d'actifs "
            "du même compte ; il ne dépend pas d'un DEX particulier."
        )
        st.caption(
            f"{len(filtered_operations)} opération(s) affichée(s) "
            f"sur {len(result['operations'])}."
        )

        show_table(
            filtered_operations,
            table_kind="operations",
            empty_message="Aucune opération ne correspond à la recherche.",
        )

        if filtered_operations:
            st.download_button(
                "Télécharger les opérations affichées CSV",
                data=to_csv_bytes(filtered_operations),
                file_name="solana_operations.csv",
                mime="text/csv",
            )

    with tab_movements:
        st.caption(
            "Vue de contrôle : variations de solde SOL et tokens observées "
            "avant/après les transactions. Une variation n'est pas "
            "nécessairement une opération distincte."
        )
        st.caption(
            f"{len(filtered_movements)} variation(s) affichée(s) "
            f"sur {len(result['movements'])}."
        )

        show_table(
            filtered_movements,
            table_kind="movements",
            empty_message=(
                "Aucune variation de solde ne correspond à la recherche."
            ),
        )

        if filtered_movements:
            st.download_button(
                "Télécharger les variations affichées CSV",
                data=to_csv_bytes(filtered_movements),
                file_name="solana_movements.csv",
                mime="text/csv",
            )

    with tab_details:
        left, right = st.columns(2)

        with left:
            st.write("**Borne de début**")
            st.code(f"slot {result['start_slot']}")
            st.write("**Borne de fin**")
            st.code(f"slot {result['end_slot']}")

        with right:
            st.write("**Plage interrogée**")
            st.code(
                f"{result['query_start_slot']} → "
                f"{result['query_end_slot']}"
            )
            st.write("**Blocs candidats / ignorés**")
            st.code(
                f"{result['candidate_blocks']} / "
                f"{result['skipped_blocks']}"
            )

        st.caption(
            "L'analyse des tokens et des swaps utilise les métadonnées "
            "déjà présentes dans les blocs téléchargés. "
            "Elle n'ajoute pas de requête RPC par opération."
        )
