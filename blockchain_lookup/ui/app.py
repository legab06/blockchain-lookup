from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, time, timezone
from time import monotonic
from uuid import uuid4

import streamlit as st

from blockchain_lookup.domain.models import SearchResult
from blockchain_lookup.domain.search_manifest import manifest_json_bytes
from blockchain_lookup.engines.bitcoin import BitcoinSearchError, search_bitcoin_window
from blockchain_lookup.engines.ethereum import EthereumSearchError, search_ethereum_window
from blockchain_lookup.engines.solana import SolanaSearchError, search_solana_window
from blockchain_lookup.runtime.concurrency import (
    SEARCH_LIMITER,
    SearchQueueTimeoutError,
    finish_session_search,
    try_start_session_search,
)
from blockchain_lookup.runtime.result_limits import ResultLimitExceeded
from blockchain_lookup.ui.downloads import render_downloads
from blockchain_lookup.ui.errors import log_unexpected_search_error
from blockchain_lookup.ui.messages import no_matches_message, partial_search_warning
from blockchain_lookup.ui.pagination import paginate_rows, render_pagination_controls
from blockchain_lookup.ui.tables import filter_rows, show_table
from blockchain_lookup.ui.themes import apply_base_styles, apply_network_theme


MAX_STATUS_MESSAGES = 60
STATUS_LOG_HEIGHT = 220
PROGRESS_BUCKETS = 50


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


def render_table_footer(
    *,
    filtered_rows: list[dict],
    page_rows: list[dict],
    filename: str,
    key_prefix: str,
    table_kind: str,
    network: str,
    page: int,
    total_pages: int,
    page_size: int,
) -> None:
    if not page_rows:
        return

    footer_left, footer_right = st.columns(
        [2.45, 1.85],
        vertical_alignment="center",
    )

    with footer_left:
        render_downloads(
            filtered_rows,
            page_rows,
            filename=filename,
            key_prefix=key_prefix,
            table_kind=table_kind,
            network=network,
        )

    with footer_right:
        render_pagination_controls(
            total_rows=len(filtered_rows),
            page=page,
            total_pages=total_pages,
            page_size=page_size,
            key_prefix=key_prefix,
        )


def run_app() -> None:
    st.set_page_config(
        page_title="Blockchain Lookup",
        page_icon="🔎",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    apply_base_styles()

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
            st.text_input(
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
            disabled=bool(st.session_state.get("search_running", False)),
        )

    # Emplacements stables : ils permettent de retirer immédiatement l'ancien
    # rendu de résultats pendant qu'une nouvelle recherche synchrone s'exécute.
    search_feedback_slot = st.empty()
    results_slot = st.empty()

    if submitted and try_start_session_search(st.session_state):
        # L'ancien tableau est supprimé côté interface avant tout appel RPC.
        results_slot.empty()
        with search_feedback_slot.container():
            try:
                # Une nouvelle recherche repart du même état propre que le bouton
                # « Effacer la recherche » : ancien résultat, vue, filtres, pagination
                # et préparation CSV disparaissent avant le lancement.
                clear_search_results()

                submitted_amount_raw = str(st.session_state.get("lookup_amount", ""))
                submitted_amount = submitted_amount_raw.strip()
                submitted_asset = str(asset)
                submitted_network = str(network)
                search_id = uuid4().hex[:12]
                started_at = monotonic()
                logger = logging.getLogger(__name__)

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

                        def on_queued() -> None:
                            message = (
                                "Serveur occupé — votre recherche démarrera dès "
                                "qu’une place sera disponible."
                            )
                            status_box.update(label=message, state="running", expanded=True)
                            progress_bar.progress(0, text="En attente d’une place…")
                            on_status(message)

                        try:
                            with SEARCH_LIMITER.slot(
                                search_id=search_id,
                                network=submitted_network,
                                on_queued=on_queued,
                            ):
                                status_box.update(
                                    label=f"Recherche {network} en cours…",
                                    state="running",
                                    expanded=True,
                                )
                                progress_bar.progress(0, text="Recherche démarrée")
                                on_status("Recherche démarrée.")
                                logger.info(
                                    "Search started id=%s network=%s",
                                    search_id,
                                    submitted_network,
                                )
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
                            logger.info(
                                "Search completed id=%s network=%s duration_seconds=%.3f matches=%s",
                                search_id, submitted_network, monotonic() - started_at,
                                len(result["matches"]),
                            )
                        except SearchQueueTimeoutError as exc:
                            logger.warning(
                                "Search failed id=%s network=%s type=%s duration_seconds=%.3f",
                                search_id, submitted_network, type(exc).__name__, monotonic() - started_at,
                            )
                            status_box.update(
                                label="Serveur occupé",
                                state="error",
                                expanded=True,
                            )
                            st.error(str(exc))
                        except ResultLimitExceeded as exc:
                            logger.warning(
                                "Search failed id=%s network=%s type=%s duration_seconds=%.3f",
                                search_id, submitted_network, type(exc).__name__, monotonic() - started_at,
                            )
                            status_box.update(
                                label="Limite de résultats atteinte",
                                state="error",
                                expanded=True,
                            )
                            st.error(str(exc))
                        except ValueError as exc:
                            logger.warning(
                                "Search failed id=%s network=%s type=%s duration_seconds=%.3f",
                                search_id, submitted_network, type(exc).__name__, monotonic() - started_at,
                            )
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
                            logger.warning(
                                "Search failed id=%s network=%s type=%s duration_seconds=%.3f",
                                search_id, submitted_network, type(exc).__name__, monotonic() - started_at,
                            )
                            status_box.update(
                                label="Erreur pendant la recherche",
                                state="error",
                                expanded=True,
                            )
                            st.error(str(exc))
                        except Exception as exc:
                            logger.warning(
                                "Search failed id=%s network=%s type=%s duration_seconds=%.3f",
                                search_id, submitted_network, type(exc).__name__, monotonic() - started_at,
                            )
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

            finally:
                finish_session_search(st.session_state)

    result: SearchResult | None = st.session_state.get("lookup_result")

    with results_slot.container():
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

                render_table_footer(
                    filtered_rows=filtered_rows,
                    page_rows=page_rows,
                    filename=filename,
                    key_prefix=key_prefix,
                    table_kind=table_kind,
                    network=result["network"],
                    page=page,
                    total_pages=total_pages,
                    page_size=page_size,
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
                    if result.get("covered_start_dt") and result.get("covered_end_dt"):
                        st.write("**Couverture temporelle disponible**")
                        st.code(
                            f"{result['covered_start_dt'].strftime('%d/%m/%Y %H:%M:%S')} → "
                            f"{result['covered_end_dt'].strftime('%d/%m/%Y %H:%M:%S')} UTC"
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
