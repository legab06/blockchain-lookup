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
    return pd.DataFrame(rows).to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")


DISPLAY_COLUMN_LABELS = {
    "block": "Bloc",
    "block_time_utc": "Date / heure UTC",
    "transaction_index": "N° dans le bloc",
    "signature": "Transaction",
    "source": "Expéditeur",
    "destination": "Destinataire",
    "sol": "Montant (SOL)",
    "amount_sol": "Montant (SOL)",
    "fee_sol": "Frais (SOL)",
    "delta_sol": "Variation (SOL)",
    "absolute_delta_sol": "Variation absolue (SOL)",
    "account_count": "Nb. de comptes",
    "accounts": "Comptes concernés",
    "transfer_count": "Nb. de transferts",
    "transfer_amounts_sol": "Montant(s) transféré(s)",
    "transfer_parties": "Expéditeur → destinataire",
    "account": "Compte concerné",
    "match_target": "Montant recherché ?",
    "match_type": "Type de résultat",
    "instruction_type": "Type d’opération",
    "detail": "Détail",
    "error": "Erreur",
    "status": "Statut",
    "explorer": "Explorateur",
    "solscan": "Solscan",
}

TABLE_COLUMNS = {
    "matches": [
        "block_time_utc",
        "amount_sol",
        "source",
        "destination",
        "account",
        "signature",
        "match_type",
        "detail",
        "solscan",
        "explorer",
        "block",
    ],
    "transactions": [
        "block_time_utc",
        "transfer_amounts_sol",
        "transfer_count",
        "status",
        "signature",
        "transfer_parties",
        "fee_sol",
        "account_count",
        "accounts",
        "solscan",
        "explorer",
        "block",
        "transaction_index",
        "error",
    ],
    "transfers": [
        "block_time_utc",
        "sol",
        "source",
        "destination",
        "signature",
        "match_target",
        "instruction_type",
        "solscan",
        "explorer",
        "block",
    ],
    "movements": [
        "block_time_utc",
        "absolute_delta_sol",
        "delta_sol",
        "account",
        "signature",
        "match_target",
        "solscan",
        "explorer",
        "block",
    ],
}


def show_table(rows: list[dict], *, table_kind: str, empty_message: str) -> None:
    if not rows:
        st.info(empty_message)
        return

    columns = TABLE_COLUMNS[table_kind]
    table = pd.DataFrame(rows).reindex(columns=columns).rename(columns=DISPLAY_COLUMN_LABELS)
    if "Statut" in table:
        table["Statut"] = table["Statut"].replace(
            {"SUCCESS": "Réussie", "FAILED": "Échouée"}
        )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Explorateur": st.column_config.LinkColumn("Explorateur", display_text="Ouvrir"),
            "Solscan": st.column_config.LinkColumn("Solscan", display_text="Ouvrir"),
        },
    )


def filter_transactions(rows: list[dict], query: str, only_success: bool) -> list[dict]:
    query = query.strip().casefold()
    filtered: list[dict] = []

    for row in rows:
        if only_success and row.get("status") != "SUCCESS":
            continue

        if query:
            searchable = " ".join(
                str(row.get(key, ""))
                for key in (
                    "block",
                    "block_time_utc",
                    "signature",
                    "status",
                    "fee_sol",
                    "accounts",
                    "transfer_amounts_sol",
                    "transfer_parties",
                    "error",
                )
            ).casefold()

            if query not in searchable:
                continue

        filtered.append(row)

    return filtered


st.title("🔎 Blockchain Lookup")
st.markdown(
    '<div class="lookup-subtitle">Retrouver une opération blockchain à partir d’une date, d’une heure approximative et, si disponible, d’un montant.</div>',
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
    st.caption("Toutes les dates et heures de recherche sont interprétées en UTC.")

now_utc = datetime.now(timezone.utc)

with st.form("lookup_form", border=True):
    st.subheader("Critères de recherche")
    st.caption("Saisissez les éléments communiqués par le prestataire. Le montant est facultatif.")

    col_date, col_time, col_tolerance = st.columns([1.15, 1.15, 1])

    with col_date:
        search_date = st.date_input(
            "Date UTC",
            value=now_utc.date(),
            format="DD/MM/YYYY",
        )

    with col_time:
        search_time = st.time_input(
            "Heure approximative UTC",
            value=time(now_utc.hour, now_utc.minute, now_utc.second),
            step=1,
        )

    with col_tolerance:
        tolerance = st.number_input(
            "Tolérance ± (secondes)",
            min_value=0,
            max_value=3600,
            value=30,
            step=5,
        )

    amount = st.text_input(
        "Montant (SOL) — facultatif",
        placeholder="Ex. 1,25",
        help="Laissez vide si le montant est inconnu ou incertain. Virgule ou point acceptés.",
    )

    submitted = st.form_submit_button(
        "Rechercher",
        type="primary",
        use_container_width=True,
    )

if submitted:
    if network != "Solana":
        st.warning(f"Le moteur {network} n'est pas encore branché. Pour l'instant, sélectionne Solana.")
    else:
        progress_bar = st.progress(0, text="Initialisation…")

        with st.status("Recherche Solana en cours…", expanded=True) as status_box:
            def on_status(message: str) -> None:
                status_box.write(message)

            def on_progress(current: int, total: int, slot: int) -> None:
                if total <= 0:
                    progress_bar.progress(0, text="Aucun bloc candidat")
                    return
                ratio = min(max(current / total, 0.0), 1.0)
                progress_bar.progress(
                    ratio,
                    text=f"Analyse des blocs : {current}/{total} · slot {slot}",
                )

            try:
                result = search_solana_window(
                    search_date=search_date,
                    search_time=search_time,
                    tolerance_seconds=int(tolerance),
                    amount_sol=amount,
                    progress_callback=on_progress,
                    status_callback=on_status,
                )
            except ValueError as exc:
                status_box.update(label="Paramètres invalides", state="error", expanded=True)
                st.error(str(exc))
            except SolanaSearchError as exc:
                status_box.update(label="Erreur pendant la recherche", state="error", expanded=True)
                st.error(str(exc))
            except Exception as exc:
                status_box.update(label="Erreur inattendue", state="error", expanded=True)
                st.exception(exc)
            else:
                progress_bar.progress(1.0, text="Recherche terminée")
                status_box.update(label="Recherche terminée", state="complete", expanded=False)
                st.session_state["solana_result"] = result

result = st.session_state.get("solana_result")

if result:
    st.divider()
    st.subheader("Résultats de la recherche")

    window_label = (
        f"{result['start_dt'].strftime('%d/%m/%Y %H:%M:%S')} → "
        f"{result['end_dt'].strftime('%d/%m/%Y %H:%M:%S')} UTC"
    )
    st.markdown(f'<span class="utc-badge">{window_label}</span>', unsafe_allow_html=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Blocs analysés", result["analyzed_blocks"])
    m2.metric("Transactions", len(result["transactions"]))
    m3.metric("Transferts", len(result["transfers"]))
    m4.metric("Résultats exacts", len(result["matches"]))

    if result["target_sol"] is not None:
        if result["matches"]:
            st.success(
                f"{len(result['matches'])} résultat(s) exact(s) pour {result['target_sol']} SOL."
            )
        else:
            st.warning(
                f"Aucune correspondance exacte pour {result['target_sol']} SOL. "
                "Consultez l’onglet Transactions pour examiner toutes les opérations de la période."
            )

    tab_matches, tab_tx, tab_transfers, tab_movements, tab_details = st.tabs(
        [
            "🎯 Résultats",
            "🧾 Transactions",
            "↔️ Transferts",
            "📊 Variations de solde",
            "ℹ️ Résumé de la recherche",
        ]
    )

    with tab_matches:
        st.caption("Correspondances exactes avec le montant renseigné.")
        if result["target_sol"] is None:
            st.info("Aucun montant n'a été renseigné. Consultez Transactions pour parcourir toute la période.")
        else:
            match_rows = []
            for row in result["matches"]:
                item = dict(row)
                item["match_type"] = {
                    "instruction": "Transfert",
                    "balance_delta": "Variation de solde",
                }.get(item.get("match_type"), item.get("match_type", ""))
                match_rows.append(item)

            show_table(
                match_rows,
                table_kind="matches",
                empty_message="Aucun résultat exact trouvé.",
            )
            if result["matches"]:
                st.download_button(
                    "Télécharger les résultats CSV",
                    data=to_csv_bytes(result["matches"]),
                    file_name="solana_matches.csv",
                    mime="text/csv",
                )

    with tab_tx:
        st.caption(
            "Toutes les transactions trouvées dans la fenêtre, même si le montant ne correspond pas exactement."
        )

        search_col, status_col = st.columns([3, 1])
        with search_col:
            tx_query = st.text_input(
                "Rechercher dans les transactions",
                placeholder="Hash, adresse, montant, bloc, heure…",
                key="tx_search",
            )
        with status_col:
            only_success = st.checkbox(
                "Réussies uniquement",
                value=False,
                key="tx_success_only",
            )

        filtered_tx = filter_transactions(result["transactions"], tx_query, only_success)
        st.caption(
            f"{len(filtered_tx)} transaction(s) affichée(s) sur {len(result['transactions'])}."
        )

        show_table(
            filtered_tx,
            table_kind="transactions",
            empty_message="Aucune transaction ne correspond à ce filtre.",
        )
        if result["transactions"]:
            st.download_button(
                "Télécharger toutes les transactions CSV",
                data=to_csv_bytes(result["transactions"]),
                file_name="solana_transactions.csv",
                mime="text/csv",
            )

    with tab_transfers:
        st.caption("Détail des transferts SOL explicitement détectés dans les transactions.")
        show_table(
            result["transfers"],
            table_kind="transfers",
            empty_message="Aucun transfert détecté.",
        )
        if result["transfers"]:
            st.download_button(
                "Télécharger les transferts CSV",
                data=to_csv_bytes(result["transfers"]),
                file_name="solana_transfers.csv",
                mime="text/csv",
            )

    with tab_movements:
        st.caption(
            "Vue complémentaire : variations de solde des comptes impliqués. "
            "Un mouvement n'est pas nécessairement un transfert distinct."
        )
        show_table(
            result["movements"],
            table_kind="movements",
            empty_message="Aucune variation de solde détectée.",
        )
        if result["movements"]:
            st.download_button(
                "Télécharger les variations de solde CSV",
                data=to_csv_bytes(result["movements"]),
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
            st.code(f"{result['query_start_slot']} → {result['query_end_slot']}")
            st.write("**Blocs candidats / ignorés**")
            st.code(f"{result['candidate_blocks']} / {result['skipped_blocks']}")
