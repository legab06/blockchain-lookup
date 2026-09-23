import streamlit as st


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
