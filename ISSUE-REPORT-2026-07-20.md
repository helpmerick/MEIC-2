# Issue Report: Close Order and TPF/TPT Functionality - 2026-07-20

**Created:** 2026-07-20  
**Reported By:** Rick (User)  
**Status:** NEEDS VERIFICATION  

## Summary
During live trading of SPX 0DTE iron condors, the manual Close button and automatic Floor/Target (TPF/TPT) exit mechanisms failed to function properly. Market order creation was broken due to incorrect order type handling and price parameter misuse. A fix has been applied and pushed, but several concerns remain unresolved.

## Issues Fixed

### 1. Market Order Type Not Recognized (FIXED)
**Problem:** The system was attempting to create market orders with order_type="market", but this type was not recognized by the tastytrade adapter, causing `AttributeError: type object 'OrderType' has no attribute 'MARKETABLE'`.

**Root Cause:** 
- `marketable_close()` was creating orders as `order_type="marketable_limit"` WITH a price parameter
- Later attempts to use true market orders (`order_type="market"`) were not mapped in the adapter's type_map
- The enum value used was incorrect (attempted `OrderType.MARKETABLE` instead of `OrderType.MARKET`)

**Fix Applied:**
- Added "market" to ORDER_TYPES in order_intent.py
- Ensured "market" is NOT in PRICED_TYPES (so price is optional)
- Changed `marketable_close()` to create `order_type="market"` without price parameter
- Updated tastytrade adapter type_map to include `"market": OrderType.MARKET`
- Removed price parameter from all `marketable_close()` call sites:
  - close_entry.py line 145-149 (manual/TPF/TPT closes)
  - watchdog.py line 114-117 (escalation buybacks)

**Affected Functionality:**
- Manual Close button (CLS-02)
- Automatic Floor exits (TPF)
- Automatic Target exits (TPT)
- Stop escalation buybacks

## Unresolved Concerns

### 2. TPF/TPT Close Order Handling (UNVERIFIED)
**Concern:** The close logic (assemble_close_inputs → CloseEntry.close) is DESIGNED to look for resting protective stops and cancel/replace them with new close orders. However, this behavior has NOT been verified in live trading.

**Expected Behavior (per code review):**
1. `assemble_close_inputs()` calls `broker.working_orders()` to get all resting orders
2. Identifies `stop_market` orders (protective stops) for each side
3. Passes these stop IDs to `CloseEntry.close()`
4. `CloseEntry.close()` calls `broker.replace()` to atomically cancel + submit new close
5. If no resting stop exists, submits new close order directly

**Verification Needed:**
- Does `broker.working_orders()` consistently return all resting orders?
- Does `broker.replace()` actually execute atomically?
- Do TPF/TPT monitors correctly trigger when profit reaches floor/target levels?
- Are there race conditions between multiple concurrent close attempts?

**Testing Required:**
- Set Floor at specific % on profitable position
- Monitor bot logs for "working_orders() returned X", "Found resting stop", "broker.replace()"
- Confirm trade auto-closes when floor/target is reached
- Check logs for any errors during close sequence

### 3. Idempotency Issue (CRITICAL)
**Problem Observed:** During testing, a close button press was buffered in the system. When the bot restarted, the old close request was re-processed and closed trades unintentionally.

**Impact:** Unintended trade closures, potential loss of position management

**Questions for Creator:**
- Is there HTTP request buffering that could replay old requests on restart?
- Does the idempotency_key (ORD-04) protection work correctly for close orders?
- Should close requests be cleared/deduped on bot restart?
- Is there a way to prevent replayed close requests from executing?

## Code Changes
Commit: `8e1f36e` - "fix: market order handling for close operations (CLS-01/TPF/TPT)"

Files Modified:
- `backend/src/meic/application/order_intent.py`
- `backend/src/meic/application/close_entry.py`
- `backend/src/meic/adapters/tastytrade/adapter.py`
- `backend/src/meic/application/watchdog.py`

## Recommendations for Creator

### Immediate (Before Next Trading Day)
1. Review the market order fix and verify OrderType.MARKET is correct for your tastytrade SDK version
2. Audit the idempotency_key handling in order submissions and HTTP layer
3. Add logging to `assemble_close_inputs()` and `CloseEntry.close()` to track order cancellation flow

### Short-term (This Week)
1. Implement automated tests for TPF/TPT close sequences
2. Add integration tests that verify resting orders are properly cancelled before new closes
3. Review `broker.working_orders()` to ensure it's consistently returning all open orders
4. Audit HTTP request buffering/replay logic

### Documentation
1. Document the CLS-01 close sequence and expected log messages
2. Add troubleshooting guide for when TPF/TPT closes don't trigger
3. Clarify the idempotency guarantees and how they apply to close orders

## Test Plan for Tomorrow
User will test with logging enabled:
1. Place trade with Floor at 25%, Target at 60%+
2. Monitor bot logs during profit fluctuations
3. Report if closes trigger at expected levels
4. Check for any "working_orders", "broker.replace", or order-related errors

## Related Code Sections
- `backend/src/meic/composition/close_assembly.py` - Lines 87-93 (order collection)
- `backend/src/meic/application/close_entry.py` - Lines 143-187 (close execution)
- `backend/src/meic/adapters/api/server.py` - Lines 1361-1402 (TPF/TPT trigger)
- `backend/src/meic/application/tpf_monitor.py` - Floor monitoring
- `backend/src/meic/application/tpt_monitor.py` - Target monitoring

---

**Status:** Awaiting verification of TPF/TPT close logic and resolution of idempotency issue before declaring live trading safe.
