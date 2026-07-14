/**
 * Ostium read-only data reader (step-8 data source for the analysis console).
 *
 * HARD BOUNDARY — same rule as Hyperliquid's Exchange endpoint and BydFi's
 * Trading/Account interfaces. @ostium/builder-sdk ships full trading
 * capability; NONE of it may ever be used from this project:
 *
 *   Forbidden client modes (never instantiate):
 *     createSelfAndSelf, createSelfAndGasless,
 *     createDelegatedAndSelf, createDelegatedAndGasless
 *
 *   Forbidden write methods (never call):
 *     approveUsdc, setupGaslessDelegation, setDelegate, removeDelegate,
 *     openTrade, closeTrade, modifyOrder, updateCollateral, cancelOrder
 *
 *   Forbidden transaction builders (never call):
 *     getSetupGaslessDelegationTx, getApproveUsdcTx, getSetDelegateTx,
 *     getRemoveDelegateTx, getOpenTradeTx, getCloseTradeTx,
 *     getCancelOrderTx, getModifyOrderTx, getUpdateCollateralTx
 *
 * The ONLY permitted construction is OstiumClient.createReadOnly(): no
 * wallet, no private key, no signer is ever configured. In this mode the
 * SDK itself enforces the boundary — write methods throw INVALID_CONFIG
 * (docs.ostium.com/developer/client-modes/read-only). Do not "fix" such an
 * error by switching modes; it means someone violated this file's rule.
 *
 * Usage (prints JSON to stdout for the Python side to consume):
 *   node read_client.js pairs    -> getPairs()
 *   node read_client.js prices   -> getAllPrices()
 */

import { OstiumClient } from "@ostium/builder-sdk";

async function main() {
  const what = process.argv[2];
  if (!["pairs", "prices"].includes(what)) {
    console.error("usage: node read_client.js <pairs|prices>");
    process.exit(2);
  }

  // Read-only: no key, no signer, write methods throw INVALID_CONFIG.
  const client = await OstiumClient.createReadOnly();

  const data = what === "pairs" ? await client.getPairs()
                                : await client.getAllPrices();
  console.log(JSON.stringify(data, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
