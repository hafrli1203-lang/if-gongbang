"""Print a short Markdown summary of a JUnit XML report: commit, counts, and skipped reasons."""
import os
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    path, title = sys.argv[1], sys.argv[2]
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    if not os.path.exists(path):
        print(f"### {title} ({sha})\n\nNo report was produced.")
        return 0
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    total = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    passed = total - failures - errors - skipped
    print(f"### {title} ({sha})\n")
    print("| passed | failed | errors | skipped | total |\n|---|---|---|---|---|")
    print(f"| {passed} | {failures} | {errors} | {skipped} | {total} |\n")
    reasons: dict[str, int] = {}
    for case in root.iter("testcase"):
        skip = case.find("skipped")
        if skip is not None:
            msg = (skip.get("message") or "").strip()[:120]
            reasons[msg] = reasons.get(msg, 0) + 1
    if reasons:
        print("Skipped reasons:\n")
        for msg, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"- {n} × {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
