from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Any

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_RPC_DELAY = 0.15
BOUNDARY_SLOT_MARGIN = 10
LAMPORTS_PER_SOL = Decimal("1000000000")

ProgressCallback = Callable[[int, int, int], None]
StatusCallback = Callable[[str], None]


class SolanaSearchError(RuntimeError):
    """Erreur lisible par l'interface lors d'une recherche Solana."""


def _notify(callback: StatusCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _parse_amount(amount_sol: str | Decimal | None) -> tuple[Decimal | None, int | None]:
    if amount_sol is None:
        return None, None

    raw = str(amount_sol).strip().replace(",", ".")
    if not raw:
        return None, None

    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Montant SOL invalide.") from exc

    if amount < 0:
        raise ValueError("Le montant SOL doit être positif ou nul.")

    lamports = amount * LAMPORTS_PER_SOL
    if lamports != lamports.to_integral_value():
        raise ValueError("Le montant SOL ne peut pas dépasser 9 décimales.")

    return amount, int(lamports)


def search_solana_window(
    search_date: date,
    search_time: dt_time,
    tolerance_seconds: int = 30,
    amount_sol: str | Decimal | None = None,
    *,
    rpc_url: str = DEFAULT_RPC_URL,
    rpc_delay: float = DEFAULT_RPC_DELAY,
    retries: int = 10,
    progress_callback: ProgressCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """
    Recherche les transactions Solana situées dans une fenêtre UTC.

    Le résultat contient les mêmes catégories que le script CLI d'origine :
    transactions, transferts SOL, variations de solde et correspondances du montant.
    """

    if tolerance_seconds < 0:
        raise ValueError("La tolérance doit être positive ou nulle.")

    target_sol, target_lamports = _parse_amount(amount_sol)

    center_dt = datetime.combine(search_date, search_time).replace(tzinfo=timezone.utc)
    start_dt = center_dt - timedelta(seconds=tolerance_seconds)
    end_dt = center_dt + timedelta(seconds=tolerance_seconds)

    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    def rpc(method: str, params: list | None = None):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }
        data = json.dumps(payload).encode()

        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    rpc_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "BlockchainLookupStreamlit/1.0",
                    },
                )

                with urllib.request.urlopen(req, timeout=90) as response:
                    result = json.loads(response.read().decode())

                if "error" in result:
                    raise RuntimeError(result["error"])

                if rpc_delay:
                    time.sleep(rpc_delay)
                return result["result"]

            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    wait = 2 + attempt * 2
                    _notify(
                        status_callback,
                        f"Limite du RPC Solana atteinte (429) — nouvelle tentative dans {wait}s…",
                    )
                    time.sleep(wait)
                    continue
                raise SolanaSearchError(f"Erreur HTTP RPC Solana : {exc}") from exc

            except Exception as exc:
                if attempt == retries - 1:
                    raise SolanaSearchError(
                        f"Impossible de joindre le RPC Solana : {exc}"
                    ) from exc

                wait = 2 + attempt
                _notify(
                    status_callback,
                    f"Erreur RPC ({exc}) — tentative {attempt + 1}/{retries}, reprise dans {wait}s…",
                )
                time.sleep(wait)

        raise SolanaSearchError("Impossible de joindre le RPC Solana.")

    def valid_slot_near(slot: int, low: int, high: int, radius: int = 100):
        first = max(low, slot - radius)
        last = min(high, slot + radius)
        blocks = rpc("getBlocks", [first, last, {"commitment": "finalized"}])
        if not blocks:
            return None
        return min(blocks, key=lambda value: abs(value - slot))

    def get_block_time(slot: int):
        return rpc("getBlockTime", [slot])

    def find_nearest_slot_for_timestamp(target_ts: int):
        low = rpc("getFirstAvailableBlock")
        high = rpc("getSlot", [{"commitment": "finalized"}])

        best_slot = None
        best_time = None
        best_diff = float("inf")

        while low <= high:
            midpoint = (low + high) // 2
            slot = valid_slot_near(midpoint, low, high)

            if slot is None:
                raise SolanaSearchError(
                    f"Aucun bloc valide trouvé autour du slot {midpoint}."
                )

            block_time = get_block_time(slot)
            if block_time is None:
                raise SolanaSearchError(f"Le bloc {slot} n'a pas de timestamp.")

            diff = block_time - target_ts
            if abs(diff) < best_diff:
                best_diff = abs(diff)
                best_slot = slot
                best_time = block_time

            if block_time < target_ts:
                low = slot + 1
            elif block_time > target_ts:
                high = slot - 1
            else:
                return slot, block_time

        if best_slot is None or best_time is None:
            raise SolanaSearchError("Impossible de déterminer le slot correspondant à la date.")

        return best_slot, best_time

    _notify(status_callback, "Recherche de la borne de début…")
    start_slot, start_slot_time = find_nearest_slot_for_timestamp(start_ts)

    _notify(status_callback, "Recherche de la borne de fin…")
    end_slot, end_slot_time = find_nearest_slot_for_timestamp(end_ts)

    query_start_slot = max(0, min(start_slot, end_slot) - BOUNDARY_SLOT_MARGIN)
    query_end_slot = max(start_slot, end_slot) + BOUNDARY_SLOT_MARGIN

    _notify(
        status_callback,
        f"Récupération des blocs {query_start_slot} → {query_end_slot}…",
    )
    blocks_to_check = rpc(
        "getBlocks",
        [query_start_slot, query_end_slot, {"commitment": "finalized"}],
    )

    transactions_rows: list[dict[str, Any]] = []
    transfers_rows: list[dict[str, Any]] = []
    movements_rows: list[dict[str, Any]] = []
    matches_rows: list[dict[str, Any]] = []

    analyzed_blocks = 0
    skipped_blocks = 0

    def add_match(
        match_type: str,
        block_slot: int,
        block_time: str,
        signature: str,
        amount_lamports: int,
        source: str = "",
        destination: str = "",
        account: str = "",
        detail: str = "",
    ) -> None:
        if target_lamports is None:
            return
        if abs(int(amount_lamports)) != target_lamports:
            return

        matches_rows.append(
            {
                "match_type": match_type,
                "block": block_slot,
                "block_time_utc": block_time,
                "signature": signature,
                "source": source,
                "destination": destination,
                "account": account,
                "amount_lamports": int(amount_lamports),
                "amount_sol": f"{Decimal(abs(int(amount_lamports))) / LAMPORTS_PER_SOL:.9f}",
                "detail": detail,
                "explorer": f"https://explorer.solana.com/tx/{signature}",
                "solscan": f"https://solscan.io/tx/{signature}",
            }
        )

    def extract_sol_instruction(
        instruction: Any,
        block_slot: int,
        block_time: str,
        signature: str,
        location: str,
    ) -> None:
        if not isinstance(instruction, dict):
            return

        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            return

        instruction_type = parsed.get("type", "")
        info = parsed.get("info", {})
        if not isinstance(info, dict):
            return

        lamports = info.get("lamports")
        if lamports is None:
            return

        try:
            lamports = int(lamports)
        except (TypeError, ValueError):
            return

        source = info.get("source") or info.get("from") or info.get("account") or ""
        destination = (
            info.get("destination")
            or info.get("to")
            or info.get("newAccount")
            or info.get("newAccountPubkey")
            or ""
        )

        transfers_rows.append(
            {
                "block": block_slot,
                "block_time_utc": block_time,
                "signature": signature,
                "location": location,
                "instruction_type": instruction_type,
                "source": source,
                "destination": destination,
                "lamports": lamports,
                "sol": f"{Decimal(lamports) / LAMPORTS_PER_SOL:.9f}",
                "match_target": (
                    "OUI"
                    if target_lamports is not None and lamports == target_lamports
                    else ""
                ),
                "explorer": f"https://explorer.solana.com/tx/{signature}",
                "solscan": f"https://solscan.io/tx/{signature}",
            }
        )

        if target_lamports is not None and lamports == target_lamports:
            add_match(
                match_type="instruction",
                block_slot=block_slot,
                block_time=block_time,
                signature=signature,
                amount_lamports=lamports,
                source=source,
                destination=destination,
                detail=f"{location} / {instruction_type}",
            )

    total_candidates = len(blocks_to_check)
    _notify(status_callback, f"Analyse de {total_candidates} blocs candidats…")

    for block_number, block_slot in enumerate(blocks_to_check, start=1):
        if progress_callback:
            progress_callback(block_number - 1, total_candidates, block_slot)

        try:
            block = rpc(
                "getBlock",
                [
                    block_slot,
                    {
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "rewards": False,
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            )
        except SolanaSearchError:
            skipped_blocks += 1
            continue

        if block is None:
            skipped_blocks += 1
            continue

        block_timestamp = block.get("blockTime")
        if block_timestamp is None:
            skipped_blocks += 1
            continue

        if not (start_ts <= block_timestamp <= end_ts):
            continue

        analyzed_blocks += 1
        block_time = datetime.fromtimestamp(
            block_timestamp,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        transactions = block.get("transactions", [])

        for tx_index, tx in enumerate(transactions, start=1):
            transaction = tx.get("transaction", {})
            meta = tx.get("meta", {}) or {}

            signatures = transaction.get("signatures", [])
            signature = signatures[0] if signatures else ""

            success = meta.get("err") is None
            fee_lamports = int(meta.get("fee", 0) or 0)

            message = transaction.get("message", {})
            account_keys = message.get("accountKeys", [])
            pre_balances = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])

            addresses = []
            for account in account_keys:
                if isinstance(account, dict):
                    address = account.get("pubkey", "")
                else:
                    address = str(account)
                addresses.append(address)

            transactions_rows.append(
                {
                    "block": block_slot,
                    "block_time_utc": block_time,
                    "transaction_index": tx_index,
                    "signature": signature,
                    "status": "SUCCESS" if success else "FAILED",
                    "fee_lamports": fee_lamports,
                    "fee_sol": f"{Decimal(fee_lamports) / LAMPORTS_PER_SOL:.9f}",
                    "account_count": len(addresses),
                    "accounts": " | ".join(addresses),
                    "error": "" if success else json.dumps(meta.get("err"), ensure_ascii=False),
                    "explorer": f"https://explorer.solana.com/tx/{signature}",
                    "solscan": f"https://solscan.io/tx/{signature}",
                }
            )

            for account_index, address in enumerate(addresses):
                if account_index >= len(pre_balances) or account_index >= len(post_balances):
                    continue

                before = int(pre_balances[account_index])
                after = int(post_balances[account_index])
                delta = after - before
                if delta == 0:
                    continue

                movements_rows.append(
                    {
                        "block": block_slot,
                        "block_time_utc": block_time,
                        "signature": signature,
                        "account": address,
                        "balance_before_lamports": before,
                        "balance_after_lamports": after,
                        "delta_lamports": delta,
                        "delta_sol": f"{Decimal(delta) / LAMPORTS_PER_SOL:+.9f}",
                        "absolute_delta_sol": f"{Decimal(abs(delta)) / LAMPORTS_PER_SOL:.9f}",
                        "match_target": (
                            "OUI"
                            if target_lamports is not None and abs(delta) == target_lamports
                            else ""
                        ),
                        "explorer": f"https://explorer.solana.com/tx/{signature}",
                        "solscan": f"https://solscan.io/tx/{signature}",
                    }
                )

                if target_lamports is not None and abs(delta) == target_lamports:
                    add_match(
                        match_type="balance_delta",
                        block_slot=block_slot,
                        block_time=block_time,
                        signature=signature,
                        amount_lamports=delta,
                        account=address,
                        detail=f"variation de solde {delta:+d} lamports",
                    )

            for instruction_index, instruction in enumerate(message.get("instructions", [])):
                extract_sol_instruction(
                    instruction,
                    block_slot,
                    block_time,
                    signature,
                    f"outer:{instruction_index}",
                )

            for group in meta.get("innerInstructions", []) or []:
                parent_index = group.get("index", "")
                for inner_index, instruction in enumerate(group.get("instructions", [])):
                    extract_sol_instruction(
                        instruction,
                        block_slot,
                        block_time,
                        signature,
                        f"inner:{parent_index}:{inner_index}",
                    )

    if progress_callback:
        progress_callback(total_candidates, total_candidates, query_end_slot)

    unique_matches = []
    seen_matches = set()
    for row in matches_rows:
        key = (
            row["match_type"],
            row["signature"],
            row["source"],
            row["destination"],
            row["account"],
            row["amount_lamports"],
            row["detail"],
        )
        if key not in seen_matches:
            seen_matches.add(key)
            unique_matches.append(row)

    _notify(status_callback, "Recherche terminée.")

    return {
        "network": "Solana",
        "center_dt": center_dt,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "tolerance_seconds": tolerance_seconds,
        "target_sol": target_sol,
        "target_lamports": target_lamports,
        "start_slot": start_slot,
        "start_slot_time": start_slot_time,
        "end_slot": end_slot,
        "end_slot_time": end_slot_time,
        "query_start_slot": query_start_slot,
        "query_end_slot": query_end_slot,
        "candidate_blocks": total_candidates,
        "analyzed_blocks": analyzed_blocks,
        "skipped_blocks": skipped_blocks,
        "transactions": transactions_rows,
        "transfers": transfers_rows,
        "movements": movements_rows,
        "matches": unique_matches,
    }
