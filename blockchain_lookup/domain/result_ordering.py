from typing import Any


def sort_result_rows(
    rows: list[dict[str, Any]],
    transaction_indices: dict[tuple[int, str], int],
) -> None:
    """Apply one-based transaction positions and a stable newest-first order."""
    for row in rows:
        key = (int(row.get("block", 0)), str(row.get("signature", "")))
        transaction_index = transaction_indices.get(key)
        if transaction_index is not None:
            row["transaction_index"] = transaction_index

    rows.sort(
        key=lambda row: (
            int(row.get("block", 0)),
            int(row.get("transaction_index", 0)),
            str(row.get("signature", "")),
            str(row.get("operation_type", row.get("match_type", ""))),
            str(row.get("asset", row.get("matched_asset", ""))),
            str(row.get("matched_amount", row.get("absolute_delta_amount", ""))),
            str(row.get("sent", "")),
            str(row.get("received", "")),
            str(row.get("source", "")),
            str(row.get("destination", "")),
            str(row.get("account", "")),
            str(row.get("detail", "")),
        ),
        reverse=True,
    )
