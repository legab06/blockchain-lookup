from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from amount_matching import AmountCriterion, parse_amount_criterion

DEFAULT_API_URL = "https://mempool.space/api"
DEFAULT_API_DELAY = 0.03
SATOSHIS_PER_BTC = Decimal("100000000")
MAX_CANDIDATE_BLOCKS = 24
BOUNDARY_BLOCK_MARGIN = 2

ProgressCallback = Callable[[int, int, int], None]
StatusCallback = Callable[[str], None]


class BitcoinSearchError(RuntimeError):
    """Erreur lisible par l'interface lors d'une recherche Bitcoin."""


def _notify(callback: StatusCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _sha256d(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("CompactSize tronqué.")

    prefix = data[offset]
    offset += 1

    if prefix < 0xFD:
        return prefix, offset
    if prefix == 0xFD:
        size = 2
    elif prefix == 0xFE:
        size = 4
    else:
        size = 8

    end = offset + size
    if end > len(data):
        raise ValueError("CompactSize tronqué.")
    return int.from_bytes(data[offset:end], "little"), end


def _require(data: bytes, offset: int, size: int) -> int:
    end = offset + size
    if end > len(data):
        raise ValueError("Transaction Bitcoin tronquée.")
    return end


def _base58check(version: int, payload: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    raw = bytes([version]) + payload
    raw += _sha256d(raw)[:4]
    number = int.from_bytes(raw, "big")

    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded

    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or "")


def _bech32_polymod(values: list[int]) -> int:
    generators = [
        0x3B6A57B2,
        0x26508E6D,
        0x1EA119FA,
        0x3D4233DD,
        0x2A1462B3,
    ]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return (
        [ord(char) >> 5 for char in hrp]
        + [0]
        + [ord(char) & 31 for char in hrp]
    )


def _convert_bits(
    data: bytes,
    from_bits: int,
    to_bits: int,
    *,
    pad: bool,
) -> list[int] | None:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1

    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (
            (accumulator << from_bits) | value
        ) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)

    if pad:
        if bits:
            result.append(
                (accumulator << (to_bits - bits)) & max_value
            )
    elif (
        bits >= from_bits
        or ((accumulator << (to_bits - bits)) & max_value)
    ):
        return None

    return result


def _encode_segwit_address(
    witness_version: int,
    program: bytes,
) -> str:
    converted = _convert_bits(program, 8, 5, pad=True)
    if converted is None:
        return ""

    data = [witness_version] + converted
    constant = (
        1 if witness_version == 0 else 0x2BC830A3
    )
    values = _bech32_hrp_expand("bc") + data + [0] * 6
    polymod = _bech32_polymod(values) ^ constant
    checksum = [
        (polymod >> (5 * (5 - index))) & 31
        for index in range(6)
    ]
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    return "bc1" + "".join(
        charset[value] for value in data + checksum
    )


def _decode_output_destination(
    script: bytes,
) -> tuple[str, str]:
    if (
        len(script) == 25
        and script[:3] == b"\x76\xa9\x14"
        and script[-2:] == b"\x88\xac"
    ):
        return _base58check(0x00, script[3:23]), "P2PKH"

    if (
        len(script) == 23
        and script[:2] == b"\xa9\x14"
        and script[-1:] == b"\x87"
    ):
        return _base58check(0x05, script[2:22]), "P2SH"

    if len(script) >= 4:
        opcode = script[0]
        program_length = script[1]
        if (
            program_length == len(script) - 2
            and 2 <= program_length <= 40
        ):
            if opcode == 0x00:
                witness_version = 0
            elif 0x51 <= opcode <= 0x60:
                witness_version = opcode - 0x50
            else:
                witness_version = -1

            if 0 <= witness_version <= 16:
                address = _encode_segwit_address(
                    witness_version,
                    script[2:],
                )
                if (
                    witness_version == 0
                    and program_length == 20
                ):
                    script_type = "P2WPKH"
                elif (
                    witness_version == 0
                    and program_length == 32
                ):
                    script_type = "P2WSH"
                elif (
                    witness_version == 1
                    and program_length == 32
                ):
                    script_type = "P2TR"
                else:
                    script_type = (
                        f"Witness v{witness_version}"
                    )
                return address, script_type

    if script[:1] == b"\x6a":
        return "OP_RETURN", "OP_RETURN"

    return "", "Script non standard"


def _parse_transaction(
    data: bytes,
    offset: int,
) -> tuple[dict[str, Any], int]:
    start = offset
    version_end = _require(data, offset, 4)
    version_bytes = data[offset:version_end]
    version = int.from_bytes(
        version_bytes,
        "little",
        signed=True,
    )
    offset = version_end

    segwit = (
        offset + 1 < len(data)
        and data[offset] == 0
        and data[offset + 1] != 0
    )
    if segwit:
        offset += 2

    stripped_parts = [version_bytes]

    vin_count_start = offset
    vin_count, offset = _read_varint(data, offset)
    stripped_parts.append(data[vin_count_start:offset])

    inputs: list[dict[str, Any]] = []
    for _ in range(vin_count):
        input_start = offset

        prev_hash_end = _require(data, offset, 32)
        prev_hash_raw = data[offset:prev_hash_end]
        offset = prev_hash_end

        prev_index_end = _require(data, offset, 4)
        prev_index = int.from_bytes(
            data[offset:prev_index_end],
            "little",
        )
        offset = prev_index_end

        script_length, offset = _read_varint(
            data,
            offset,
        )
        script_end = _require(
            data,
            offset,
            script_length,
        )
        offset = script_end

        sequence_end = _require(data, offset, 4)
        sequence = int.from_bytes(
            data[offset:sequence_end],
            "little",
        )
        offset = sequence_end

        stripped_parts.append(data[input_start:offset])
        inputs.append(
            {
                "txid": prev_hash_raw[::-1].hex(),
                "vout": prev_index,
                "sequence": sequence,
                "coinbase": (
                    prev_hash_raw == b"\x00" * 32
                    and prev_index == 0xFFFFFFFF
                ),
            }
        )

    vout_count_start = offset
    vout_count, offset = _read_varint(data, offset)
    stripped_parts.append(data[vout_count_start:offset])

    outputs: list[dict[str, Any]] = []
    for output_index in range(vout_count):
        output_start = offset

        value_end = _require(data, offset, 8)
        value_sats = int.from_bytes(
            data[offset:value_end],
            "little",
        )
        offset = value_end

        script_length, offset = _read_varint(
            data,
            offset,
        )
        script_end = _require(
            data,
            offset,
            script_length,
        )
        script = data[offset:script_end]
        offset = script_end

        stripped_parts.append(data[output_start:offset])
        destination, script_type = (
            _decode_output_destination(script)
        )
        outputs.append(
            {
                "index": output_index,
                "value_sats": value_sats,
                "script": script.hex(),
                "destination": destination,
                "script_type": script_type,
            }
        )

    if segwit:
        for _ in range(vin_count):
            item_count, offset = _read_varint(
                data,
                offset,
            )
            for _ in range(item_count):
                item_length, offset = _read_varint(
                    data,
                    offset,
                )
                offset = _require(
                    data,
                    offset,
                    item_length,
                )

    locktime_end = _require(data, offset, 4)
    locktime_bytes = data[offset:locktime_end]
    locktime = int.from_bytes(
        locktime_bytes,
        "little",
    )
    offset = locktime_end
    stripped_parts.append(locktime_bytes)

    stripped = b"".join(stripped_parts)
    txid = _sha256d(stripped)[::-1].hex()
    wtxid = _sha256d(
        data[start:offset]
    )[::-1].hex()

    return (
        {
            "txid": txid,
            "wtxid": wtxid,
            "version": version,
            "locktime": locktime,
            "segwit": segwit,
            "inputs": inputs,
            "outputs": outputs,
            "size": offset - start,
        },
        offset,
    )


def _parse_block(
    raw_block: bytes,
) -> list[dict[str, Any]]:
    if len(raw_block) < 81:
        raise ValueError("Bloc Bitcoin brut invalide.")

    offset = 80
    transaction_count, offset = _read_varint(
        raw_block,
        offset,
    )
    transactions: list[dict[str, Any]] = []

    for _ in range(transaction_count):
        transaction, offset = _parse_transaction(
            raw_block,
            offset,
        )
        transactions.append(transaction)

    if offset != len(raw_block):
        raise ValueError(
            "Données supplémentaires inattendues "
            "après le bloc Bitcoin."
        )

    return transactions


def _explorer_links(txid: str) -> tuple[str, str]:
    return (
        f"https://mempool.space/tx/{txid}",
        f"https://blockstream.info/tx/{txid}",
    )


def _select_candidate_blocks(
    blocks: list[dict[str, Any]],
    *,
    start_ts: int,
    end_ts: int,
    center_ts: int,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Sélectionne les blocs horodatés dans la fenêtre demandée.

    Si aucun bloc Bitcoin ne tombe dans cette fenêtre, conserve le bloc
    temporellement le plus proche. Bitcoin n'horodate pas chaque transaction :
    sans ce repli, une fenêtre courte (par exemple ±30 s) est très souvent vide
    alors qu'un bloc pertinent se trouve quelques minutes avant ou après.
    """
    in_window = [
        block
        for block in blocks
        if start_ts <= int(block.get("timestamp", 0)) <= end_ts
    ]
    if in_window:
        return in_window, False

    if not blocks:
        return [], False

    nearest = min(
        blocks,
        key=lambda block: (
            abs(int(block.get("timestamp", 0)) - center_ts),
            int(block.get("height", 0)),
        ),
    )
    return [nearest], True


def search_bitcoin_window(
    search_date: date,
    search_time: dt_time,
    tolerance_seconds: int = 30,
    amount_btc: str | Decimal | None = None,
    *,
    asset_symbol: str = "BTC",
    api_url: str = DEFAULT_API_URL,
    api_delay: float = DEFAULT_API_DELAY,
    retries: int = 6,
    progress_callback: ProgressCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """Recherche les sorties BTC confirmées dans une fenêtre UTC."""

    if tolerance_seconds < 0:
        raise ValueError(
            "La tolérance doit être positive ou nulle."
        )

    target_asset = str(
        asset_symbol or "BTC"
    ).strip().upper()
    if target_asset != "BTC":
        raise ValueError(
            "Actif Bitcoin non pris en charge."
        )

    target_criterion: AmountCriterion | None = (
        parse_amount_criterion(
            amount_btc,
            max_decimals=8,
        )
    )
    target_amount = (
        target_criterion.amount
        if target_criterion
        else None
    )

    center_dt = datetime.combine(
        search_date,
        search_time,
    ).replace(tzinfo=timezone.utc)
    start_dt = center_dt - timedelta(
        seconds=tolerance_seconds
    )
    end_dt = center_dt + timedelta(
        seconds=tolerance_seconds
    )
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    base = api_url.rstrip("/")
    block_cache: dict[int, dict[str, Any]] = {}

    def request(
        path: str,
        *,
        response_type: str = "json",
        timeout: int = 90,
    ) -> Any:
        url = f"{base}{path}"

        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "BlockchainLookupStreamlit/4.0"
                        )
                    },
                )
                with urllib.request.urlopen(
                    req,
                    timeout=timeout,
                ) as response:
                    payload = response.read()

                if api_delay:
                    time.sleep(api_delay)

                if response_type == "bytes":
                    return payload

                text = payload.decode(
                    "utf-8"
                ).strip()
                if response_type == "text":
                    return text

                return json.loads(text)

            except urllib.error.HTTPError as exc:
                if (
                    exc.code == 429
                    and attempt < retries - 1
                ):
                    wait = 2 + attempt * 2
                    _notify(
                        status_callback,
                        "Limite API Bitcoin atteinte "
                        f"(429) — reprise dans {wait}s…",
                    )
                    time.sleep(wait)
                    continue

                raise BitcoinSearchError(
                    f"Erreur HTTP API Bitcoin : {exc}"
                ) from exc

            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
            ) as exc:
                if attempt == retries - 1:
                    raise BitcoinSearchError(
                        "Impossible de joindre l'API "
                        f"Bitcoin : {exc}"
                    ) from exc

                wait = 1 + attempt
                _notify(
                    status_callback,
                    f"Erreur API Bitcoin ({exc}) — "
                    f"tentative {attempt + 1}/{retries}, "
                    f"reprise dans {wait}s…",
                )
                time.sleep(wait)

        raise BitcoinSearchError(
            "Impossible de joindre l'API Bitcoin."
        )

    def cache_block_page(
        start_height: int,
    ) -> list[dict[str, Any]]:
        page = request(
            f"/blocks/{max(0, start_height)}"
        )
        if not isinstance(page, list):
            raise BitcoinSearchError(
                "Réponse de blocs Bitcoin invalide."
            )

        for block in page:
            try:
                height = int(block["height"])
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue
            block_cache[height] = block

        return page

    def block_at_height(
        height: int,
    ) -> dict[str, Any]:
        if height in block_cache:
            return block_cache[height]

        page = cache_block_page(height)
        for block in page:
            if int(
                block.get("height", -1)
            ) == height:
                return block

        raise BitcoinSearchError(
            f"Bloc Bitcoin {height} introuvable."
        )

    _notify(
        status_callback,
        "Lecture de la hauteur Bitcoin actuelle…",
    )
    tip_height = int(
        request(
            "/blocks/tip/height",
            response_type="text",
        )
    )
    tip_block = block_at_height(tip_height)
    tip_ts = int(
        tip_block.get("timestamp", 0)
    )

    if start_ts > tip_ts:
        tip_dt = datetime.fromtimestamp(
            tip_ts,
            tz=timezone.utc,
        )
        raise BitcoinSearchError(
            "La fenêtre demandée est postérieure "
            "au dernier bloc Bitcoin disponible "
            f"({tip_dt.strftime('%d/%m/%Y %H:%M:%S UTC')})."
        )

    def nearest_height(
        target_ts: int,
    ) -> int:
        try:
            result = request(
                "/v1/mining/blocks/timestamp/"
                f"{target_ts}"
            )
            height = int(result.get("height"))
            if (
                height < 0
                or height > tip_height
            ):
                raise ValueError
            return height
        except (
            BitcoinSearchError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            low = 0
            high = tip_height
            best_height = 0
            best_diff: int | None = None

            while low <= high:
                midpoint = (
                    low + high
                ) // 2
                block = block_at_height(
                    midpoint
                )
                timestamp = int(
                    block.get("timestamp", 0)
                )
                diff = abs(
                    timestamp - target_ts
                )

                if (
                    best_diff is None
                    or diff < best_diff
                ):
                    best_height = midpoint
                    best_diff = diff

                if timestamp < target_ts:
                    low = midpoint + 1
                elif timestamp > target_ts:
                    high = midpoint - 1
                else:
                    return midpoint

            return best_height

    _notify(
        status_callback,
        "Localisation des blocs proches "
        "de la fenêtre UTC…",
    )
    start_near = nearest_height(start_ts)
    end_near = nearest_height(end_ts)

    scan_start = max(
        0,
        min(start_near, end_near)
        - BOUNDARY_BLOCK_MARGIN,
    )
    scan_end = min(
        tip_height,
        max(start_near, end_near)
        + BOUNDARY_BLOCK_MARGIN,
    )

    scanned_meta: list[
        dict[str, Any]
    ] = []
    for height in range(
        scan_start,
        scan_end + 1,
    ):
        scanned_meta.append(
            block_at_height(height)
        )

    candidate_meta, time_fallback_used = (
        _select_candidate_blocks(
            scanned_meta,
            start_ts=start_ts,
            end_ts=end_ts,
            center_ts=int(center_dt.timestamp()),
        )
    )

    candidate_meta.sort(
        key=lambda block: int(
            block["height"]
        )
    )

    time_fallback_offset_seconds: int | None = None
    if time_fallback_used and candidate_meta:
        fallback_block = candidate_meta[0]
        time_fallback_offset_seconds = (
            int(fallback_block.get("timestamp", 0))
            - int(center_dt.timestamp())
        )
        fallback_dt = datetime.fromtimestamp(
            int(fallback_block.get("timestamp", 0)),
            tz=timezone.utc,
        )
        _notify(
            status_callback,
            "Aucun bloc Bitcoin n'est horodaté dans la fenêtre demandée. "
            "Analyse du bloc le plus proche : "
            f"{fallback_block.get('height')} "
            f"({fallback_dt.strftime('%d/%m/%Y %H:%M:%S UTC')}).",
        )

    if (
        len(candidate_meta)
        > MAX_CANDIDATE_BLOCKS
    ):
        raise BitcoinSearchError(
            "La fenêtre couvre trop de blocs "
            "Bitcoin pour le service public. "
            "Réduisez la tolérance."
        )

    transactions_rows: list[
        dict[str, Any]
    ] = []
    operations_rows: list[
        dict[str, Any]
    ] = []
    movements_rows: list[
        dict[str, Any]
    ] = []
    matches_rows: list[
        dict[str, Any]
    ] = []

    total_candidates = len(
        candidate_meta
    )
    analyzed_blocks = 0

    for block_position, block in enumerate(
        candidate_meta,
        start=1,
    ):
        height = int(block["height"])
        block_hash = str(block["id"])
        block_ts = int(
            block["timestamp"]
        )
        block_time = (
            datetime.fromtimestamp(
                block_ts,
                tz=timezone.utc,
            ).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        )

        _notify(
            status_callback,
            f"Bloc Bitcoin {height} — "
            "téléchargement et analyse "
            "du bloc brut…",
        )

        raw_block = request(
            f"/block/{block_hash}/raw",
            response_type="bytes",
            timeout=120,
        )

        try:
            transactions = _parse_block(
                raw_block
            )
        except ValueError as exc:
            raise BitcoinSearchError(
                "Impossible de décoder le "
                f"bloc Bitcoin {height} : {exc}"
            ) from exc

        for tx_index, transaction in enumerate(
            transactions
        ):
            txid = transaction["txid"]
            explorer, secondary = (
                _explorer_links(txid)
            )

            total_output_sats = sum(
                int(output["value_sats"])
                for output
                in transaction["outputs"]
            )
            total_output_btc = (
                Decimal(total_output_sats)
                / SATOSHIS_PER_BTC
            )

            destinations: set[str] = set()
            output_count = 0

            for output in transaction[
                "outputs"
            ]:
                value_sats = int(
                    output["value_sats"]
                )
                if value_sats <= 0:
                    continue

                amount_btc_value = (
                    Decimal(value_sats)
                    / SATOSHIS_PER_BTC
                )
                amount_text = (
                    _format_decimal(
                        amount_btc_value
                    )
                )
                destination = str(
                    output.get(
                        "destination"
                    ) or ""
                )
                script_type = str(
                    output.get(
                        "script_type"
                    ) or "Script"
                )

                if (
                    destination
                    and destination
                    != "OP_RETURN"
                ):
                    destinations.add(
                        destination
                    )

                match_quality = (
                    target_criterion.classify(
                        amount_btc_value
                    )
                    if target_criterion
                    is not None
                    else None
                )
                marker = {
                    "exact": "EXACT",
                    "approximate": "APPROX",
                }.get(
                    match_quality,
                    "",
                )

                output_count += 1
                row = {
                    "block": height,
                    "block_time_utc": (
                        block_time
                    ),
                    "transaction_index": (
                        tx_index
                    ),
                    "signature": txid,
                    "operation_type": (
                        "transfer"
                    ),
                    "sent": (
                        f"{amount_text} BTC"
                    ),
                    "received": "",
                    "source": "",
                    "destination": (
                        destination
                        or script_type
                    ),
                    "account": (
                        destination
                        or script_type
                    ),
                    "evidence": (
                        "Sortie Bitcoin "
                        f"vout #{output['index']}"
                    ),
                    "detail": (
                        f"{script_type} · "
                        f"{value_sats} satoshis"
                    ),
                    "asset": "BTC",
                    "delta_amount": amount_text,
                    "absolute_delta_amount": (
                        amount_text
                    ),
                    "match_target": marker,
                    "explorer": explorer,
                    "secondary_explorer": (
                        secondary
                    ),
                }

                operations_rows.append(row)
                movements_rows.append(row)

                if (
                    match_quality
                    is not None
                ):
                    matches_rows.append(
                        {
                            "match_type": (
                                "transfer"
                            ),
                            "match_role": (
                                "Sortie BTC exacte"
                                if match_quality
                                == "exact"
                                else (
                                    "Sortie BTC "
                                    "approchée"
                                )
                            ),
                            "match_quality": (
                                match_quality
                            ),
                            "block": height,
                            "block_time_utc": (
                                block_time
                            ),
                            "transaction_index": (
                                tx_index
                            ),
                            "signature": txid,
                            "matched_amount": (
                                amount_text
                            ),
                            "matched_asset": (
                                "BTC"
                            ),
                            "sent": (
                                f"{amount_text} BTC"
                            ),
                            "received": "",
                            "source": "",
                            "destination": (
                                destination
                                or script_type
                            ),
                            "account": (
                                destination
                                or script_type
                            ),
                            "detail": (
                                "Sortie vout "
                                f"#{output['index']} · "
                                f"{script_type} · "
                                f"{value_sats} "
                                "satoshis"
                            ),
                            "explorer": (
                                explorer
                            ),
                            "secondary_explorer": (
                                secondary
                            ),
                        }
                    )

            transactions_rows.append(
                {
                    "block": height,
                    "block_time_utc": (
                        block_time
                    ),
                    "transaction_index": (
                        tx_index
                    ),
                    "signature": txid,
                    "status": "SUCCESS",
                    "account_count": len(
                        destinations
                    ),
                    "accounts": (
                        " | ".join(
                            sorted(
                                destinations
                            )
                        )
                    ),
                    "operation_count": (
                        output_count
                    ),
                    "operation_summary": (
                        f"{output_count} "
                        "sortie(s) BTC · "
                        f"{_format_decimal(total_output_btc)} "
                        "BTC au total"
                    ),
                    "explorer": explorer,
                    "secondary_explorer": (
                        secondary
                    ),
                }
            )

        analyzed_blocks += 1
        if progress_callback:
            progress_callback(
                block_position,
                total_candidates,
                height,
            )

    def reverse_key(
        row: dict[str, Any],
    ) -> tuple[int, int]:
        return (
            int(row.get("block", 0)),
            int(
                row.get(
                    "transaction_index",
                    0,
                )
            ),
        )

    transactions_rows.sort(
        key=reverse_key,
        reverse=True,
    )
    operations_rows.sort(
        key=reverse_key,
        reverse=True,
    )
    movements_rows.sort(
        key=reverse_key,
        reverse=True,
    )
    matches_rows.sort(
        key=reverse_key,
        reverse=True,
    )

    first_candidate = (
        int(candidate_meta[0]["height"])
        if candidate_meta
        else start_near
    )
    last_candidate = (
        int(candidate_meta[-1]["height"])
        if candidate_meta
        else end_near
    )

    return {
        "network": "Bitcoin",
        "start_dt": start_dt,
        "end_dt": end_dt,
        "start_block": first_candidate,
        "end_block": last_candidate,
        "query_start_block": scan_start,
        "query_end_block": scan_end,
        "candidate_blocks": (
            total_candidates
        ),
        "skipped_blocks": (
            scan_end
            - scan_start
            + 1
            - total_candidates
        ),
        "time_fallback_used": (
            time_fallback_used
        ),
        "time_fallback_offset_seconds": (
            time_fallback_offset_seconds
        ),
        "time_fallback_block": (
            int(candidate_meta[0]["height"])
            if time_fallback_used and candidate_meta
            else None
        ),
        "time_fallback_block_timestamp": (
            int(candidate_meta[0]["timestamp"])
            if time_fallback_used and candidate_meta
            else None
        ),
        "analyzed_blocks": (
            analyzed_blocks
        ),
        "transactions": (
            transactions_rows
        ),
        "operations": operations_rows,
        "movements": movements_rows,
        "matches": matches_rows,
        "target_amount": target_amount,
        "target_asset": target_asset,
        "target_amount_precision": (
            target_criterion.decimal_places
            if target_criterion
            else None
        ),
        "target_amount_tolerance": (
            target_criterion.tolerance
            if target_criterion
            else None
        ),
        "time_basis": "block_timestamp",
    }
