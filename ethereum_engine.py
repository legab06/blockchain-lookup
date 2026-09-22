from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from amount_matching import AmountCriterion, parse_amount_criterion

DEFAULT_RPC_URL = "https://ethereum-rpc.publicnode.com"
DEFAULT_RPC_DELAY = 0.03
WEI_PER_ETH = Decimal("1000000000000000000")
BOUNDARY_BLOCK_MARGIN = 1
RECEIPT_BATCH_SIZE = 80

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)

KNOWN_ERC20 = {
    "USDC": {
        "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "decimals": 6,
    },
    "USDT": {
        "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "decimals": 6,
    },
    "WETH": {
        "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "decimals": 18,
    },
    "DAI": {
        "address": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "decimals": 18,
    },
    "WBTC": {
        "address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        "decimals": 8,
    },
}
TOKEN_BY_ADDRESS = {
    data["address"].lower(): {
        "symbol": symbol,
        "decimals": int(data["decimals"]),
    }
    for symbol, data in KNOWN_ERC20.items()
}

ProgressCallback = Callable[[int, int, int], None]
StatusCallback = Callable[[str], None]


class EthereumSearchError(RuntimeError):
    """Erreur lisible par l'interface lors d'une recherche Ethereum."""


class _PrunedHistoryUnavailable(RuntimeError):
    """Le backend RPC a élagué les blocs antérieurs à une certaine hauteur."""

    def __init__(self, earliest_block: int, message: str):
        super().__init__(message)
        self.earliest_block = earliest_block


def _notify(callback: StatusCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _hex_to_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        if isinstance(value, int):
            return value
        return int(str(value), 16)
    except (TypeError, ValueError):
        return default


def _normalize_address(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return raw if raw.startswith("0x") else f"0x{raw}"


def _topic_address(topic: Any) -> str:
    raw = str(topic or "").lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) < 40:
        return ""
    return "0x" + raw[-40:]


def _parse_amount(
    value: str | Decimal | None,
    asset: str,
) -> Decimal | None:
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

    decimals = 18 if asset == "ETH" else KNOWN_ERC20.get(asset, {}).get("decimals")
    if decimals is not None:
        base_units = amount * (Decimal(10) ** int(decimals))
        if base_units != base_units.to_integral_value():
            raise ValueError(
                f"Un montant {asset} ne peut pas dépasser {int(decimals)} décimales."
            )

    return amount


def _target_raw_units(asset: str, amount: Decimal | None) -> int | None:
    if amount is None:
        return None

    if asset == "ETH":
        decimals = 18
    else:
        decimals = KNOWN_ERC20.get(asset, {}).get("decimals")

    if decimals is None:
        return None

    raw = amount * (Decimal(10) ** int(decimals))
    if raw != raw.to_integral_value():
        return None
    return int(raw)


def _target_token_address(asset: str) -> str:
    if asset == "ETH":
        return ""
    data = KNOWN_ERC20.get(asset)
    if not data:
        return ""
    return str(data["address"]).lower()


def _target_match_quality(
    *,
    target_asset: str,
    target_criterion: AmountCriterion | None,
    target_token: str,
    asset: str,
    token_address: str,
    amount: Decimal,
) -> str | None:
    if target_criterion is None:
        return None

    normalized_asset = str(asset or "").upper()
    normalized_token = str(token_address or "").lower()

    if target_asset == "ETH":
        if normalized_asset not in {"ETH", "WETH"}:
            return None
    elif target_token:
        if normalized_token != target_token:
            return None
    elif normalized_asset != target_asset:
        return None

    return target_criterion.classify(amount)


def _target_matches(
    *,
    target_asset: str,
    target_criterion: AmountCriterion | None,
    target_token: str,
    asset: str,
    token_address: str,
    amount: Decimal,
) -> bool:
    return _target_match_quality(
        target_asset=target_asset,
        target_criterion=target_criterion,
        target_token=target_token,
        asset=asset,
        token_address=token_address,
        amount=amount,
    ) is not None


def _input_contains_raw_amount(input_data: Any, raw_amount: int | None) -> bool:
    if raw_amount is None or raw_amount < 0:
        return False

    raw = str(input_data or "").lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if not raw:
        return False

    needle = f"{raw_amount:064x}"
    return needle in raw


def _explorer_links(tx_hash: str) -> tuple[str, str]:
    return (
        f"https://etherscan.io/tx/{tx_hash}",
        f"https://eth.blockscout.com/tx/{tx_hash}",
    )


def search_ethereum_window(
    search_date: date,
    search_time: dt_time,
    tolerance_seconds: int = 30,
    amount_eth: str | Decimal | None = None,
    *,
    asset_symbol: str = "ETH",
    rpc_url: str = DEFAULT_RPC_URL,
    rpc_delay: float = DEFAULT_RPC_DELAY,
    retries: int = 8,
    progress_callback: ProgressCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """
    Recherche les transactions Ethereum mainnet situées dans une fenêtre UTC.

    Le moteur s'appuie uniquement sur le JSON-RPC standard :
    - blocs complets pour les transactions,
    - reçus pour le statut, les frais et les logs ERC-20.

    Les transferts ETH internes à un contrat ne sont pas visibles sans API de
    trace. Les montants encodés dans le calldata d'un swap déjà identifié sont
    néanmoins utilisés comme preuve complémentaire.
    """

    if tolerance_seconds < 0:
        raise ValueError("La tolérance doit être positive ou nulle.")

    target_asset = str(asset_symbol or "ETH").strip().upper()
    if target_asset not in {"ETH", "USDC", "USDT"}:
        raise ValueError("Actif Ethereum non pris en charge.")

    target_max_decimals = (
        18
        if target_asset == "ETH"
        else int(KNOWN_ERC20[target_asset]["decimals"])
    )
    target_criterion: AmountCriterion | None = parse_amount_criterion(
        amount_eth,
        max_decimals=target_max_decimals,
    )
    target_amount = (
        target_criterion.amount
        if target_criterion is not None
        else None
    )
    target_token = _target_token_address(target_asset)
    target_raw_units = _target_raw_units(target_asset, target_amount)

    center_dt = datetime.combine(search_date, search_time).replace(tzinfo=timezone.utc)
    start_dt = center_dt - timedelta(seconds=tolerance_seconds)
    end_dt = center_dt + timedelta(seconds=tolerance_seconds)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    request_id = 0

    def rpc(method: str, params: list | None = None) -> Any:
        nonlocal request_id
        request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or [],
        }
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(retries):
            try:
                request = urllib.request.Request(
                    rpc_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "BlockchainLookupStreamlit/3.0",
                    },
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    result = json.loads(response.read().decode("utf-8"))

                if "error" in result:
                    error = result["error"]
                    if isinstance(error, dict):
                        message = str(error.get("message") or error)
                        earliest_match = re.search(
                            r"earliest available\s+(\d+)",
                            message,
                            flags=re.IGNORECASE,
                        )
                        if (
                            error.get("code") == 4444
                            or "pruned history unavailable" in message.casefold()
                        ) and earliest_match:
                            raise _PrunedHistoryUnavailable(
                                int(earliest_match.group(1)),
                                message,
                            )
                    raise RuntimeError(error)

                if rpc_delay:
                    time.sleep(rpc_delay)
                return result.get("result")

            except _PrunedHistoryUnavailable:
                raise

            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    wait = 2 + attempt * 2
                    _notify(
                        status_callback,
                        "Limite du RPC Ethereum atteinte (429) — "
                        f"nouvelle tentative dans {wait}s…",
                    )
                    time.sleep(wait)
                    continue
                raise EthereumSearchError(
                    f"Erreur HTTP RPC Ethereum : {exc}"
                ) from exc

            except Exception as exc:
                if attempt == retries - 1:
                    raise EthereumSearchError(
                        f"Impossible de joindre le RPC Ethereum : {exc}"
                    ) from exc
                wait = 1 + attempt
                _notify(
                    status_callback,
                    f"Erreur RPC Ethereum ({exc}) — "
                    f"tentative {attempt + 1}/{retries}, reprise dans {wait}s…",
                )
                time.sleep(wait)

        raise EthereumSearchError("Impossible de joindre le RPC Ethereum.")

    def rpc_batch(calls: list[tuple[str, list]]) -> list[Any]:
        nonlocal request_id
        if not calls:
            return []

        payload: list[dict[str, Any]] = []
        ids: list[int] = []
        for method, params in calls:
            request_id += 1
            ids.append(request_id)
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )

        data = json.dumps(payload).encode("utf-8")

        for attempt in range(retries):
            try:
                request = urllib.request.Request(
                    rpc_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "BlockchainLookupStreamlit/3.0",
                    },
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = json.loads(response.read().decode("utf-8"))

                if not isinstance(raw, list):
                    raise RuntimeError("Réponse batch Ethereum invalide.")

                by_id: dict[int, Any] = {}
                for item in raw:
                    item_id = item.get("id")
                    if "error" in item:
                        by_id[item_id] = None
                    else:
                        by_id[item_id] = item.get("result")

                if rpc_delay:
                    time.sleep(rpc_delay)

                return [by_id.get(item_id) for item_id in ids]

            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    wait = 2 + attempt * 2
                    _notify(
                        status_callback,
                        "Limite du RPC Ethereum atteinte pendant les reçus (429) — "
                        f"reprise dans {wait}s…",
                    )
                    time.sleep(wait)
                    continue
                raise EthereumSearchError(
                    f"Erreur HTTP RPC Ethereum : {exc}"
                ) from exc

            except Exception as exc:
                if attempt == retries - 1:
                    raise EthereumSearchError(
                        f"Impossible de récupérer les reçus Ethereum : {exc}"
                    ) from exc
                time.sleep(1 + attempt)

        return [None] * len(calls)

    def get_block(block_number: int, *, full_transactions: bool) -> dict[str, Any] | None:
        return rpc(
            "eth_getBlockByNumber",
            [hex(block_number), bool(full_transactions)],
        )

    def block_timestamp(block_number: int) -> int:
        block = get_block(block_number, full_transactions=False)
        if not block:
            raise EthereumSearchError(
                f"Le bloc Ethereum {block_number} est introuvable."
            )
        timestamp = _hex_to_int(block.get("timestamp"), -1)
        if timestamp < 0:
            raise EthereumSearchError(
                f"Le bloc Ethereum {block_number} n'a pas de timestamp."
            )
        return timestamp

    _notify(status_callback, "Lecture de la hauteur Ethereum actuelle…")
    latest_block = _hex_to_int(rpc("eth_blockNumber"), -1)
    if latest_block < 0:
        raise EthereumSearchError("Impossible de déterminer le dernier bloc Ethereum.")

    latest_ts = block_timestamp(latest_block)
    if start_ts > latest_ts:
        latest_dt = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
        raise EthereumSearchError(
            "La fenêtre demandée est postérieure au dernier bloc Ethereum disponible "
            f"({latest_dt.strftime('%d/%m/%Y %H:%M:%S UTC')})."
        )

    minimum_available_block = 0
    try:
        earliest_available_ts = block_timestamp(0)
    except _PrunedHistoryUnavailable as exc:
        minimum_available_block = exc.earliest_block
        earliest_available_ts = block_timestamp(minimum_available_block)
        earliest_dt = datetime.fromtimestamp(
            earliest_available_ts,
            tz=timezone.utc,
        )
        _notify(
            status_callback,
            "Le RPC Ethereum utilisé est pruné : historique disponible à partir "
            f"du bloc {minimum_available_block} "
            f"({earliest_dt.strftime('%d/%m/%Y %H:%M:%S UTC')}).",
        )
    else:
        earliest_dt = datetime.fromtimestamp(
            earliest_available_ts,
            tz=timezone.utc,
        )

    if end_ts < earliest_available_ts:
        raise EthereumSearchError(
            "Le RPC Ethereum public utilisé ne conserve pas l'historique assez ancien. "
            f"Premier bloc disponible : {minimum_available_block} "
            f"({earliest_dt.strftime('%d/%m/%Y %H:%M:%S UTC')}). "
            "Cette recherche nécessite un RPC Ethereum avec historique archive."
        )

    def first_block_at_or_after(target_ts: int) -> int:
        low = minimum_available_block
        high = latest_block
        answer = latest_block

        while low <= high:
            midpoint = (low + high) // 2
            timestamp = block_timestamp(midpoint)
            if timestamp >= target_ts:
                answer = midpoint
                high = midpoint - 1
            else:
                low = midpoint + 1
        return answer

    def last_block_at_or_before(target_ts: int) -> int:
        low = minimum_available_block
        high = latest_block
        answer = minimum_available_block

        while low <= high:
            midpoint = (low + high) // 2
            timestamp = block_timestamp(midpoint)
            if timestamp <= target_ts:
                answer = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        return answer

    _notify(status_callback, "Recherche de la borne de début Ethereum…")
    start_block = first_block_at_or_after(start_ts)

    _notify(status_callback, "Recherche de la borne de fin Ethereum…")
    end_block = last_block_at_or_before(end_ts)

    query_start_block = max(
        minimum_available_block,
        min(start_block, end_block) - BOUNDARY_BLOCK_MARGIN,
    )
    query_end_block = min(
        latest_block,
        max(start_block, end_block) + BOUNDARY_BLOCK_MARGIN,
    )

    candidate_blocks = query_end_block - query_start_block + 1
    if candidate_blocks > 800:
        raise EthereumSearchError(
            "La fenêtre couvre trop de blocs Ethereum pour le RPC public. "
            "Réduisez la tolérance à moins d'environ 80 minutes."
        )

    transactions_rows: list[dict[str, Any]] = []
    operations_rows: list[dict[str, Any]] = []
    movements_rows: list[dict[str, Any]] = []
    raw_amount_evidence_rows: list[dict[str, Any]] = []
    direct_native_match_rows: list[dict[str, Any]] = []

    analyzed_blocks = 0
    skipped_blocks = 0

    def append_operation(
        *,
        operation_type: str,
        block_number: int,
        block_time: str,
        tx_hash: str,
        sent: str = "",
        received: str = "",
        source: str = "",
        destination: str = "",
        account: str = "",
        evidence: str = "",
        detail: str = "",
        legs: list[dict[str, str]] | None = None,
    ) -> None:
        explorer, secondary = _explorer_links(tx_hash)
        operations_rows.append(
            {
                "operation_type": operation_type,
                "block": block_number,
                "block_time_utc": block_time,
                "signature": tx_hash,
                "sent": sent,
                "received": received,
                "source": source,
                "destination": destination,
                "account": account,
                "evidence": evidence,
                "detail": detail,
                "legs": legs or [],
                "explorer": explorer,
                "secondary_explorer": secondary,
            }
        )

    def append_movement(
        *,
        block_number: int,
        block_time: str,
        tx_hash: str,
        account: str,
        asset: str,
        token_address: str,
        delta: Decimal,
        detail: str,
    ) -> None:
        if not account or delta == 0:
            return

        explorer, secondary = _explorer_links(tx_hash)
        movements_rows.append(
            {
                "block": block_number,
                "block_time_utc": block_time,
                "signature": tx_hash,
                "account": account,
                "asset": asset,
                "token_address": token_address,
                "balance_before": "",
                "balance_after": "",
                "delta_amount": (
                    ("+" if delta > 0 else "") + _format_decimal(delta)
                ),
                "absolute_delta_amount": _format_decimal(abs(delta)),
                "match_target": (
                    "OUI"
                    if _target_matches(
                        target_asset=target_asset,
                        target_criterion=target_criterion,
                        target_token=target_token,
                        asset=asset,
                        token_address=token_address,
                        amount=delta,
                    )
                    else ""
                ),
                "detail": detail,
                "explorer": explorer,
                "secondary_explorer": secondary,
            }
        )

    total_candidates = candidate_blocks
    _notify(
        status_callback,
        f"Analyse de {total_candidates} blocs Ethereum candidats…",
    )

    for position, block_number in enumerate(
        range(query_start_block, query_end_block + 1),
        start=1,
    ):
        if progress_callback:
            progress_callback(position - 1, total_candidates, block_number)

        block = get_block(block_number, full_transactions=True)
        if not block:
            skipped_blocks += 1
            continue

        block_ts = _hex_to_int(block.get("timestamp"), -1)
        if block_ts < 0:
            skipped_blocks += 1
            continue

        if not (start_ts <= block_ts <= end_ts):
            continue

        analyzed_blocks += 1
        block_time = datetime.fromtimestamp(
            block_ts,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        transactions = block.get("transactions") or []
        if not isinstance(transactions, list):
            transactions = []

        tx_hashes = [
            str(tx.get("hash") or "")
            for tx in transactions
            if isinstance(tx, dict) and tx.get("hash")
        ]

        receipts_by_hash: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(tx_hashes), RECEIPT_BATCH_SIZE):
            chunk = tx_hashes[offset : offset + RECEIPT_BATCH_SIZE]
            receipts = rpc_batch(
                [("eth_getTransactionReceipt", [tx_hash]) for tx_hash in chunk]
            )
            for tx_hash, receipt in zip(chunk, receipts):
                if isinstance(receipt, dict):
                    receipts_by_hash[tx_hash] = receipt

        for tx_index, tx in enumerate(transactions, start=1):
            if not isinstance(tx, dict):
                continue

            tx_hash = str(tx.get("hash") or "")
            sender = _normalize_address(tx.get("from"))
            destination = _normalize_address(tx.get("to"))
            value_wei = _hex_to_int(tx.get("value"), 0)
            value_eth = Decimal(value_wei) / WEI_PER_ETH
            input_data = str(tx.get("input") or "")

            receipt = receipts_by_hash.get(tx_hash)
            if receipt is None:
                receipt = rpc("eth_getTransactionReceipt", [tx_hash])
                if not isinstance(receipt, dict):
                    receipt = {}

            success = str(receipt.get("status") or "").lower() == "0x1"
            gas_used = _hex_to_int(receipt.get("gasUsed"), 0)
            effective_gas_price = _hex_to_int(
                receipt.get("effectiveGasPrice") or tx.get("gasPrice"),
                0,
            )
            fee_eth = (
                Decimal(gas_used * effective_gas_price) / WEI_PER_ETH
                if gas_used and effective_gas_price
                else Decimal(0)
            )

            explorer, secondary = _explorer_links(tx_hash)
            logs = receipt.get("logs") or []
            if not isinstance(logs, list):
                logs = []

            addresses: set[str] = {address for address in (sender, destination) if address}
            tx_asset_deltas: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

            if success and value_wei > 0:
                amount_text = _format_decimal(value_eth)

                native_match_quality = (
                    target_criterion.classify(value_eth)
                    if target_asset == "ETH" and target_criterion is not None
                    else None
                )
                if native_match_quality is not None:
                    direct_native_match_rows.append(
                        {
                            "match_type": "transfer",
                            "match_role": (
                                "Valeur native exacte"
                                if native_match_quality == "exact"
                                else "Valeur native approchée"
                            ),
                            "match_quality": native_match_quality,
                            "block": block_number,
                            "block_time_utc": block_time,
                            "signature": tx_hash,
                            "matched_amount": amount_text,
                            "matched_asset": "ETH",
                            "sent": f"{amount_text} ETH",
                            "received": "",
                            "source": sender,
                            "destination": destination,
                            "account": "",
                            "detail": (
                                (
                                    "Correspondance exacte"
                                    if native_match_quality == "exact"
                                    else "Correspondance approchée"
                                )
                                + " sur le champ value "
                                + f"({value_wei} wei)"
                            ),
                            "explorer": explorer,
                            "secondary_explorer": secondary,
                        }
                    )

                append_operation(
                    operation_type="transfer",
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    sent=f"{amount_text} ETH",
                    source=sender,
                    destination=destination,
                    evidence="Valeur native de la transaction",
                    detail="Transfert ETH / appel de contrat avec valeur",
                    legs=[
                        {
                            "direction": "transfer",
                            "asset": "ETH",
                            "token_address": "",
                            "amount": amount_text,
                        }
                    ],
                )
                append_movement(
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    account=sender,
                    asset="ETH",
                    token_address="",
                    delta=-value_eth,
                    detail="Valeur native envoyée",
                )
                append_movement(
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    account=destination,
                    asset="ETH",
                    token_address="",
                    delta=value_eth,
                    detail="Valeur native reçue",
                )
                if sender:
                    tx_asset_deltas[("ETH", "")] -= value_eth
                if destination and destination == sender:
                    tx_asset_deltas[("ETH", "")] += value_eth

            for log in logs:
                if not isinstance(log, dict):
                    continue

                topics = log.get("topics") or []
                if (
                    len(topics) < 3
                    or str(topics[0]).lower() != TRANSFER_TOPIC
                ):
                    continue

                token_address = _normalize_address(log.get("address"))
                token_meta = TOKEN_BY_ADDRESS.get(token_address)
                if not token_meta:
                    continue

                source = _topic_address(topics[1])
                target = _topic_address(topics[2])
                raw_amount = _hex_to_int(log.get("data"), 0)
                decimals = int(token_meta["decimals"])
                token_amount = Decimal(raw_amount) / (Decimal(10) ** decimals)
                asset = str(token_meta["symbol"])
                amount_text = _format_decimal(token_amount)

                addresses.update(address for address in (source, target) if address)

                append_operation(
                    operation_type="token_transfer",
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    sent=f"{amount_text} {asset}",
                    source=source,
                    destination=target,
                    evidence="Log ERC-20 Transfer",
                    detail=f"Contrat {token_address}",
                    legs=[
                        {
                            "direction": "transfer",
                            "asset": asset,
                            "token_address": token_address,
                            "amount": amount_text,
                        }
                    ],
                )
                append_movement(
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    account=source,
                    asset=asset,
                    token_address=token_address,
                    delta=-token_amount,
                    detail="Transfert ERC-20",
                )
                append_movement(
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    account=target,
                    asset=asset,
                    token_address=token_address,
                    delta=token_amount,
                    detail="Transfert ERC-20",
                )

                economic_asset = "ETH" if asset == "WETH" else asset
                economic_token = "" if asset == "WETH" else token_address
                if source == sender:
                    tx_asset_deltas[(economic_asset, economic_token)] -= token_amount
                if target == sender:
                    tx_asset_deltas[(economic_asset, economic_token)] += token_amount

            normalized_deltas = [
                (asset, token_address, delta)
                for (asset, token_address), delta in tx_asset_deltas.items()
                if delta
            ]
            negatives = [item for item in normalized_deltas if item[2] < 0]
            positives = [item for item in normalized_deltas if item[2] > 0]
            distinct_assets = {
                (asset, token_address)
                for asset, token_address, _delta in normalized_deltas
            }

            if success and negatives and positives and len(distinct_assets) >= 2:
                sent_parts = [
                    f"{_format_decimal(abs(delta))} {asset}"
                    for asset, _token, delta in sorted(
                        negatives,
                        key=lambda item: abs(item[2]),
                        reverse=True,
                    )
                ]
                received_parts = [
                    f"{_format_decimal(delta)} {asset}"
                    for asset, _token, delta in sorted(
                        positives,
                        key=lambda item: abs(item[2]),
                        reverse=True,
                    )
                ]

                legs: list[dict[str, str]] = []
                for asset, token_address, delta in negatives:
                    legs.append(
                        {
                            "direction": "sent",
                            "asset": asset,
                            "token_address": token_address,
                            "amount": _format_decimal(abs(delta)),
                        }
                    )
                for asset, token_address, delta in positives:
                    legs.append(
                        {
                            "direction": "received",
                            "asset": asset,
                            "token_address": token_address,
                            "amount": _format_decimal(delta),
                        }
                    )

                append_operation(
                    operation_type="swap_probable",
                    block_number=block_number,
                    block_time=block_time,
                    tx_hash=tx_hash,
                    sent=" + ".join(sent_parts),
                    received=" + ".join(received_parts),
                    account=sender,
                    evidence="Mouvements nets de plusieurs actifs",
                    detail="Échange déduit des transferts visibles dans la transaction",
                    legs=legs,
                )

                if _input_contains_raw_amount(input_data, target_raw_units):
                    raw_amount_evidence_rows.append(
                        {
                            "block": block_number,
                            "block_time_utc": block_time,
                            "signature": tx_hash,
                            "account": sender,
                            "explorer": explorer,
                            "secondary_explorer": secondary,
                        }
                    )

            transactions_rows.append(
                {
                    "block": block_number,
                    "block_time_utc": block_time,
                    "transaction_index": tx_index,
                    "signature": tx_hash,
                    "status": "SUCCESS" if success else "FAILED",
                    "fee_eth": _format_decimal(fee_eth),
                    "account_count": len(addresses),
                    "accounts": " | ".join(sorted(addresses)),
                    "error": "" if success else "Exécution EVM échouée",
                    "explorer": explorer,
                    "secondary_explorer": secondary,
                }
            )

    if progress_callback:
        progress_callback(total_candidates, total_candidates, query_end_block)

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

    operations_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in operations_rows:
        operations_by_signature[operation["signature"]].append(operation)

    for transaction_row in transactions_rows:
        tx_operations = operations_by_signature.get(transaction_row["signature"], [])
        summaries: list[str] = []

        swaps = [
            operation
            for operation in tx_operations
            if operation["operation_type"] == "swap_probable"
        ]
        others = [
            operation
            for operation in tx_operations
            if operation["operation_type"] != "swap_probable"
        ]

        for operation in swaps + others:
            if operation["operation_type"] == "swap_probable":
                summary = (
                    f"Swap probable : {operation['sent']} → "
                    f"{operation['received']}"
                )
            else:
                summary = operation["sent"] or operation["received"] or "Opération"

            if summary not in summaries:
                summaries.append(summary)
            if len(summaries) >= 4:
                break

        transaction_row["operation_count"] = len(tx_operations)
        transaction_row["operation_summary"] = " | ".join(summaries)

    matches_rows: list[dict[str, Any]] = []
    matched_signature_asset_amount: set[tuple[str, str, str]] = set()

    if target_amount is not None:
        for row in direct_native_match_rows:
            matches_rows.append(row)
            matched_signature_asset_amount.add(
                (row["signature"], "ETH", row["matched_amount"])
            )

        for operation in operations_rows:
            for leg in operation.get("legs", []):
                try:
                    leg_amount = Decimal(str(leg.get("amount", "0")))
                except InvalidOperation:
                    continue

                asset = str(leg.get("asset") or "")
                token_address = str(leg.get("token_address") or "").lower()
                match_quality = _target_match_quality(
                    target_asset=target_asset,
                    target_criterion=target_criterion,
                    target_token=target_token,
                    asset=asset,
                    token_address=token_address,
                    amount=leg_amount,
                )
                if match_quality is None:
                    continue

                direction = str(leg.get("direction") or "")
                role = {
                    "sent": "Montant envoyé",
                    "received": "Montant reçu",
                    "transfer": "Montant transféré",
                }.get(direction, "Montant correspondant")

                display_asset = "ETH" if asset == "WETH" and target_asset == "ETH" else asset
                amount_text = _format_decimal(abs(leg_amount))
                identity_asset = (
                    "ETH"
                    if asset in {"ETH", "WETH"}
                    else (token_address or asset)
                )
                dedupe_key = (
                    operation["signature"],
                    identity_asset,
                    amount_text,
                )
                if dedupe_key in matched_signature_asset_amount:
                    continue

                matches_rows.append(
                    {
                        "match_type": operation["operation_type"],
                        "match_role": role,
                        "match_quality": match_quality,
                        "block": operation["block"],
                        "block_time_utc": operation["block_time_utc"],
                        "signature": operation["signature"],
                        "matched_amount": amount_text,
                        "matched_asset": display_asset,
                        "sent": operation["sent"],
                        "received": operation["received"],
                        "source": operation["source"],
                        "destination": operation["destination"],
                        "account": operation["account"],
                        "detail": operation["detail"],
                        "explorer": operation["explorer"],
                        "secondary_explorer": operation["secondary_explorer"],
                    }
                )

                matched_signature_asset_amount.add(dedupe_key)

        for evidence in raw_amount_evidence_rows:
            amount_text = _format_decimal(target_amount)
            identity_asset = (
                "ETH"
                if target_asset == "ETH"
                else (target_token or target_asset)
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
                    "detail": "Montant communiqué trouvé dans le calldata du swap",
                    "explorer": evidence["explorer"],
                    "secondary_explorer": evidence["secondary_explorer"],
                }
            )
            matched_signature_asset_amount.add(dedupe_key)

        for movement in movements_rows:
            try:
                movement_amount = Decimal(str(movement["absolute_delta_amount"]))
            except InvalidOperation:
                continue

            asset = str(movement.get("asset") or "")
            token_address = str(movement.get("token_address") or "").lower()
            match_quality = _target_match_quality(
                target_asset=target_asset,
                target_criterion=target_criterion,
                target_token=target_token,
                asset=asset,
                token_address=token_address,
                amount=movement_amount,
            )
            if match_quality is None:
                continue

            amount_text = _format_decimal(movement_amount)
            identity_asset = "ETH" if asset in {"ETH", "WETH"} else (token_address or asset)
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
                        "Mouvement exact"
                        if match_quality == "exact"
                        else "Mouvement approché"
                    ),
                    "match_quality": match_quality,
                    "block": movement["block"],
                    "block_time_utc": movement["block_time_utc"],
                    "signature": movement["signature"],
                    "matched_amount": amount_text,
                    "matched_asset": "ETH" if asset == "WETH" and target_asset == "ETH" else asset,
                    "sent": "",
                    "received": "",
                    "source": "",
                    "destination": "",
                    "account": movement["account"],
                    "detail": movement["detail"],
                    "explorer": movement["explorer"],
                    "secondary_explorer": movement["secondary_explorer"],
                }
            )
            matched_signature_asset_amount.add(dedupe_key)

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

    _notify(status_callback, "Recherche Ethereum terminée.")

    return {
        "network": "Ethereum",
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
        "target_token": target_token,
        "start_block": start_block,
        "end_block": end_block,
        "query_start_block": query_start_block,
        "query_end_block": query_end_block,
        "candidate_blocks": total_candidates,
        "analyzed_blocks": analyzed_blocks,
        "skipped_blocks": skipped_blocks,
        "transactions": transactions_rows,
        "operations": operations_rows,
        "movements": movements_rows,
        "matches": unique_matches,
    }
