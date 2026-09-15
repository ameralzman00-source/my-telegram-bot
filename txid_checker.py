# -*- coding: utf-8 -*-
"""Read-only BSC BEP20 USDT TXID verification for the P2P bot.

This module never signs or sends blockchain transactions.
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_DOWN

BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.bnbchain.org").strip()
BSC_RPC_URLS = [x.strip() for x in os.getenv("BSC_RPC_URLS", "").split(",") if x.strip()]
if BSC_RPC_URL and BSC_RPC_URL not in BSC_RPC_URLS:
    BSC_RPC_URLS.insert(0, BSC_RPC_URL)
for _url in (
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-dataseed2.bnbchain.org",
    "https://bsc-dataseed3.bnbchain.org",
    "https://bsc-dataseed4.bnbchain.org",
):
    if _url not in BSC_RPC_URLS:
        BSC_RPC_URLS.append(_url)
USDT_BSC_CONTRACT = os.getenv(
    "USDT_BSC_CONTRACT",
    "0x55d398326f99059fF775485246999027B3197955",
).strip()
USDT_BSC_DECIMALS = 18


def normalize_address(value):
    value = (value or "").strip()
    return value.lower() if re.fullmatch(r"0x[a-fA-F0-9]{40}", value) else ""


def normalize_txid(value):
    value = (value or "").strip()
    return value.lower() if re.fullmatch(r"0x[a-fA-F0-9]{64}", value) else ""


def _bsc_rpc_call(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    request = urllib.request.Request(
        BSC_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_error = None
    for rpc_url in BSC_RPC_URLS:
        request.full_url = rpc_url
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = json.loads(response.read().decode("utf-8"))
            if body.get("error"):
                last_error = RuntimeError(str(body["error"]))
                continue
            return body.get("result")
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"BSC RPC unavailable: {last_error}")


def _verify_bep20_usdt_deposit_sync(txid, expected_sender, expected_recipient, expected_amount):
    txid = normalize_txid(txid)
    sender = normalize_address(expected_sender)
    recipient = normalize_address(expected_recipient)
    contract = normalize_address(USDT_BSC_CONTRACT)

    if not txid:
        return {"ok": False, "reason": "صيغة TXID غير صحيحة."}
    sender_check_enabled = bool(sender)
    if not recipient:
        return {"ok": False, "reason": "عنوان BEP20 للوسيط غير مضبوط."}
    if not contract:
        return {"ok": False, "reason": "عنوان عقد USDT على BSC غير مضبوط."}

    receipt = _bsc_rpc_call("eth_getTransactionReceipt", [txid])
    if not receipt:
        return {"ok": False, "reason": "المعاملة غير موجودة أو لم تُؤكد على الشبكة بعد."}
    if receipt.get("status") != "0x1":
        return {"ok": False, "reason": "المعاملة فشلت على الشبكة."}

    transaction = _bsc_rpc_call("eth_getTransactionByHash", [txid])
    if not transaction:
        return {"ok": False, "reason": "تعذر قراءة بيانات المعاملة."}

    # Do not require tx.to to equal the USDT contract. Wallets can send USDT
    # through a router/contract while the actual USDT movement is still
    # represented by a Transfer event emitted by the USDT contract.
    # Security is based on the successful receipt + exact USDT Transfer log.
    tx_sender = normalize_address(transaction.get("from"))
    if sender_check_enabled and tx_sender and tx_sender != sender:
        return {"ok": False, "reason": "مرسل المعاملة لا يطابق عنوان البائع المتوقع."}

    expected_units = int((Decimal(str(expected_amount)) * (Decimal(10) ** USDT_BSC_DECIMALS)).to_integral(rounding=ROUND_DOWN))
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    for log in receipt.get("logs", []):
        if normalize_address(log.get("address")) != contract:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or (topics[0] or "").lower() != transfer_topic:
            continue
        from_topic = (topics[1] or "").lower()
        to_topic = (topics[2] or "").lower()
        if len(from_topic) != 66 or len(to_topic) != 66:
            continue
        log_sender = "0x" + from_topic[-40:]
        log_recipient = "0x" + to_topic[-40:]
        try:
            amount_units = int((log.get("data") or "0x0"), 16)
        except ValueError:
            continue
        if log_recipient == recipient and amount_units == expected_units and (not sender_check_enabled or log_sender == sender):
            try:
                block_number = int((receipt.get("blockNumber") or "0x0"), 16)
            except ValueError:
                block_number = 0
            confirmations = None
            try:
                latest = int(_bsc_rpc_call("eth_blockNumber", []), 16)
                confirmations = max(0, latest - block_number + 1)
            except Exception:
                pass
            return {
                "ok": True,
                "reason": (
                    "تم العثور على تحويل USDT مطابق للصفقة والمرسل."
                    if sender_check_enabled
                    else "تم العثور على تحويل USDT مطابق للمبلغ والوسيط؛ لم يتم فحص عنوان البائع لأنه غير مضبوط."
                ),
                "block_number": block_number,
                "confirmations": confirmations,
            }

    return {"ok": False, "reason": "لم يتم العثور على تحويل USDT يطابق المرسل والوسيط والمبلغ المطلوب داخل المعاملة."}


async def verify_bep20_usdt_deposit(txid, expected_sender, expected_recipient, expected_amount):
    try:
        return await asyncio.to_thread(
            _verify_bep20_usdt_deposit_sync,
            txid, expected_sender, expected_recipient, expected_amount,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "reason": "تعذر الاتصال بأي خادم BSC للتحقق التلقائي حالياً. تحقق من اتصال السيرفر بالإنترنت أو جرّب إعادة الفحص."}
    except Exception as exc:
        return {"ok": False, "reason": f"تعذر إكمال الفحص التلقائي: {exc}"}
