from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from amount_matching import AmountCriterion, parse_amount_criterion

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_RPC_DELAY = 0.15
BOUNDARY_SLOT_MARGIN = 10
MAX_SUPPORTED_TRANSACTION_VERSION = 1
LAMPORTS_PER_SOL = Decimal("1000000000")

KNOWN_TOKEN_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "PYUSD": "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
    "USDG": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
}
KNOWN_TOKEN_DECIMALS = {
    "USDC": 6,
    "USDT": 6,
    "PYUSD": 6,
    "USDG": 6,
}
WSOL_MINT = "So11111111111111111111111111111111111111112"
MINT_TO_SYMBOL = {mint: symbol for symbol, mint in KNOWN_TOKEN_MINTS.items()}
MINT_TO_SYMBOL[WSOL_MINT] = "WSOL"

ProgressCallback = Callable[[int, int, int], None]
StatusCallback = Callable[[str], None]


class SolanaSearchError(RuntimeError):
    """Erreur lisible par l'interface lors d'une recherche Solana."""


def _notify(callback: StatusCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _parse_amount(value: str | Decimal | None, asset: str) -> Decimal | None:
    if value is None:
        return None

    raw = str(value).strip().replace(",", ".")
    if not raw:
        return None

    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Montant invalide.") from exc

    if amount < 0:
        raise ValueError("Le montant doit être positif ou nul.")

    if asset == "SOL":
        lamports = amount * LAMPORTS_PER_SOL
        if lamports != lamports.to_integral_value():
            raise ValueError("Un montant SOL ne peut pas dépasser 9 décimales.")

    return amount


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _asset_label(mint: str) -> str:
    if not mint:
        return "Token"
    if mint in MINT_TO_SYMBOL:
        return MINT_TO_SYMBOL[mint]
    if len(mint) > 10:
        return f"{mint[:4]}…{mint[-4:]}"
    return mint


def _base58_decode(value: str) -> bytes:
    """Décode une chaîne base58 sans dépendance externe."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    index = {char: position for position, char in enumerate(alphabet)}

    number = 0
    for char in value:
        if char not in index:
            raise ValueError("Donnée non base58")
        number = number * 58 + index[char]

    decoded = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + decoded


def _target_raw_units(asset: str, amount: Decimal | None) -> int | None:
    if amount is None:
        return None

    if asset == "SOL":
        raw = amount * LAMPORTS_PER_SOL
    else:
        decimals = KNOWN_TOKEN_DECIMALS.get(asset)
        if decimals is None:
            return None
        raw = amount * (Decimal(10) ** decimals)

    if raw != raw.to_integral_value():
        return None

    return int(raw)


def _raw_instruction_contains_amount(data: Any, raw_amount: int | None) -> bool:
    """
    Cherche un montant u64 little-endian dans les données brutes d'une
    instruction. Les programmes Solana encodent très souvent les montants
    d'entrée/sortie de cette façon (Borsh/Anchor).
    """
    if raw_amount is None or raw_amount < 0 or raw_amount > 0xFFFFFFFFFFFFFFFF:
        return False

    if isinstance(data, (list, tuple)) and data:
        encoded = data[0]
    else:
        encoded = data

    if not isinstance(encoded, str) or not encoded:
        return False

    try:
        decoded = _base58_decode(encoded)
    except ValueError:
        return False

    needle = int(raw_amount).to_bytes(8, "little", signed=False)
    return needle in decoded


def _token_balance_amount(entry: dict[str, Any] | None) -> tuple[Decimal, int]:
    if not entry:
        return Decimal(0), 0

    ui = entry.get("uiTokenAmount") or {}
    raw_amount = str(ui.get("amount", "0"))
    decimals = int(ui.get("decimals", 0) or 0)

    try:
        amount = Decimal(raw_amount) / (Decimal(10) ** decimals)
    except (InvalidOperation, ValueError):
        amount = Decimal(0)

    return amount, decimals


def search_solana_window(
    search_date: date,
    search_time: dt_time,
    tolerance_seconds: int = 30,
    amount_sol: str | Decimal | None = None,
    *,
    asset_symbol: str = "SOL",
    rpc_url: str = DEFAULT_RPC_URL,
    rpc_delay: float = DEFAULT_RPC_DELAY,
    retries: int = 10,
    progress_callback: ProgressCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """
    Recherche les transactions Solana situées dans une fenêtre UTC.

    amount_sol conserve son nom pour compatibilité avec la première version de
    l'application, mais représente désormais le montant communiqué pour
    l'actif choisi avec asset_symbol.
    """

    if tolerance_seconds < 0:
        raise ValueError("La tolérance doit être positive ou nulle.")

    target_asset = str(asset_symbol or "SOL").strip().upper()
    target_mint = KNOWN_TOKEN_MINTS.get(target_asset, "")
    target_max_decimals = (
        9
        if target_asset == "SOL"
        else KNOWN_TOKEN_DECIMALS.get(target_asset, 9)
    )
    target_criterion: AmountCriterion | None = parse_amount_criterion(
        amount_sol,
        max_decimals=target_max_decimals,
    )
    target_amount = (
        target_criterion.amount
        if target_criterion is not None
        else None
    )

    target_lamports: int | None = None
    if target_asset == "SOL" and target_amount is not None:
        target_lamports = int(target_amount * LAMPORTS_PER_SOL)

    target_raw_units = _target_raw_units(target_asset, target_amount)

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
                        "User-Agent": "BlockchainLookupStreamlit/2.0",
                    },
                )

                with urllib.request.urlopen(req, timeout=90) as response:
                    result = json.loads(response.read().decode())

                if "error" in result:
                    error = result["error"]
                    if isinstance(error, dict) and error.get("code") == -32015:
                        raise SolanaSearchError(
                            "Version de transaction Solana non prise en charge : "
                            f"{error.get('message', error)}"
                        )
                    raise RuntimeError(error)

                if rpc_delay:
                    time.sleep(rpc_delay)
                return result["result"]

            except SolanaSearchError:
                raise

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

    def target_match_quality(
        asset: str,
        mint: str,
        amount: Decimal,
    ) -> str | None:
        if target_criterion is None:
            return None

        if target_asset == "SOL":
            if not (asset in {"SOL", "WSOL"} or mint == WSOL_MINT):
                return None
        elif target_mint:
            if mint != target_mint:
                return None
        elif asset.upper() != target_asset:
            return None

        return target_criterion.classify(amount)

    def target_matches(asset: str, mint: str, amount: Decimal) -> bool:
        return target_match_quality(asset, mint, amount) is not None

    def target_match_marker(asset: str, mint: str, amount: Decimal) -> str:
        quality = target_match_quality(asset, mint, amount)
        return {
            "exact": "EXACT",
            "approximate": "APPROX",
        }.get(quality, "")

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
    operations_rows: list[dict[str, Any]] = []
    transfer_evidence_rows: list[dict[str, Any]] = []
    raw_amount_evidence_rows: list[dict[str, Any]] = []

    analyzed_blocks = 0
    skipped_blocks = 0

    def append_operation(
        *,
        operation_type: str,
        block_slot: int,
        block_time: str,
        signature: str,
        sent: str = "",
        received: str = "",
        source: str = "",
        destination: str = "",
        account: str = "",
        evidence: str = "",
        detail: str = "",
        legs: list[dict[str, str]] | None = None,
    ) -> None:
        operations_rows.append(
            {
                "operation_type": operation_type,
                "block": block_slot,
                "block_time_utc": block_time,
                "signature": signature,
                "sent": sent,
                "received": received,
                "source": source,
                "destination": destination,
                "account": account,
                "evidence": evidence,
                "detail": detail,
                "legs": legs or [],
                "explorer": f"https://explorer.solana.com/tx/{signature}",
                "solscan": f"https://solscan.io/tx/{signature}",
            }
        )

    def append_raw_amount_evidence(
        *,
        block_slot: int,
        block_time: str,
        signature: str,
        location: str,
        program_id: str,
    ) -> None:
        raw_amount_evidence_rows.append(
            {
                "block": block_slot,
                "block_time_utc": block_time,
                "signature": signature,
                "location": location,
                "program_id": program_id,
                "explorer": f"https://explorer.solana.com/tx/{signature}",
                "solscan": f"https://solscan.io/tx/{signature}",
            }
        )

    def inspect_raw_instruction_amount(
        instruction: Any,
        *,
        block_slot: int,
        block_time: str,
        signature: str,
        location: str,
    ) -> None:
        if target_raw_units is None or not isinstance(instruction, dict):
            return

        data = instruction.get("data")
        if not _raw_instruction_contains_amount(data, target_raw_units):
            return

        program_id = str(
            instruction.get("programId")
            or instruction.get("program_id")
            or instruction.get("program")
            or ""
        )

        append_raw_amount_evidence(
            block_slot=block_slot,
            block_time=block_time,
            signature=signature,
            location=location,
            program_id=program_id,
        )

    def append_transfer_evidence(
        *,
        block_slot: int,
        block_time: str,
        signature: str,
        asset: str,
        mint: str,
        amount: str,
        source: str,
        destination: str,
        location: str,
    ) -> None:
        transfer_evidence_rows.append(
            {
                "block": block_slot,
                "block_time_utc": block_time,
                "signature": signature,
                "asset": asset,
                "mint": mint,
                "amount": amount,
                "source": source,
                "destination": destination,
                "location": location,
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
        if instruction_type not in {"transfer", "transferWithSeed"}:
            return

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
        amount = Decimal(lamports) / LAMPORTS_PER_SOL
        amount_text = _format_decimal(amount)

        append_transfer_evidence(
            block_slot=block_slot,
            block_time=block_time,
            signature=signature,
            asset="SOL",
            mint="",
            amount=amount_text,
            source=source,
            destination=destination,
            location=location,
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
                "sol": amount_text,
                "match_target": target_match_marker("SOL", "", amount),
                "explorer": f"https://explorer.solana.com/tx/{signature}",
                "solscan": f"https://solscan.io/tx/{signature}",
            }
        )

        # Les inner instructions servent de preuve technique, mais on évite de
        # les présenter comme des opérations utilisateur distinctes.
        if not location.startswith("outer:"):
            return

        append_operation(
            operation_type="transfer",
            block_slot=block_slot,
            block_time=block_time,
            signature=signature,
            sent=f"{amount_text} SOL",
            source=source,
            destination=destination,
            evidence="Instruction de transfert SOL",
            detail=instruction_type,
            legs=[
                {
                    "direction": "transfer",
                    "asset": "SOL",
                    "mint": "",
                    "amount": amount_text,
                }
            ],
        )

    def extract_token_instruction(
        instruction: Any,
        block_slot: int,
        block_time: str,
        signature: str,
        location: str,
        token_accounts: dict[str, dict[str, Any]],
    ) -> None:
        if not isinstance(instruction, dict):
            return

        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            return

        instruction_type = parsed.get("type", "")
        if instruction_type not in {"transfer", "transferChecked"}:
            return

        info = parsed.get("info", {})
        if not isinstance(info, dict):
            return

        source_account = str(info.get("source") or "")
        destination_account = str(info.get("destination") or "")
        source_meta = token_accounts.get(source_account, {})
        destination_meta = token_accounts.get(destination_account, {})

        mint = str(
            info.get("mint")
            or source_meta.get("mint")
            or destination_meta.get("mint")
            or ""
        )

        token_amount = info.get("tokenAmount")
        decimals = None
        raw_amount = None

        if isinstance(token_amount, dict):
            raw_amount = token_amount.get("amount")
            decimals = token_amount.get("decimals")

        if raw_amount is None:
            raw_amount = info.get("amount")

        if decimals is None:
            decimals = source_meta.get("decimals")
        if decimals is None:
            decimals = destination_meta.get("decimals")

        if raw_amount is None or decimals is None:
            return

        try:
            amount = Decimal(str(raw_amount)) / (Decimal(10) ** int(decimals))
        except (InvalidOperation, TypeError, ValueError):
            return

        asset = _asset_label(mint)
        amount_text = _format_decimal(amount)
        source = str(source_meta.get("owner") or info.get("authority") or source_account)
        destination = str(destination_meta.get("owner") or destination_account)

        append_transfer_evidence(
            block_slot=block_slot,
            block_time=block_time,
            signature=signature,
            asset=asset,
            mint=mint,
            amount=amount_text,
            source=source,
            destination=destination,
            location=location,
        )

        if not location.startswith("outer:"):
            return

        append_operation(
            operation_type="token_transfer",
            block_slot=block_slot,
            block_time=block_time,
            signature=signature,
            sent=f"{amount_text} {asset}",
            source=source,
            destination=destination,
            evidence="Instruction de transfert de token",
            detail=instruction_type,
            legs=[
                {
                    "direction": "transfer",
                    "asset": asset,
                    "mint": mint,
                    "amount": amount_text,
                }
            ],
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
                        "maxSupportedTransactionVersion": MAX_SUPPORTED_TRANSACTION_VERSION,
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

            addresses: list[str] = []
            signer_addresses: set[str] = set()

            for account in account_keys:
                if isinstance(account, dict):
                    address = str(account.get("pubkey", ""))
                    if account.get("signer"):
                        signer_addresses.add(address)
                else:
                    address = str(account)
                addresses.append(address)

            if addresses:
                signer_addresses.add(addresses[0])

            transactions_rows.append(
                {
                    "block": block_slot,
                    "block_time_utc": block_time,
                    "transaction_index": tx_index,
                    "signature": signature,
                    "status": "SUCCESS" if success else "FAILED",
                    "fee_lamports": fee_lamports,
                    "fee_sol": _format_decimal(
                        Decimal(fee_lamports) / LAMPORTS_PER_SOL
                    ),
                    "account_count": len(addresses),
                    "accounts": " | ".join(addresses),
                    "error": "" if success else json.dumps(meta.get("err"), ensure_ascii=False),
                    "explorer": f"https://explorer.solana.com/tx/{signature}",
                    "solscan": f"https://solscan.io/tx/{signature}",
                }
            )

            owner_asset_deltas: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)

            # Variations natives SOL.
            for account_index, address in enumerate(addresses):
                if account_index >= len(pre_balances) or account_index >= len(post_balances):
                    continue

                before_lamports = int(pre_balances[account_index])
                after_lamports = int(post_balances[account_index])
                delta_lamports = after_lamports - before_lamports

                if delta_lamports == 0:
                    continue

                before = Decimal(before_lamports) / LAMPORTS_PER_SOL
                after = Decimal(after_lamports) / LAMPORTS_PER_SOL
                delta = Decimal(delta_lamports) / LAMPORTS_PER_SOL

                movements_rows.append(
                    {
                        "block": block_slot,
                        "block_time_utc": block_time,
                        "signature": signature,
                        "account": address,
                        "asset": "SOL",
                        "mint": "",
                        "balance_before": _format_decimal(before),
                        "balance_after": _format_decimal(after),
                        "delta_amount": (
                            ("+" if delta > 0 else "") + _format_decimal(delta)
                        ),
                        "absolute_delta_amount": _format_decimal(abs(delta)),
                        "match_target": target_match_marker("SOL", "", delta),
                        "explorer": f"https://explorer.solana.com/tx/{signature}",
                        "solscan": f"https://solscan.io/tx/{signature}",
                    }
                )

                if address in signer_addresses:
                    economic_delta_lamports = delta_lamports
                    if account_index == 0:
                        # Le premier compte paie habituellement les frais.
                        economic_delta_lamports += fee_lamports

                    if economic_delta_lamports:
                        owner_asset_deltas[(address, "SOL", "")] += (
                            Decimal(economic_delta_lamports) / LAMPORTS_PER_SOL
                        )

            # Variations des tokens SPL déjà présentes dans les métadonnées RPC.
            pre_token_balances = meta.get("preTokenBalances") or []
            post_token_balances = meta.get("postTokenBalances") or []

            token_states: dict[tuple[int, str], dict[str, Any]] = {}

            for entry in pre_token_balances:
                try:
                    account_index = int(entry.get("accountIndex"))
                except (TypeError, ValueError):
                    continue
                mint = str(entry.get("mint") or "")
                if not mint:
                    continue
                token_states.setdefault((account_index, mint), {})["pre"] = entry

            for entry in post_token_balances:
                try:
                    account_index = int(entry.get("accountIndex"))
                except (TypeError, ValueError):
                    continue
                mint = str(entry.get("mint") or "")
                if not mint:
                    continue
                token_states.setdefault((account_index, mint), {})["post"] = entry

            token_accounts: dict[str, dict[str, Any]] = {}

            for (account_index, mint), state in token_states.items():
                pre_entry = state.get("pre")
                post_entry = state.get("post")

                before, pre_decimals = _token_balance_amount(pre_entry)
                after, post_decimals = _token_balance_amount(post_entry)
                decimals = post_decimals if post_entry else pre_decimals
                delta = after - before

                owner = str(
                    (post_entry or {}).get("owner")
                    or (pre_entry or {}).get("owner")
                    or ""
                )
                asset = _asset_label(mint)

                token_account_address = (
                    addresses[account_index] if 0 <= account_index < len(addresses) else ""
                )
                if token_account_address:
                    token_accounts[token_account_address] = {
                        "owner": owner,
                        "mint": mint,
                        "asset": asset,
                        "decimals": decimals,
                    }

                if delta == 0:
                    continue

                movements_rows.append(
                    {
                        "block": block_slot,
                        "block_time_utc": block_time,
                        "signature": signature,
                        "account": owner or token_account_address,
                        "asset": asset,
                        "mint": mint,
                        "balance_before": _format_decimal(before),
                        "balance_after": _format_decimal(after),
                        "delta_amount": (
                            ("+" if delta > 0 else "") + _format_decimal(delta)
                        ),
                        "absolute_delta_amount": _format_decimal(abs(delta)),
                        "match_target": target_match_marker(asset, mint, delta),
                        "explorer": f"https://explorer.solana.com/tx/{signature}",
                        "solscan": f"https://solscan.io/tx/{signature}",
                    }
                )

                if owner and owner in signer_addresses:
                    if mint == WSOL_MINT:
                        # Pour l'analyse économique, SOL et Wrapped SOL sont
                        # le même actif. Cela évite de classer un wrap/unwrap
                        # technique comme un swap utilisateur.
                        owner_asset_deltas[(owner, "SOL", "")] += delta
                    else:
                        owner_asset_deltas[(owner, asset, mint)] += delta

            # Un même signataire qui perd un actif et en reçoit un autre est
            # présenté comme un échange probable. Cette heuristique ne dépend
            # d'aucun DEX particulier et n'ajoute aucun appel réseau.
            deltas_by_owner: dict[str, list[tuple[str, str, Decimal]]] = defaultdict(list)
            for (owner, asset, mint), delta in owner_asset_deltas.items():
                if delta:
                    deltas_by_owner[owner].append((asset, mint, delta))

            for owner, legs in deltas_by_owner.items():
                negatives = [leg for leg in legs if leg[2] < 0]
                positives = [leg for leg in legs if leg[2] > 0]

                distinct_assets = {(asset, mint) for asset, mint, _ in legs}
                if not negatives or not positives or len(distinct_assets) < 2:
                    continue

                sent_parts = [
                    f"{_format_decimal(abs(delta))} {asset}"
                    for asset, _mint, delta in sorted(
                        negatives,
                        key=lambda item: abs(item[2]),
                        reverse=True,
                    )
                ]
                received_parts = [
                    f"{_format_decimal(delta)} {asset}"
                    for asset, _mint, delta in sorted(
                        positives,
                        key=lambda item: abs(item[2]),
                        reverse=True,
                    )
                ]

                operation_legs: list[dict[str, str]] = []
                for asset, mint, delta in negatives:
                    operation_legs.append(
                        {
                            "direction": "sent",
                            "asset": asset,
                            "mint": mint,
                            "amount": _format_decimal(abs(delta)),
                        }
                    )
                for asset, mint, delta in positives:
                    operation_legs.append(
                        {
                            "direction": "received",
                            "asset": asset,
                            "mint": mint,
                            "amount": _format_decimal(delta),
                        }
                    )

                append_operation(
                    operation_type="swap_probable",
                    block_slot=block_slot,
                    block_time=block_time,
                    signature=signature,
                    sent=" + ".join(sent_parts),
                    received=" + ".join(received_parts),
                    account=owner,
                    evidence="Variations nettes de plusieurs actifs",
                    detail="Échange déduit des soldes avant/après",
                    legs=operation_legs,
                )

            # Instructions principales : transferts directs utilisateur.
            for instruction_index, instruction in enumerate(message.get("instructions", [])):
                location = f"outer:{instruction_index}"
                inspect_raw_instruction_amount(
                    instruction,
                    block_slot=block_slot,
                    block_time=block_time,
                    signature=signature,
                    location=location,
                )
                extract_sol_instruction(
                    instruction,
                    block_slot,
                    block_time,
                    signature,
                    location,
                )
                extract_token_instruction(
                    instruction,
                    block_slot,
                    block_time,
                    signature,
                    location,
                    token_accounts,
                )

            # Les inner instructions ne créent pas d'opération utilisateur
            # distincte, mais servent de preuve technique pour retrouver un
            # montant exact à l'intérieur d'un swap.
            for group in meta.get("innerInstructions", []) or []:
                parent_index = group.get("index", "")
                for inner_index, instruction in enumerate(group.get("instructions", [])):
                    location = f"inner:{parent_index}:{inner_index}"
                    inspect_raw_instruction_amount(
                        instruction,
                        block_slot=block_slot,
                        block_time=block_time,
                        signature=signature,
                        location=location,
                    )
                    extract_sol_instruction(
                        instruction,
                        block_slot,
                        block_time,
                        signature,
                        location,
                    )
                    extract_token_instruction(
                        instruction,
                        block_slot,
                        block_time,
                        signature,
                        location,
                        token_accounts,
                    )

    if progress_callback:
        progress_callback(total_candidates, total_candidates, query_end_slot)

    # Déduplication des opérations.
    unique_operations: list[dict[str, Any]] = []
    seen_operations: set[tuple[Any, ...]] = set()

    for row in operations_rows:
        key = (
            row["operation_type"],
            row["signature"],
            row["sent"],
            row["received"],
            row["source"],
            row["destination"],
            row["account"],
        )
        if key in seen_operations:
            continue
        seen_operations.add(key)
        unique_operations.append(row)

    operations_rows = unique_operations

    # Résumé lisible dans l'onglet Transactions.
    operations_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in operations_rows:
        operations_by_signature[operation["signature"]].append(operation)

    for transaction_row in transactions_rows:
        tx_operations = operations_by_signature.get(transaction_row["signature"], [])
        summaries: list[str] = []

        for operation in tx_operations:
            if operation["operation_type"] == "swap_probable":
                summary = f"Swap probable : {operation['sent']} → {operation['received']}"
            else:
                summary = operation["sent"] or operation["received"] or "Opération"
            if summary not in summaries:
                summaries.append(summary)

        transaction_row["operation_count"] = len(tx_operations)
        transaction_row["operation_summary"] = " | ".join(summaries)

    # Correspondances exactes ou approchées : on cherche le montant sur toutes
    # les jambes d'une opération, puis on conserve les variations de solde
    # comme filet de sécurité.
    matches_rows: list[dict[str, Any]] = []
    matched_signature_asset_amount: set[tuple[str, str, str]] = set()

    if target_amount is not None:
        for operation in operations_rows:
            for leg in operation.get("legs", []):
                try:
                    leg_amount = Decimal(str(leg.get("amount", "0")))
                except InvalidOperation:
                    continue

                asset = str(leg.get("asset") or "")
                mint = str(leg.get("mint") or "")
                match_quality = target_match_quality(asset, mint, leg_amount)
                if match_quality is None:
                    continue

                direction = str(leg.get("direction") or "")
                role = {
                    "sent": "Montant envoyé",
                    "received": "Montant reçu",
                    "transfer": "Montant transféré",
                }.get(direction, "Montant correspondant")

                amount_text = _format_decimal(abs(leg_amount))
                matches_rows.append(
                    {
                        "match_type": operation["operation_type"],
                        "match_role": role,
                        "match_quality": match_quality,
                        "block": operation["block"],
                        "block_time_utc": operation["block_time_utc"],
                        "signature": operation["signature"],
                        "matched_amount": amount_text,
                        "matched_asset": asset,
                        "sent": operation["sent"],
                        "received": operation["received"],
                        "source": operation["source"],
                        "destination": operation["destination"],
                        "account": operation["account"],
                        "detail": operation["detail"],
                        "explorer": operation["explorer"],
                        "solscan": operation["solscan"],
                    }
                )
                identity_asset = (
                    "SOL"
                    if asset in {"SOL", "WSOL"} or mint == WSOL_MINT
                    else (mint or asset)
                )
                matched_signature_asset_amount.add(
                    (operation["signature"], identity_asset, amount_text)
                )

        # Les swaps passent souvent par des inner instructions. Le montant
        # communiqué peut donc exister exactement dans un transfert interne,
        # même si le delta économique net diffère légèrement (frais, rent,
        # wrapping, routage multi-hop). On n'utilise cette preuve comme
        # résultat de swap que si la transaction est déjà reconnue comme swap.
        for evidence in transfer_evidence_rows:
            try:
                evidence_amount = Decimal(str(evidence["amount"]))
            except InvalidOperation:
                continue

            asset = str(evidence.get("asset") or "")
            mint = str(evidence.get("mint") or "")
            match_quality = target_match_quality(asset, mint, evidence_amount)
            if match_quality is None:
                continue

            amount_text = _format_decimal(abs(evidence_amount))
            identity_asset = (
                "SOL"
                if asset in {"SOL", "WSOL"} or mint == WSOL_MINT
                else (mint or asset)
            )
            dedupe_key = (
                evidence["signature"],
                identity_asset,
                amount_text,
            )
            if dedupe_key in matched_signature_asset_amount:
                continue

            swap_operations = [
                operation
                for operation in operations_by_signature.get(
                    evidence["signature"], []
                )
                if operation["operation_type"] == "swap_probable"
            ]

            if not swap_operations:
                continue

            swap = swap_operations[0]
            matches_rows.append(
                {
                    "match_type": "swap_probable",
                    "match_role": (
                        "Montant exact observé dans le swap"
                        if match_quality == "exact"
                        else "Montant approché observé dans le swap"
                    ),
                    "match_quality": match_quality,
                    "block": evidence["block"],
                    "block_time_utc": evidence["block_time_utc"],
                    "signature": evidence["signature"],
                    "matched_amount": amount_text,
                    "matched_asset": (
                        "SOL"
                        if asset == "WSOL" or mint == WSOL_MINT
                        else asset
                    ),
                    "sent": swap["sent"],
                    "received": swap["received"],
                    "source": evidence["source"],
                    "destination": evidence["destination"],
                    "account": swap["account"],
                    "detail": (
                        (
                            "Correspondance exacte"
                            if match_quality == "exact"
                            else "Correspondance approchée"
                        )
                        + " dans une instruction interne du swap "
                        + f"({evidence['location']})"
                    ),
                    "explorer": evidence["explorer"],
                    "solscan": evidence["solscan"],
                }
            )
            matched_signature_asset_amount.add(dedupe_key)

        # Certains DEX/agrégateurs encodent le montant demandé directement
        # dans les données brutes de l'instruction du swap. Il peut alors ne
        # correspondre ni à un transfert parsé ni au delta net final.
        for evidence in raw_amount_evidence_rows:
            swap_operations = [
                operation
                for operation in operations_by_signature.get(
                    evidence["signature"], []
                )
                if operation["operation_type"] == "swap_probable"
            ]
            if not swap_operations:
                continue

            amount_text = _format_decimal(target_amount)
            identity_asset = (
                "SOL"
                if target_asset in {"SOL", "WSOL"}
                else (target_mint or target_asset)
            )
            dedupe_key = (
                evidence["signature"],
                identity_asset,
                amount_text,
            )
            if dedupe_key in matched_signature_asset_amount:
                continue

            swap = swap_operations[0]
            matches_rows.append(
                {
                    "match_type": "swap_probable",
                    "match_role": "Montant exact encodé dans le swap",
                    "match_quality": "exact",
                    "block": evidence["block"],
                    "block_time_utc": evidence["block_time_utc"],
                    "signature": evidence["signature"],
                    "matched_amount": amount_text,
                    "matched_asset": target_asset,
                    "sent": swap["sent"],
                    "received": swap["received"],
                    "source": "",
                    "destination": "",
                    "account": swap["account"],
                    "detail": (
                        "Montant communiqué trouvé tel quel dans les données "
                        f"brutes de l'instruction {evidence['location']}"
                        + (
                            f" · programme {evidence['program_id']}"
                            if evidence["program_id"]
                            else ""
                        )
                    ),
                    "explorer": evidence["explorer"],
                    "solscan": evidence["solscan"],
                }
            )
            matched_signature_asset_amount.add(dedupe_key)

        for movement in movements_rows:
            try:
                movement_amount = Decimal(str(movement["absolute_delta_amount"]))
            except InvalidOperation:
                continue

            asset = str(movement.get("asset") or "")
            mint = str(movement.get("mint") or "")
            match_quality = target_match_quality(asset, mint, movement_amount)
            if match_quality is None:
                continue

            amount_text = _format_decimal(abs(movement_amount))
            identity_asset = (
                "SOL"
                if asset in {"SOL", "WSOL"} or mint == WSOL_MINT
                else (mint or asset)
            )
            dedupe_key = (
                movement["signature"],
                identity_asset,
                amount_text,
            )
            if dedupe_key in matched_signature_asset_amount:
                continue

            matches_rows.append(
                {
                    "match_type": "balance_delta",
                    "match_role": (
                        "Variation de solde exacte"
                        if match_quality == "exact"
                        else "Variation de solde approchée"
                    ),
                    "match_quality": match_quality,
                    "block": movement["block"],
                    "block_time_utc": movement["block_time_utc"],
                    "signature": movement["signature"],
                    "matched_amount": amount_text,
                    "matched_asset": asset,
                    "sent": "",
                    "received": "",
                    "source": "",
                    "destination": "",
                    "account": movement["account"],
                    "detail": f"Variation de solde {movement['delta_amount']} {asset}",
                    "explorer": movement["explorer"],
                    "solscan": movement["solscan"],
                }
            )
            matched_signature_asset_amount.add(dedupe_key)

    # Déduplication finale des correspondances.
    unique_matches: list[dict[str, Any]] = []
    seen_matches: set[tuple[Any, ...]] = set()

    for row in matches_rows:
        key = (
            row["match_type"],
            row["match_role"],
            row["signature"],
            row["matched_amount"],
            row["matched_asset"],
            row["source"],
            row["destination"],
            row["account"],
        )
        if key in seen_matches:
            continue
        seen_matches.add(key)
        unique_matches.append(row)

    _notify(status_callback, "Recherche terminée.")

    return {
        "network": "Solana",
        "center_dt": center_dt,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "tolerance_seconds": tolerance_seconds,
        "target_amount": target_amount,
        "target_asset": target_asset,
        "target_amount_precision": (
            target_criterion.decimal_places
            if target_criterion is not None
            else None
        ),
        "target_amount_tolerance": (
            target_criterion.tolerance
            if target_criterion is not None
            else None
        ),
        "target_mint": target_mint,
        # Compatibilité avec les premières versions de l'interface.
        "target_sol": target_amount if target_asset == "SOL" else None,
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
        "operations": operations_rows,
        "movements": movements_rows,
        "matches": unique_matches,
    }