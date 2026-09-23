from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

from bitcoin_engine import BitcoinSearchError, search_bitcoin_window
from ethereum_engine import EthereumSearchError, search_ethereum_window
from operation_labels import operation_type_label
from search_manifest import manifest_json_bytes
from search_result_messages import no_matches_message, partial_search_warning
from solana_engine import SolanaSearchError, search_solana_window
from streamlit_errors import log_unexpected_search_error


st.set_page_config(
    page_title="Blockchain Lookup",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="collapsed",
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

        /* Navigation des vues : compacte, sans fond ni pastille.
           Le soulignement de l'onglet actif est injecté dynamiquement. */
        .st-key-result_tabs_nav {
            margin-top: -0.15rem;
            margin-bottom: 0.1rem;
        }
        .st-key-result_tabs_nav [data-testid="stButton"] {
            width: auto !important;
            flex: 0 0 auto !important;
        }
        .st-key-result_tabs_nav [data-testid="stButton"] > button {
            width: auto !important;
            border: 0 !important;
            border-radius: 0 !important;
            border-bottom: 2px solid transparent !important;
            background: transparent !important;
            box-shadow: none !important;
            color: inherit !important;
            padding: 0.32rem 0.58rem 0.42rem 0.58rem !important;
            min-height: auto !important;
        }
        .st-key-result_tabs_nav [data-testid="stButton"] > button:hover,
        .st-key-result_tabs_nav [data-testid="stButton"] > button:focus,
        .st-key-result_tabs_nav [data-testid="stButton"] > button:active {
            background: transparent !important;
            color: inherit !important;
            box-shadow: none !important;
        }
        @media (prefers-color-scheme: dark) {
            .compact-filter-title {
                color: rgba(250, 250, 250, 0.86);
            }
        }

        /* Sélecteur réseau directement dans la page principale. */
        .network-picker-label {
            margin: -0.35rem 0 0.32rem 0;
            color: #8a8f98;
            font-size: 0.73rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }
        .st-key-network_selector {
            width: 100%;
            max-width: 520px;
            margin-bottom: 1.2rem;
        }

        @media (max-width: 768px) {
            .block-container {
                padding-top: 1.25rem;
            }
            .network-picker-label {
                margin-top: -0.2rem;
            }
            .st-key-network_selector {
                max-width: none;
                margin-bottom: 0.9rem;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)


NETWORK_THEMES = {
    "Solana": {
        "symbol": "SOL",
        "accent": "#14F195",
        "accent_hover": "#10D987",
        "secondary": "#9945FF",
        "soft": "rgba(20, 241, 149, 0.12)",
        "ring": "rgba(20, 241, 149, 0.24)",
        "gradient": "linear-gradient(90deg, #9945FF 0%, #14F195 100%)",
    },
    "Ethereum": {
        "symbol": "ETH",
        "accent": "#627EEA",
        "accent_hover": "#526DD0",
        "secondary": "#8A92B2",
        "soft": "rgba(98, 126, 234, 0.12)",
        "ring": "rgba(98, 126, 234, 0.24)",
        "gradient": "linear-gradient(90deg, #627EEA 0%, #8A92B2 100%)",
    },
    "Bitcoin": {
        "symbol": "BTC",
        "accent": "#F7931A",
        "accent_hover": "#DD7F0B",
        "secondary": "#FFB347",
        "soft": "rgba(247, 147, 26, 0.12)",
        "ring": "rgba(247, 147, 26, 0.24)",
        "gradient": "linear-gradient(90deg, #F7931A 0%, #FFB347 100%)",
    },
}


def apply_network_theme(network: str) -> None:
    theme = NETWORK_THEMES[network]
    st.markdown(
        f"""
        <style>
            :root {{
                --chain-accent: {theme["accent"]};
                --chain-accent-hover: {theme["accent_hover"]};
                --chain-secondary: {theme["secondary"]};
                --chain-soft: {theme["soft"]};
                --chain-ring: {theme["ring"]};
                --chain-gradient: {theme["gradient"]};
            }}

            /* Sélecteur réseau compact : l'état actif reprend la couleur
               de la blockchain, sans radio ni badge redondant. */
            .st-key-network_selector [data-testid="stSegmentedControl"] {{
                width: 100%;
            }}
            .st-key-network_selector [data-testid="stSegmentedControl"] > div {{
                width: 100%;
                gap: 0.38rem;
                background: transparent !important;
            }}
            .st-key-network_selector button {{
                flex: 1 1 0;
                min-width: 0 !important;
                min-height: 2.7rem !important;
                padding: 0.48rem 0.9rem !important;
                border: 1px solid rgba(128, 128, 128, 0.22) !important;
                border-radius: 999px !important;
                background: transparent !important;
                box-shadow: none !important;
                font-size: 0.92rem !important;
                font-weight: 650 !important;
                white-space: nowrap !important;
            }}
            .st-key-network_selector button span,
            .st-key-network_selector button p {{
                max-width: none !important;
                overflow: visible !important;
                text-overflow: clip !important;
                white-space: nowrap !important;
            }}
            .st-key-network_selector button:hover {{
                border-color: var(--chain-accent) !important;
                color: var(--chain-accent) !important;
            }}
            .st-key-network_selector button[aria-pressed="true"] {{
                border-color: var(--chain-ring) !important;
                background: var(--chain-soft) !important;
                color: var(--chain-accent) !important;
                box-shadow: inset 0 -2px 0 var(--chain-accent) !important;
            }}
            /* Ligne d'identité très légère sous le titre principal. */
            .lookup-subtitle {{
                border-left: 3px solid var(--chain-accent);
                padding-left: 0.72rem;
            }}

            /* Champs : focus cohérent avec la blockchain sélectionnée. */
            [data-baseweb="input"]:focus-within,
            [data-baseweb="select"] > div:focus-within,
            [data-baseweb="textarea"]:focus-within {{
                border-color: var(--chain-accent) !important;
                box-shadow: 0 0 0 1px var(--chain-ring) !important;
            }}

            /* CTA principal de recherche. */
            div[data-testid="stFormSubmitButton"] > button {{
                background: var(--chain-gradient) !important;
                border-color: var(--chain-accent) !important;
                color: #ffffff !important;
                font-weight: 650 !important;
                box-shadow: none !important;
            }}
            div[data-testid="stFormSubmitButton"] > button:hover {{
                border-color: var(--chain-accent-hover) !important;
                filter: brightness(0.96);
            }}
            div[data-testid="stFormSubmitButton"] > button:focus {{
                box-shadow: 0 0 0 0.2rem var(--chain-ring) !important;
            }}

            /* Les boutons utilitaires restent neutres, avec un rappel au survol. */
            div[data-testid="stDownloadButton"] > button:hover {{
                border-color: var(--chain-accent) !important;
                color: var(--chain-accent) !important;
            }}

            /* Navigation des résultats : le réseau remplace l'ancien rouge fixe. */
            .st-key-result_tabs_nav [data-testid="stButton"] > button:hover {{
                color: var(--chain-accent) !important;
            }}

            /* Repères discrets autour des informations importantes. */
            .utc-badge {{
                border-color: var(--chain-ring);
            }}
            div[data-testid="stMetric"] {{
                border-top: 2px solid var(--chain-accent);
            }}

        </style>
        """,
        unsafe_allow_html=True,
    )


MAX_STATUS_MESSAGES = 60
STATUS_LOG_HEIGHT = 220
PROGRESS_BUCKETS = 50


@st.cache_data(show_spinner=False)
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

    columns = TABLE_COLUMNS[table_kind]
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

    table = pd.DataFrame(localize_rows(rows, network)).reindex(columns=columns)
    table = table.dropna(axis=1, how="all").rename(columns=labels)

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
        on_click="ignore",
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
                on_click="ignore",
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


def clear_search_results() -> None:
    for key in (
        "lookup_result",
        "global_result_search",
        "tx_success_only",
        "result_view",
    ):
        st.session_state.pop(key, None)

    for prefix in ("matches", "transactions", "operations", "movements"):
        for suffix in ("page", "page_size", "prepare_full_csv"):
            st.session_state.pop(f"{prefix}_{suffix}", None)


st.title("🔎 Blockchain Lookup")
st.markdown(
    '<div class="lookup-subtitle">'
    "Retrouver une opération blockchain à partir d’une date, "
    "d’une heure approximative et, si disponible, d’un montant."
    "</div>",
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="network-picker-label">Réseau</div>',
    unsafe_allow_html=True,
)

network_options = ["Solana", "Ethereum", "Bitcoin"]
network = st.segmented_control(
    "Réseau blockchain",
    network_options,
    default="Solana",
    key="network_selector",
    label_visibility="collapsed",
    width="stretch",
)
if network is None:
    network = "Solana"

apply_network_theme(network)

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
            key="lookup_amount",
            help=(
                "Montant communiqué par le prestataire. "
                "Il sera recherché dans les transferts, swaps probables "
                "et variations de solde de l'actif choisi. "
                "Une correspondance approchée est aussi admise selon le "
                "nombre de décimales saisi, avec un écart maximal plafonné."
            ),
        )

    with asset_col:
        asset_options = {
            "Solana": ["SOL", "USDC", "USDT"],
            "Ethereum": ["ETH", "USDC", "USDT"],
            "Bitcoin": ["BTC"],
        }[network]
        asset = st.selectbox(
            "Actif",
            asset_options,
            index=0,
            help="Actif auquel correspond le montant communiqué.",
        )

    submitted = st.form_submit_button(
        "Rechercher",
        type="primary",
        use_container_width=True,
    )

if submitted:
    # Un nouveau lancement invalide immédiatement l'ancien résultat. Cela évite
    # qu'une recherche précédente reste affichée si la nouvelle échoue ou si
    # un widget de formulaire n'a pas encore été synchronisé côté serveur.
    st.session_state.pop("lookup_result", None)

    submitted_amount_raw = str(st.session_state.get("lookup_amount", ""))
    submitted_amount = submitted_amount_raw.strip()
    submitted_asset = str(asset)
    submitted_network = str(network)

    if network in {"Solana", "Ethereum", "Bitcoin"}:
        progress_bar = st.progress(0, text="Initialisation…")

        with st.status(
            f"Recherche {network} en cours…",
            expanded=True,
        ) as status_box:
            status_messages: deque[str] = deque(maxlen=MAX_STATUS_MESSAGES)
            progress_ui_state = {"bucket": -1}

            with st.container(height=STATUS_LOG_HEIGHT, border=False):
                status_log = st.empty()

            def render_status_log() -> None:
                if not status_messages:
                    status_log.caption("Aucun événement pour le moment.")
                    return
                status_log.text("\n".join(status_messages))

            def on_status(message: str) -> None:
                normalized = " ".join(str(message).split())
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                status_messages.appendleft(f"[{timestamp}] {normalized}")
                render_status_log()

            def on_progress(current: int, total: int, block_number: int) -> None:
                if total <= 0:
                    progress_bar.progress(0, text="Aucun bloc candidat")
                    return

                bucket = (
                    PROGRESS_BUCKETS
                    if current >= total
                    else int(current * PROGRESS_BUCKETS / total)
                )
                if bucket == progress_ui_state["bucket"]:
                    return
                progress_ui_state["bucket"] = bucket

                ratio = min(max(current / total, 0.0), 1.0)
                unit = "slot" if network == "Solana" else "bloc"
                progress_bar.progress(
                    ratio,
                    text=(
                        f"Analyse des blocs : {current}/{total} "
                        f"· {unit} {block_number}"
                    ),
                )

            on_status("Initialisation de la recherche…")

            try:
                if network == "Solana":
                    result = search_solana_window(
                        search_date=search_date,
                        search_time=search_time,
                        tolerance_seconds=int(tolerance),
                        amount_sol=submitted_amount_raw,
                        asset_symbol=submitted_asset,
                        progress_callback=on_progress,
                        status_callback=on_status,
                    )
                elif network == "Ethereum":
                    result = search_ethereum_window(
                        search_date=search_date,
                        search_time=search_time,
                        tolerance_seconds=int(tolerance),
                        amount_eth=submitted_amount_raw,
                        asset_symbol=submitted_asset,
                        progress_callback=on_progress,
                        status_callback=on_status,
                    )
                else:
                    result = search_bitcoin_window(
                        search_date=search_date,
                        search_time=search_time,
                        tolerance_seconds=int(tolerance),
                        amount_btc=submitted_amount_raw,
                        asset_symbol=submitted_asset,
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
            except (
                SolanaSearchError,
                EthereumSearchError,
                BitcoinSearchError,
            ) as exc:
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
                st.error(
                    log_unexpected_search_error(
                        logging.getLogger(__name__),
                        exc,
                    )
                )
            else:
                if submitted_amount and result.get("target_amount") is None:
                    raise RuntimeError(
                        "Le montant saisi n'a pas été transmis au moteur de recherche."
                    )

                result["submitted_amount_raw"] = submitted_amount_raw
                result["submitted_asset"] = submitted_asset
                result["submitted_network"] = submitted_network

                progress_bar.progress(1.0, text="Recherche terminée")
                status_box.update(
                    label="Recherche terminée",
                    state="complete",
                    expanded=False,
                )
                st.session_state["lookup_result"] = result

result = st.session_state.get("lookup_result")

if result and result.get("network") == network:
    st.divider()
    title_col, clear_col = st.columns(
        [5, 1.35],
        vertical_alignment="center",
    )

    with title_col:
        st.subheader("Résultats de la recherche")

    with clear_col:
        st.button(
            "Effacer la recherche",
            key="clear_blockchain_search",
            on_click=clear_search_results,
            icon=":material/delete_sweep:",
            width="stretch",
        )

    window_label = (
        f"{result['start_dt'].strftime('%d/%m/%Y %H:%M:%S')} → "
        f"{result['end_dt'].strftime('%d/%m/%Y %H:%M:%S')} UTC"
    )
    st.markdown(
        f'<span class="utc-badge">{window_label}</span>',
        unsafe_allow_html=True,
    )

    completeness_warning = partial_search_warning(result)
    if completeness_warning:
        st.warning(completeness_warning)

    if result["network"] == "Bitcoin":
        st.caption(
            "Bitcoin n'horodate pas chaque transaction : la recherche UTC "
            "s'appuie sur l'horodatage du bloc de confirmation. Les montants "
            "sont recherchés dans les sorties (vout) en satoshis."
        )

        if result.get("time_fallback_used"):
            offset_seconds = abs(
                int(result.get("time_fallback_offset_seconds") or 0)
            )
            offset_minutes, offset_remainder = divmod(offset_seconds, 60)
            block_timestamp = result.get("time_fallback_block_timestamp")
            if block_timestamp is not None:
                fallback_dt = datetime.fromtimestamp(
                    int(block_timestamp),
                    tz=timezone.utc,
                )
                fallback_time_label = fallback_dt.strftime(
                    "%d/%m/%Y %H:%M:%S UTC"
                )
            else:
                fallback_time_label = "heure inconnue"

            st.warning(
                "Aucun bloc Bitcoin n'a été horodaté dans la fenêtre demandée. "
                f"Le bloc le plus proche (#{result.get('time_fallback_block')}, "
                f"{fallback_time_label}) a donc été analysé "
                f"· écart {offset_minutes} min {offset_remainder} s."
            )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Blocs analysés", result["analyzed_blocks"])
    m2.metric("Transactions", len(result["transactions"]))
    m3.metric("Opérations détectées", len(result["operations"]))
    m4.metric("Correspondances", len(result["matches"]))

    if result["target_amount"] is not None:
        target_label = f"{result['target_amount']} {result['target_asset']}"
        precision = result.get("target_amount_precision")
        amount_tolerance = result.get("target_amount_tolerance")

        st.caption(f"Critère réellement utilisé : **{target_label}**")
        if amount_tolerance is not None and precision is not None:
            tolerance_label = format(amount_tolerance, "f")
            st.caption(
                "Correspondance approchée : écart strictement inférieur à "
                f"**{tolerance_label} {result['target_asset']}** "
                f"(précision saisie : {precision} décimale(s))."
            )

        if result["matches"]:
            exact_count = sum(
                1
                for row in result["matches"]
                if row.get("match_quality") == "exact"
            )
            approximate_count = sum(
                1
                for row in result["matches"]
                if row.get("match_quality") == "approximate"
            )
            st.success(
                f"{len(result['matches'])} correspondance(s) pour {target_label} "
                f"· {exact_count} exacte(s) · "
                f"{approximate_count} approchée(s)."
            )
        else:
            st.warning(no_matches_message(result, target_label))
    else:
        st.caption("Critère réellement utilisé : **aucun montant**")

    st.markdown("#### Vue des données")
    if result["network"] == "Solana":
        movement_view = "📊 Variations de solde"
    elif result["network"] == "Bitcoin":
        movement_view = "📊 Sorties BTC"
    else:
        movement_view = "📊 Mouvements"
    view_options = [
        "🎯 Résultats",
        "🧾 Transactions",
        "💸 Opérations",
        movement_view,
        "ℹ️ Résumé de la recherche",
    ]
    if st.session_state.get("result_view") not in view_options:
        st.session_state["result_view"] = view_options[0]

    def select_result_view(selected_view: str) -> None:
        st.session_state["result_view"] = selected_view

    active_view_index = view_options.index(st.session_state["result_view"])
    st.markdown(
        f"""
        <style>
            .st-key-result_tab_item_{active_view_index}
            [data-testid="stButton"] > button {{
                border-bottom-color: var(--chain-accent) !important;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(
        key="result_tabs_nav",
        horizontal=True,
        wrap=False,
        horizontal_alignment="left",
        vertical_alignment="center",
        gap="xsmall",
    ):
        for index, option in enumerate(view_options):
            with st.container(
                key=f"result_tab_item_{index}",
                width="content",
            ):
                st.button(
                    option,
                    key=f"result_tab_button_{index}",
                    type="secondary",
                    width="content",
                    on_click=select_result_view,
                    args=(option,),
                )

    view = st.session_state["result_view"]

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
            filename = f"{result['network'].lower()}_matches.csv"
            empty_message = (
                no_matches_message(result)
                if not result["matches"]
                else "Aucune correspondance trouvée."
            )
            intro = (
                "Correspondances exactes ou approchées selon la précision "
                "du montant renseigné."
            )

        elif view == "🧾 Transactions":
            source_rows = result["transactions"]
            table_kind = "transactions"
            key_prefix = "transactions"
            filename = f"{result['network'].lower()}_transactions.csv"
            empty_message = "Aucune transaction ne correspond à la recherche."
            intro = (
                "Toutes les transactions de la fenêtre. « — » signifie "
                "qu'aucune opération utilisateur claire n'a été détectée."
            )

        elif view == "💸 Opérations":
            source_rows = result["operations"]
            table_kind = "operations"
            key_prefix = "operations"
            filename = f"{result['network'].lower()}_operations.csv"
            empty_message = "Aucune opération ne correspond à la recherche."
            native_symbol = {
                "Solana": "SOL",
                "Ethereum": "ETH",
                "Bitcoin": "BTC",
            }[result["network"]]
            if result["network"] == "Bitcoin":
                intro = (
                    "Sorties BTC décodées directement depuis les transactions "
                    "du bloc brut."
                )
            else:
                intro = (
                    f"Transferts {native_symbol}, transferts de tokens et swaps "
                    "probables détectés automatiquement."
                )

        else:
            source_rows = result["movements"]
            table_kind = "movements"
            key_prefix = "movements"
            filename = f"{result['network'].lower()}_movements.csv"
            empty_message = (
                "Aucune variation de solde ne correspond à la recherche."
            )
            if result["network"] == "Solana":
                intro = (
                    "Variations de solde SOL et tokens avant/après les transactions."
                )
            elif result["network"] == "Bitcoin":
                intro = (
                    "Sorties BTC positives du bloc, décodées en satoshis et "
                    "adresses standards lorsque le script le permet."
                )
            else:
                intro = (
                    "Mouvements ETH et ERC-20 observables dans les transactions "
                    "et leurs logs."
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
            network=result["network"],
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
            if result["network"] == "Solana":
                st.write("**Borne de début**")
                st.code(f"slot {result['start_slot']}")
                st.write("**Borne de fin**")
                st.code(f"slot {result['end_slot']}")
            else:
                st.write("**Borne de début**")
                st.code(f"bloc {result['start_block']}")
                st.write("**Borne de fin**")
                st.code(f"bloc {result['end_block']}")

        with right:
            st.write("**Plage interrogée**")
            if result["network"] == "Solana":
                st.code(
                    f"{result['query_start_slot']} → "
                    f"{result['query_end_slot']}"
                )
            else:
                st.code(
                    f"{result['query_start_block']} → "
                    f"{result['query_end_block']}"
                )
            st.write("**Candidats / analysés / non entièrement analysés / hors fenêtre**")
            st.code(
                f"{result['candidate_blocks']} / "
                f"{result['analyzed_blocks']} / "
                f"{result['failed_blocks']} / "
                f"{result['outside_window_blocks']}"
            )
            st.write("**Complétude**")
            st.code(
                "Complète"
                if result["search_completeness"] == "complete"
                else "Partielle"
            )

        st.caption(
            "Une seule table est rendue à la fois afin de limiter la mémoire "
            "serveur et la charge du navigateur sur les recherches volumineuses."
        )

        st.write("**Manifest technique**")
        st.json(result["manifest"], expanded=False)
        st.download_button(
            "Télécharger le manifest JSON",
            data=manifest_json_bytes(result["manifest"]),
            file_name=f"{result['network'].lower()}_manifest.json",
            mime="application/json",
            key="download_search_manifest",
            on_click="ignore",
        )
