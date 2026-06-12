# Implementation Plan Review: spoolman-cfs-sync (Second Pass)

## Status: Approved for Implementation

The implementation plan has been updated (see `Review Resolution Notes` in the plan) to address all findings from the initial review.

### Resolution of Previous Findings

1. **Spoolman `/use` Non-Idempotency on Timeouts (Resolved)**
   - **Fix**: The plan now introduces a `timeout_uncertain` status and strictly prohibits automatic retries for this state. It mandates clear UI messaging requiring user verification before a manual retry.
   - **Assessment**: Excellent. This is the safest way to handle non-idempotent remote APIs without complex distributed locking.

2. **Moonraker Native Spoolman Conflict Mitigation (Resolved)**
   - **Fix**: The plan now includes best-effort detection for Moonraker's native `[spoolman]` component (e.g., via Moonraker component lists) and surfaces a persistent UI warning if detected. It correctly decides against automatically issuing `CLEAR_ACTIVE_SPOOL` commands to avoid unintended printer-control side effects.
   - **Assessment**: Great architectural boundary decision. The persistent warning is sufficient and safe for v1.

3. **`job_key` Uniqueness and Reliability (Resolved)**
   - **Fix**: The plan clearly defines a `stable print key` prioritizing Moonraker's `job_id`, falling back to a composite of filename and start/end timestamps.
   - **Assessment**: Solid approach to ensure uniqueness across restarts and repeated prints of the same file.

4. **Syncing Both Length and Weight (Resolved)**
   - **Fix**: The plan now explicitly states it will send only `use_length` in v1, allowing Spoolman to derive weight from its own filament metadata.
   - **Assessment**: Good simplification that avoids multi-source-of-truth discrepancies.

5. **Implementation Sequence Improvement (Resolved)**
   - **Fix**: Idempotency and the sync-record gate have been moved earlier in the implementation order (Step 4), ensuring all subsequent dry-run and real sync paths route through it.
   - **Assessment**: Safe and logical sequencing.

---

## Answers to Focus Areas (Re-evaluated)

1. **Is direct Spoolman sync the right implementation path for this app?**
   Yes. It avoids the timing races of active-spool switching during rapid mid-print CFS tool changes.
2. **Is post-print reconciliation a safe first sync mode?**
   Yes. It limits state changes to a single reliable event and is much easier to make idempotent.
3. **Is the proposed idempotency model strong enough to avoid duplicate Spoolman deductions?**
   Yes, with the addition of the `timeout_uncertain` handling, the idempotency model is now very robust against both logical retries and network failures.
4. **Is `POST /api/v1/spool/{spool_id}/use` used appropriately?**
   Yes. Limiting it to `use_length` only is the correct and safest approach.
5. **Does the plan handle unmapped/unidentified CFS slots gracefully?**
   Yes, `skipped_unmapped` is a safe and explicit fallback.
6. **Does it sufficiently prevent double-accounting with Moonraker’s native Spoolman integration?**
   Yes, the active detection and persistent UI warning is a sufficient and safe mitigation for v1.
7. **Are the proposed state/config shapes reasonable?**
   Yes, the updated schemas accurately model the complexities of sync states (including timeouts and conflicts) without overcomplicating the data model.
8. **Are the proposed backend/UI implementation steps in a safe order?**
   Yes, the revised order builds the idempotency gate before any dry-run or real-sync logic, which is the safest path.
9. **What should be added before development begins?**
   Nothing. The plan is comprehensive and addresses all identified risks.
10. **What should be simplified for v1?**
    The scope is appropriately constrained. The decision to avoid printer-control side effects (`CLEAR_ACTIVE_SPOOL`) is a smart simplification.

## Conclusion
The implementation plan is solid, safe, and ready for development. All "Ready Criteria Before Coding" have been fully met.
