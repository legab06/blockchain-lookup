import streamlit as st


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


def apply_base_styles() -> None:
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
