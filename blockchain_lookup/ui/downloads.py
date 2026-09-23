import pandas as pd
import streamlit as st

from blockchain_lookup.ui.tables import prepare_table_dataframe


def to_csv_bytes(
    rows: list[dict], *, table_kind: str | None = None, network: str | None = None
) -> bytes:
    table = (
        prepare_table_dataframe(rows, table_kind=table_kind, network=network)
        if table_kind is not None and network is not None
        else pd.DataFrame(rows)
    )
    return (
        table
        .to_csv(index=False, sep=";", encoding="utf-8-sig")
        .encode("utf-8-sig")
    )


def render_downloads(
    filtered_rows: list[dict],
    page_rows: list[dict],
    *,
    filename: str,
    key_prefix: str,
    table_kind: str,
    network: str,
) -> None:
    if not page_rows:
        return

    download_col, prepare_col = st.columns(
        [1.25, 1.75],
        vertical_alignment="center",
    )

    with download_col:
        st.download_button(
            "Télécharger la page affichée CSV",
            data=to_csv_bytes(page_rows, table_kind=table_kind, network=network),
            file_name=filename,
            mime="text/csv",
            key=f"{key_prefix}_page_csv",
            on_click="ignore",
            use_container_width=True,
        )

    prepare_full = False
    with prepare_col:
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
            data=to_csv_bytes(filtered_rows, table_kind=table_kind, network=network),
            file_name=filename.replace(".csv", "_complet.csv"),
            mime="text/csv",
            key=f"{key_prefix}_full_csv",
            on_click="ignore",
            use_container_width=True,
        )
