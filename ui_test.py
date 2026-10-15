"""In-process UI verification of the Streamlit dashboard (no browser needed).

Run:  python ui_test.py
"""

import sys

from streamlit.testing.v1 import AppTest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    failures = []
    at = AppTest.from_file("app.py", default_timeout=180)
    at.run()

    if at.exception:
        failures.append(f"dashboard raised on initial render: {at.exception}")
    else:
        print(f"[OK] initial render: {len(at.sidebar.button)} button(s), "
              f"{len(at.sidebar.selectbox)} selectbox(es), {len(at.sidebar.slider)} slider(s)")

    if at.sidebar.button:
        at.sidebar.button[0].click()
        at.run()
        if at.exception:
            failures.append(f"dashboard raised after Compute click: {at.exception}")
        else:
            metrics = [f"{m.label}={m.value}" for m in at.main.metric]
            if len(metrics) < 7:
                failures.append(f"expected >= 7 metric cards, found {len(metrics)}")
            else:
                print("[OK] compute ran, metrics: " + " | ".join(metrics[:8]))
            if len(at.main.dataframe) < 2:
                failures.append(f"expected itinerary + comparison tables, found {len(at.main.dataframe)}")
            else:
                print(f"[OK] {len(at.main.dataframe)} dataframes rendered (itinerary + optimality gap)")

            headers = [h.value for h in at.main.subheader] + [h.value for h in at.main.header]
            if not any("\u2192" in h for h in headers):
                failures.append("route header with origin -> destination missing")
            else:
                print(f"[OK] route header: {headers[0]}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL UI TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())