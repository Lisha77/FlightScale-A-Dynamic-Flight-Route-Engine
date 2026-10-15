"""In-process UI verification with the live-API toggle enabled (cold cache).

Run:  python ui_live_test.py      (requires internet; skips provider checks offline)
"""

import sys
import time

from streamlit.testing.v1 import AppTest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    failures = []
    t0 = time.perf_counter()
    at = AppTest.from_file("app.py", default_timeout=180)
    at.run()
    if at.exception:
        failures.append(f"initial render raised: {at.exception}")
        print("\nFAILURES:", failures)
        return 1

    # Sidebar toggle[0] = "Use live APIs" (the only toggle visible when off).
    at.sidebar.toggle[0].set_value(True).run()
    if at.exception:
        failures.append(f"enabling live mode raised: {at.exception}")
    else:
        print("[OK] live mode enabled")

    # button[0] must remain "Compute optimal route" (Refresh only renders in live mode).
    at.sidebar.button[0].click().run()
    if at.exception:
        failures.append(f"compute (live) raised: {at.exception}")
    else:
        elapsed = time.perf_counter() - t0
        metrics = [f"{m.label}={m.value}" for m in at.main.metric]
        if len(metrics) < 7:
            failures.append(f"expected >= 7 metric cards, found {len(metrics)}")
        else:
            print(f"[OK] live compute in {elapsed:.1f}s, metrics: " + " | ".join(metrics[:8]))

        result = at.session_state["route_result"] if "route_result" in at.session_state else None
        if result is None:
            failures.append("route_result missing after live compute")
        else:
            providers = (result.get("api") or {}).get("providers") or {}
            print(f"[OK] refinements={result.get('refinements')}, "
                  f"providers={providers}, path hops={len(result.get('path') or [])}")
            if providers:
                print("[OK] live providers reachable through the UI")
            else:
                print("[SKIP] providers unreachable (offline?) — UI still rendered")

        if len(at.main.dataframe) < 2:
            failures.append(f"expected 2 dataframes, found {len(at.main.dataframe)}")
        else:
            print(f"[OK] {len(at.main.dataframe)} dataframes rendered")

    # Toggle back to simulation to confirm both modes render.
    at.sidebar.toggle[0].set_value(False).run()
    if at.exception:
        failures.append(f"disabling live mode raised: {at.exception}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nALL UI LIVE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())