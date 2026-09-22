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
        .table-footer-text {
            color: #8a8f98;
            font-size: 0.72rem;
            line-height: 2.35rem;
            white-space: nowrap;
        }
        .table-footer-right {
            text-align: right;
        }
        .compact-filter-title {
            font-size: 0.82rem;
            font-weight: 600;
            line-height: 1.1rem;
            margin: 0.45rem 0 0.28rem 0;
            color: rgba(49, 51, 63, 0.86);
        }
        @media (prefers-color-scheme: dark) {
            .compact-filter-title {
                color: rgba(250, 250, 250, 0.86);
            }
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
        lazy=True,
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


PAGE_SIZE_OPTIONS = (100, 250, 500)


def paginate_rows(
    rows: list[dict],
    *,
    key_prefix: str,
) -> tuple[list[dict], int, int, int]:
    if not rows:
        return [], 1, 1, 100

    page_size_key = f"{key_prefix}_page_size"
    page_key = f"{key_prefix}_page"

    if st.session_state.get(page_size_key) not in PAGE_SIZE_OPTIONS:
        st.session_state[page_size_key] = PAGE_SIZE_OPTIONS[0]

    page_size = int(st.session_state[page_size_key])
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)

    try:
        page = int(st.session_state.get(page_key, 1))
    except (TypeError, ValueError):
        page = 1

    page = max(1, min(page, total_pages))
    st.session_state[page_key] = page

    start = (page - 1) * page_size
    end = min(start + page_size, len(rows))
    return rows[start:end], page, total_pages, page_size


def change_page(page_key: str, delta: int, total_pages: int) -> None:
    current = int(st.session_state.get(page_key, 1))
    st.session_state[page_key] = max(
        1,
        min(current + delta, total_pages),
    )


def render_pagination_footer(
    *,
    total_rows: int,
    page: int,
    total_pages: int,
    page_size: int,
    key_prefix: str,
) -> None:
    if total_rows <= 0:
        return

    start_row = (page - 1) * page_size + 1
    end_row = min(page * page_size, total_rows)
    page_key = f"{key_prefix}_page"
    page_size_key = f"{key_prefix}_page_size"

    spacer_col, footer_col = st.columns(
        [7.2, 2.8],
        vertical_alignment="center",
    )

    with spacer_col:
        st.empty()

    with footer_col:
        info_col, size_col, prev_col, page_col, next_col = st.columns(
            [2.05, 1.25, 0.5, 1.25, 0.5],
            vertical_alignment="center",
        )

        with info_col:
            st.markdown(
                (
                    '<div class="table-footer-text table-footer-right">'
                    f"{start_row:,}–{end_row:,} sur {total_rows:,}"
                    "</div>"
                ).replace(",", " "),
                unsafe_allow_html=True,
            )

        with size_col:
            st.selectbox(
                "Lignes par page",
                PAGE_SIZE_OPTIONS,
                key=page_size_key,
                label_visibility="collapsed",
                format_func=lambda value: f"{value} / page",
            )

        with prev_col:
            st.button(
                "‹",
                key=f"{key_prefix}_page_prev",
                disabled=page <= 1,
                use_container_width=True,
                on_click=change_page,
                args=(page_key, -1, total_pages),
            )

        with page_col:
            st.selectbox(
                "Page",
                range(1, total_pages + 1),
                key=page_key,
                label_visibility="collapsed",
                format_func=lambda value: f"{value} / {total_pages}",
            )

        with next_col:
            st.button(
                "›",
                key=f"{key_prefix}_page_next",
                disabled=page >= total_pages,
                use_container_width=True,
                on_click=change_page,
                args=(page_key, 1, total_pages),
            )


def render_downloads(
    filtered_rows: list[dict],
    page_rows: list[dict],
    *,
    filename: str,
    key_prefix: str,
) -> None:
    if not page_rows:
        return

    st.download_button(
        "Télécharger la page affichée CSV",
        data=to_csv_bytes(page_rows),
        file_name=filename,
        mime="text/csv",
        key=f"{key_prefix}_page_csv",
    )

    if len(filtered_rows) > len(page_rows):
        prepare_full = st.checkbox(
            f"Préparer le CSV complet ({len(filtered_rows)} lignes)",
            value=False,
            key=f"{key_prefix}_prepare_full_csv",
            help=(
                "La génération complète consomme davantage de mémoire. "
                "Elle n'est effectuée que si cette case est cochée."
            ),
        )
        if prepare_full:
            st.download_button(
                "Télécharger toutes les lignes filtrées CSV",
                data=to_csv_bytes(filtered_rows),
                file_name=filename.replace(".csv", "_complet.csv"),
                mime="text/csv",
                key=f"{key_prefix}_full_csv",
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
        target_label = f"{result['target_amount']} {result['target_asset']}"
        st.caption(f"Critère recherché : **{target_label}**")

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

    st.markdown("#### Vue des données")
    view = st.segmented_control(
        "Vue",
        [
            "🎯 Résultats",
            "🧾 Transactions",
            "💸 Opérations",
            "📊 Variations de solde",
            "ℹ️ Résumé de la recherche",
        ],
        default="🎯 Résultats",
        selection_mode="single",
        label_visibility="collapsed",
        key="result_view",
        width="stretch",
    )

    if view != "ℹ️ Résumé de la recherche":
        st.markdown(
            '<div class="compact-filter-title">Filtrer les données</div>',
            unsafe_allow_html=True,
        )

        filter_col, status_col = st.columns(
            [5.2, 1.15],
            vertical_alignment="center",
        )

        with filter_col:
            global_query = st.text_input(
                "Recherche",
                placeholder="Rechercher : hash, adresse, montant, actif, bloc, heure…",
                key="global_result_search",
                help=(
                    "Le même filtre est conservé lorsque vous changez de vue."
                ),
                label_visibility="collapsed",
            )

        with status_col:
            if view == "🧾 Transactions":
                only_success = st.checkbox(
                    "Réussies uniquement",
                    value=False,
                    key="tx_success_only",
                )
            else:
                only_success = False

        if view == "🎯 Résultats":
            source_rows = result["matches"]
            table_kind = "matches"
            key_prefix = "matches"
            filename = "solana_matches.csv"
            empty_message = "Aucun résultat exact trouvé."
            intro = (
                "Correspondances exactes avec le montant et l'actif renseignés."
            )

        elif view == "🧾 Transactions":
            source_rows = result["transactions"]
            table_kind = "transactions"
            key_prefix = "transactions"
            filename = "solana_transactions.csv"
            empty_message = "Aucune transaction ne correspond à la recherche."
            intro = (
                "Toutes les transactions de la fenêtre. « — » signifie "
                "qu'aucune opération utilisateur claire n'a été détectée."
            )

        elif view == "💸 Opérations":
            source_rows = result["operations"]
            table_kind = "operations"
            key_prefix = "operations"
            filename = "solana_operations.csv"
            empty_message = "Aucune opération ne correspond à la recherche."
            intro = (
                "Transferts SOL, transferts de tokens et swaps probables "
                "détectés automatiquement."
            )

        else:
            source_rows = result["movements"]
            table_kind = "movements"
            key_prefix = "movements"
            filename = "solana_movements.csv"
            empty_message = (
                "Aucune variation de solde ne correspond à la recherche."
            )
            intro = (
                "Variations de solde SOL et tokens avant/après les transactions."
            )

        st.caption(intro)

        filtered_rows = filter_rows(
            source_rows,
            global_query,
            only_success=only_success,
        )

        page_rows, page, total_pages, page_size = paginate_rows(
            filtered_rows,
            key_prefix=key_prefix,
        )

        show_table(
            page_rows,
            table_kind=table_kind,
            empty_message=empty_message,
        )

        render_pagination_footer(
            total_rows=len(filtered_rows),
            page=page,
            total_pages=total_pages,
            page_size=page_size,
            key_prefix=key_prefix,
        )

        render_downloads(
            filtered_rows,
            page_rows,
            filename=filename,
            key_prefix=key_prefix,
        )

    else:
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
            "Une seule table est rendue à la fois afin de limiter la mémoire "
            "serveur et la charge du navigateur sur les recherches volumineuses."
        )
